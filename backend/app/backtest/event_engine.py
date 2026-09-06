"""事件驱动回测时钟引擎 (日频)。

把 EventContext (数据视图) 与 EventPortfolio (组合账本) 粘成逐交易日循环:

- 交易日序列取自面板 date 列 (升序); warmup 天数只进 history 视图, 不进脚本时相。
- 每日流程: before_trading_start(可选) -> handle_data(必填, 下单入口)
  -> 风控/挂单退出 -> 订单撮合(卖出先于买入) -> 标记更新 -> after_trading_end(可选)。
- 成交口径映射既有 MatcherConfig: close_t = 当日收盘, open_t+1 = 次日开盘
  (提交当日无成交K, 订单跨日挂单, 由组合层次日撮合)。
- entry_end 之后的日期为 mode=full 尾部: 不再调用脚本时相, 只跑退出与撮合。
- progress/cancel 复用既有 SSE 语义: 逐日上报, 取消即停并期末强平。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from app.backtest.engine import MatcherConfig, TradeRecord
from app.backtest.event_context import EventContext
from app.backtest.event_portfolio import DayBar, EventPortfolio
from app.backtest.order_router import is_suspended_bar
from app.strategy.engine import StrategyDef

_EXECUTION_COLUMNS = frozenset(
    {
        "symbol",
        "date",
        "name",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "signal_limit_up",
        "signal_limit_down",
    }
)


@dataclass
class EventRunResult:
    """事件回测 run 的原始产出; 由 StrategyBacktestService 组装为标准结果契约。"""

    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    drawdown_curve: list[dict] = field(default_factory=list)
    reject_counts: dict[str, int] = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    orders_total: int = 0
    cancelled: bool = False
    elapsed_ms: float = 0.0


class EventBacktestEngine:
    """日频事件回测时钟; 每次 run 独立实例状态, 可复用面板多次 run。"""

    def run(
        self,
        strategy: StrategyDef,
        panel: pl.DataFrame,
        params: dict,
        matcher: MatcherConfig,
        *,
        start: date,
        asset_type: str = "stock",
        entry_end: date | None = None,
        universe_by_day: dict[str, set[str]] | None = None,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> EventRunResult:
        started = time.perf_counter()
        result = EventRunResult()
        strategy_id = str(strategy.meta.get("id", "<unknown>"))
        if strategy.execution_backend != "event":
            raise ValueError("EventBacktestEngine 只能运行 execution_backend == 'event' 的策略")
        if strategy.handle_data_fn is None:
            raise ValueError("event strategy must declare handle_data")
        if matcher.entry_fill not in ("close_t", "open_t+1") or matcher.exit_fill not in (
            "close_t",
            "open_t+1",
        ):
            raise ValueError("事件回测 v1 仅支持 close_t / open_t+1 成交口径")

        missing = (
            _EXECUTION_COLUMNS
            - set(panel.columns)
            - {"name", "volume", "signal_limit_up", "signal_limit_down"}
        )
        if missing:
            raise ValueError(f"事件回测面板缺少执行列: {sorted(missing)}")

        dates = [d for d in panel["date"].unique().sort().to_list() if d >= start]
        if not dates:
            return result

        portfolio = EventPortfolio(matcher)
        context = EventContext(
            params=dict(params),
            asset_type=asset_type,
            panel=panel,
            dates=panel["date"].unique().sort().to_list(),  # 全窗口日期 (含 warmup)
            day_index=0,
            phase="before_trading_start",
            history_bars=int(strategy.event_history_bars or 60),
            portfolio=portfolio,
        )
        universe_map = universe_by_day or {}

        last_bars: dict[str, DayBar] = {}
        last_equity = 0.0
        peak = float(matcher.initial_capital)
        for day_index, day in enumerate(dates):
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                break
            bars = self._day_bars(panel, day)
            last_bars = bars
            day_str = day.isoformat()
            context.day_index = _date_index(context.dates, day)
            # 显式声明的空 universe 保持空集 (fail-closed), 不静默回退全市场。
            universe = universe_map[day_str] if day_str in universe_map else set(bars)
            portfolio.begin_day(day, context.day_index, universe)
            script_active = entry_end is None or day <= entry_end

            if script_active:
                if strategy.before_trading_start_fn is not None:
                    context.phase = "before_trading_start"
                    self._invoke(
                        strategy.before_trading_start_fn,
                        context,
                        "before_trading_start",
                        strategy_id,
                    )
                context.phase = "handle_data"
                self._invoke(strategy.handle_data_fn, context, "handle_data", strategy_id)
                result.orders_total += len(portfolio.orders_today)

            # 撮合: 风控/挂单退出先于订单 (卖出先于买入由组合层保证)。
            portfolio.apply_risk_exits(bars, self._exit_info(bars, matcher.exit_fill))
            portfolio.process_pending_orders(self._fill_info(bars, day_str, matcher))

            if script_active and strategy.after_trading_end_fn is not None:
                context.phase = "after_trading_end"
                self._invoke(
                    strategy.after_trading_end_fn,
                    context,
                    "after_trading_end",
                    strategy_id,
                )

            portfolio.update_marks(bars)
            equity = portfolio.cash + portfolio.market_value()
            peak = max(peak, equity)
            drawdown = (equity - peak) / peak if peak > 0 else 0.0
            result.equity_curve.append(
                {
                    "date": day_str,
                    "value": round(float(equity), 2),
                    "cash": round(float(portfolio.cash), 2),
                    "positions": len(portfolio.positions),
                    "exposure": round(float(portfolio.market_value() / equity), 4)
                    if equity > 0
                    else 0.0,
                }
            )
            result.drawdown_curve.append(
                {
                    "date": day_str,
                    "value": round(float(drawdown), 4),
                }
            )
            last_equity = equity
            if progress_cb is not None:
                with suppress(Exception):
                    progress_cb(
                        {
                            "day": day_index + 1,
                            "total": len(dates),
                            "date": day_str,
                            "equity": round(float(last_equity), 2),
                        }
                    )

        portfolio.finalize(last_bars)
        result.trades = list(portfolio.trades)
        result.reject_counts = portfolio.reject_counts()
        result.state = dict(context.state)
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return result

    # ── 内部 ──────────────────────────────────────────────────

    @staticmethod
    def _invoke(fn: Callable, context: EventContext, phase: str, strategy_id: str) -> None:
        try:
            fn(context)
        except Exception as exc:
            raise RuntimeError(f"策略 {strategy_id} {phase} 执行失败: {exc}") from exc

    @staticmethod
    def _day_bars(panel: pl.DataFrame, day: date) -> dict[str, DayBar]:
        day_df = panel.filter(pl.col("date") == day)
        bars: dict[str, DayBar] = {}
        has_volume = "volume" in panel.columns
        has_name = "name" in panel.columns
        has_limit_up = "signal_limit_up" in panel.columns
        has_limit_down = "signal_limit_down" in panel.columns
        for row in day_df.iter_rows(named=True):
            symbol = str(row["symbol"])
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row["volume"]) if has_volume and row.get("volume") is not None else 0.0
            bars[symbol] = DayBar(
                symbol=symbol,
                name=str(row["name"]) if has_name and row.get("name") is not None else symbol,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                has_volume=has_volume,
                limit_up=bool(row["signal_limit_up"]) if has_limit_up else False,
                limit_down=bool(row["signal_limit_down"]) if has_limit_down else False,
                suspended=is_suspended_bar(
                    open_,
                    high,
                    low,
                    close,
                    volume=volume,
                    has_volume=has_volume,
                ),
            )
        return bars

    @staticmethod
    def _exit_info(bars: dict[str, DayBar], exit_fill: str):
        """风控/到期退出: 触发日 bar 按 exit_fill 口径出价 (镜像矩阵组合撮合)。"""

        def exit_info(symbol: str) -> tuple[DayBar, float] | None:
            bar = bars.get(symbol)
            if bar is None:
                return None
            price = float(bar.open) if exit_fill == "open_t+1" else float(bar.close)
            return bar, price

        return exit_info

    @staticmethod
    def _fill_info(bars: dict[str, DayBar], day_str: str, matcher: MatcherConfig):
        """订单撮合出价: close_t 当日收盘; open_t+1 提交当日无成交K, 次日开盘。"""

        def fill_info(order: dict) -> tuple[DayBar, float] | None:
            fill = matcher.entry_fill if order["side"] == "buy" else matcher.exit_fill
            if fill == "open_t+1" and order["day"] == day_str:
                return None
            bar = bars.get(order["symbol"])
            if bar is None:
                return None
            price = float(bar.open) if fill == "open_t+1" else float(bar.close)
            return bar, price

        return fill_info


def _date_index(dates: list[date], day: date) -> int:
    return dates.index(day)
