"""事件驱动组合状态机 - 现金/持仓/订单簿/挂单/风控。

口径与矩阵组合撮合 (_simulate_portfolio_matrix) 对齐:

- 买入: shares = floor(amount / (price*(1+buy_cost)) / 100) * 100;
  entry_value = shares*price*(1+buy_cost); amount 是预算上限, 零头回现金。
- 仓位上限: max_positions (持仓数), max_exposure_pct (总仓位/总资产);
  max_positions=0 与矩阵组合撮合同口径: 不建仓 (所有买单 buy_no_slot)。
- T+1: 当日成交的仓位当日不可卖 (脚本卖单提交时拒绝; 风控跳过入场日)。
- 退出优先级 (高->低): pending(历史挂单) > 风控(止损/移动止损/移动止盈/止盈)
  > max_hold(到期) > end(期末)。脚本卖单即 "signal" 退出, 与挂单/风控共用同一管线。
- 确定性: 卖出先于买入处理, 同侧按提交顺序; 风控按持仓插入顺序遍历。

v1 边界: 不支持对已持仓标的加仓 (buy_already_held 拒绝), 不支持 partial fill。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date

import polars as pl

from app.backtest.engine import MatcherConfig, TradeRecord
from app.backtest.order_router import can_buy, can_sell

# 卖出侧订单在 fill 阶段的退出原因: 脚本主动卖单 == 既有口径的 "signal"。
_SCRIPT_SELL_REASON = "signal"


@dataclass(frozen=True)
class DayBar:
    """单个标的在某个交易日的撮合输入 (面板列 + 涨跌停/停牌判定)。"""

    symbol: str
    name: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    has_volume: bool = False
    limit_up: bool = False
    limit_down: bool = False
    suspended: bool = False


class EventPortfolio:
    """事件回测的组合账本; 每次 run 独立实例, 顺序确定性由调用方时序保证。"""

    def __init__(self, config: MatcherConfig) -> None:
        self.config = config
        self.cash = float(config.initial_capital)
        self.positions: dict[str, dict] = {}
        self.trades: list[TradeRecord] = []
        self.orders_today: list[dict] = []
        self._pending: list[dict] = []
        self._sold_today: set[str] = set()
        self._last_close: dict[str, float] = {}
        self._day_index = 0
        self._day_str = ""
        self._universe: set[str] = set()
        self._reject_counts: dict[str, int] = {}
        self._buy_cost = config.buy_cost_pct()
        self._sell_cost = config.sell_cost_pct()
        self._max_positions = max(int(config.max_positions), 0)
        self._max_exposure = min(max(float(config.max_exposure_pct), 0.0), 1.0)

    # ── 交易日推进 ────────────────────────────────────────────

    def begin_day(self, day: date, day_index: int, universe: set[str]) -> None:
        """进入新交易日: 重置当日状态, 所有持仓 hold_days += 1。"""
        self._day_str = day.isoformat()
        self._day_index = day_index
        self._universe = set(universe)
        self.orders_today = []
        self._sold_today = set()
        for pos in self.positions.values():
            pos["hold_days"] = int(pos["hold_days"]) + 1

    def update_marks(self, bars: dict[str, DayBar]) -> None:
        """撮合完成后更新峰值价与收盘价基准 (镜像现有组合撮合的收尾步骤)。"""
        for pos in self.positions.values():
            bar = bars.get(str(pos["symbol"]))
            if bar is None:
                continue
            if _valid_price(bar.high):
                pos["max_high"] = max(float(pos["max_high"]), float(bar.high))
        for bar in bars.values():
            if _valid_price(bar.close):
                self._last_close[bar.symbol] = float(bar.close)

    # ── 下单 (提交期静态校验; 成交异步) ─────────────────────────

    def submit_buy(self, symbol: str, amount: float) -> dict:
        order = self._new_order(symbol, "buy", amount=amount)
        if symbol not in self._universe:
            self._reject(order, "buy_not_in_universe")
            return order
        if symbol in self._sold_today:
            self._reject(order, "buy_same_day_reentry")
            return order
        if symbol in self.positions:
            self._reject(order, "buy_already_held")
            return order
        self._pending.append(order)
        return order

    def submit_sell(self, symbol: str, shares: float | None) -> dict:
        order = self._new_order(symbol, "sell", shares=shares)
        pos = self.positions.get(symbol)
        if pos is None:
            self._reject(order, "sell_no_position")
            return order
        requested = float(pos["shares"]) if shares is None else float(shares)
        if requested > float(pos["shares"]) + 1e-6:
            self._reject(order, "sell_invalid_shares")
            return order
        if int(pos["entry_bar"]) == self._day_index:
            self._reject(order, "sell_t_plus_one")
            return order
        order["requested"] = requested
        self._pending.append(order)
        return order

    # ── 成交阶段 ──────────────────────────────────────────────

    def apply_risk_exits(
        self,
        bars: dict[str, DayBar],
        exit_info: Callable[[str], tuple[DayBar, float] | None],
    ) -> None:
        """按插入顺序评估持仓退出: pending > 风控 > 到期。

        exit_info(symbol) 由引擎按 exit_fill 口径给出 (fill bar, fill price);
        返回 None 表示该持仓当前无可用成交 bar, 保持现状。
        """
        for symbol in list(self.positions):
            pos = self.positions.get(symbol)
            if pos is None:
                continue
            bar = bars.get(symbol)
            if bar is None:
                continue
            info = exit_info(symbol)
            if info is None:
                continue
            exit_bar, exit_price = info

            if pos.get("pending_exit_reason"):
                reason = str(pos["pending_exit_reason"])
                signal_date = str(pos.get("pending_exit_signal_date") or self._day_str)
                self._try_close(symbol, reason, signal_date, exit_bar, exit_price)
                continue
            if (
                int(pos["entry_bar"]) == self._day_index
                or bar.suspended
                or float(pos["entry_price"]) <= 0
            ):
                continue

            trigger = self._risk_trigger(pos, bar)
            if trigger is not None:
                reason, override = trigger
                self._try_close(symbol, reason, self._day_str, exit_bar, exit_price, override)
                continue
            max_hold = self.config.max_hold_days
            if max_hold is not None and int(pos["hold_days"]) >= int(max_hold):
                self._try_close(symbol, "max_hold", self._day_str, exit_bar, exit_price)

    def process_pending_orders(
        self,
        fill_info: Callable[[dict], tuple[DayBar, float] | None],
    ) -> None:
        """处理挂单: 卖出先于买入, 同侧按提交顺序。

        fill_info(order) 由引擎按 entry/exit_fill 口径给出 (fill bar, fill price);
        返回 None 表示尚无下一根K, 订单保持 pending 到下个交易日。
        """
        sells = [order for order in self._pending if order["side"] == "sell"]
        buys = [order for order in self._pending if order["side"] == "buy"]
        for order in sells + buys:
            order["attempts"] = int(order.get("attempts", 0)) + 1
            info = fill_info(order)
            if info is None:
                continue
            fill_bar, fill_price = info
            if order["side"] == "sell":
                self._fill_sell(order, fill_bar, fill_price)
            else:
                self._fill_buy(order, fill_bar, fill_price)

    def finalize(self, last_bars: dict[str, DayBar]) -> None:
        """期末强平 (reason="end"); 未成交挂单置 expired。"""
        for order in self._pending:
            order["status"] = "expired"
            self._count("pending_expired")
        self._pending = []
        for symbol in list(self.positions):
            bar = last_bars.get(symbol)
            if bar is None:
                continue
            self._try_close(symbol, "end", self._day_str, bar, float(bar.close))

    # ── 只读视图 ──────────────────────────────────────────────

    def market_value(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            mark = self._last_close.get(str(pos["symbol"]), pos["entry_price"])
            if not _valid_price(mark):
                mark = pos["entry_price"]
            total += float(pos["shares"]) * float(mark)
        return total

    def total_value(self) -> float:
        return self.cash + self.market_value()

    def positions_frame(self) -> pl.DataFrame:
        rows = []
        for pos in self.positions.values():
            mark = self._last_close.get(str(pos["symbol"]), pos["entry_price"])
            if not _valid_price(mark):
                mark = pos["entry_price"]
            shares = float(pos["shares"])
            entry_price = float(pos["entry_price"])
            market_value = shares * float(mark)
            entry_value = float(pos["entry_value"])
            rows.append(
                {
                    "symbol": pos["symbol"],
                    "name": pos.get("name", ""),
                    "entry_date": pos["entry_date"],
                    "entry_price": entry_price,
                    "shares": shares,
                    "market_value": round(market_value, 2),
                    "unrealized_pnl_pct": round((market_value - entry_value) / entry_value, 6)
                    if entry_value > 0
                    else 0.0,
                }
            )
        return (
            pl.DataFrame(
                rows,
                schema={
                    "symbol": pl.Utf8,
                    "name": pl.Utf8,
                    "entry_date": pl.Utf8,
                    "entry_price": pl.Float64,
                    "shares": pl.Float64,
                    "market_value": pl.Float64,
                    "unrealized_pnl_pct": pl.Float64,
                },
            )
            if rows
            else pl.DataFrame(
                schema={
                    "symbol": pl.Utf8,
                    "name": pl.Utf8,
                    "entry_date": pl.Utf8,
                    "entry_price": pl.Float64,
                    "shares": pl.Float64,
                    "market_value": pl.Float64,
                    "unrealized_pnl_pct": pl.Float64,
                }
            )
        )

    def reject_counts(self) -> dict[str, int]:
        return dict(self._reject_counts)

    # ── 内部: 订单簿 ───────────────────────────────────────────

    def _new_order(
        self, symbol: str, side: str, *, amount: float = 0.0, shares: float | None = None
    ) -> dict:
        order = {
            "symbol": symbol,
            "side": side,
            "requested": amount if side == "buy" else shares,
            "filled": 0.0,
            "fill_price": None,
            "fill_day": None,
            "status": "submitted",
            "reject_reason": "",
            "day": self._day_str,
            "attempts": 0,
        }
        self.orders_today.append(order)
        return order

    def _reject(self, order: dict, reason: str) -> None:
        order["status"] = "rejected"
        order["reject_reason"] = reason
        self._count(reason)

    def _count(self, reason: str) -> None:
        self._reject_counts[reason] = self._reject_counts.get(reason, 0) + 1

    # ── 内部: 买入 ─────────────────────────────────────────────

    def _fill_buy(self, order: dict, bar: DayBar, fill_price: float) -> None:
        symbol = order["symbol"]
        if symbol in self.positions:
            self._finalize_reject(order, "buy_already_held")
            return
        if symbol in self._sold_today:
            self._finalize_reject(order, "buy_same_day_reentry")
            return
        ok, blocked = can_buy(
            suspended=bar.suspended,
            fill_price=fill_price,
            open_=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            limit_up=bar.limit_up,
            limit_down=bar.limit_down,
        )
        if not ok:
            self._mark_pending(order, blocked)
            return
        if len(self.positions) >= self._max_positions:
            self._finalize_reject(order, "buy_no_slot")
            return

        market_before = self.market_value()
        equity_before = self.cash + market_before
        capacity = equity_before * self._max_exposure - market_before
        allocation = min(float(order["requested"]), self.cash, capacity)
        if equity_before <= 0 or capacity <= 0 or self._max_exposure <= 0:
            self._finalize_reject(order, "buy_exposure")
            return
        shares = float(int(allocation / (fill_price * (1 + self._buy_cost)) / 100) * 100)
        entry_value = shares * fill_price * (1 + self._buy_cost)
        if shares <= 0:
            self._finalize_reject(order, "buy_lot_size")
            return
        if entry_value > self.cash + 1e-6:
            self._finalize_reject(order, "buy_cash")
            return
        if entry_value > capacity + 1e-6:
            self._finalize_reject(order, "buy_exposure")
            return

        self.cash -= entry_value
        self.positions[symbol] = {
            "symbol": symbol,
            "name": bar.name,
            "entry_bar": self._day_index,
            "entry_date": self._day_str,
            "entry_price": fill_price,
            "entry_value": entry_value,
            "shares": shares,
            "lots": shares / 100,
            "position_pct": entry_value / equity_before if equity_before > 0 else 0.0,
            "entry_score": None,
            "max_high": fill_price,
            "hold_days": 0,
            "pending_exit_reason": None,
            "pending_exit_signal_date": None,
            "pending_exit_orders": [],
            "blocked_exit_days": 0,
        }
        order["status"] = "filled"
        order["filled"] = shares
        order["fill_price"] = fill_price
        order["fill_day"] = self._day_str
        self._complete(order)

    # ── 内部: 卖出 ─────────────────────────────────────────────

    def _fill_sell(self, order: dict, bar: DayBar, fill_price: float) -> None:
        symbol = order["symbol"]
        pos = self.positions.get(symbol)
        if pos is None:
            self._finalize_reject(order, "sell_no_position")
            return
        ok, blocked = can_sell(
            suspended=bar.suspended,
            fill_price=fill_price,
            open_=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            limit_up=bar.limit_up,
            limit_down=bar.limit_down,
        )
        if not ok:
            # 挂单通道: 脚本卖单受阻即成为该仓位的 pending 退出 (优先级高于风控),
            # 与 _try_close 的挂单语义同构; 每个受阻交易日只计一次。
            pos.setdefault("pending_exit_orders", []).append(order)
            if not pos.get("pending_exit_reason"):
                pos["pending_exit_reason"] = _SCRIPT_SELL_REASON
                pos["pending_exit_signal_date"] = self._day_str
                self._count("pending_exit")
                pos["blocked_exit_days"] = int(pos.get("blocked_exit_days", 0)) + 1
                self._mark_pending(order, blocked)
            else:
                order["status"] = "pending"
                order["reject_reason"] = blocked
            return
        requested = order["requested"]
        sold = float(pos["shares"]) if requested is None else float(requested)
        self._close_position(
            symbol, _SCRIPT_SELL_REASON, self._day_str, fill_price, sell_shares=sold
        )
        order["status"] = "filled"
        order["filled"] = sold
        order["fill_price"] = fill_price
        order["fill_day"] = self._day_str
        self._complete(order)

    def _try_close(
        self,
        symbol: str,
        reason: str,
        signal_date: str,
        bar: DayBar,
        fill_price: float,
        override: float | None = None,
    ) -> bool:
        price = float(override) if override is not None else float(fill_price)
        ok, blocked = can_sell(
            suspended=bar.suspended,
            fill_price=price,
            open_=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            limit_up=bar.limit_up,
            limit_down=bar.limit_down,
        )
        if not ok:
            pos = self.positions.get(symbol)
            if pos is not None:
                if not pos.get("pending_exit_reason"):
                    pos["pending_exit_reason"] = reason
                    pos["pending_exit_signal_date"] = signal_date
                    self._count("pending_exit")
                pos["blocked_exit_days"] = int(pos.get("blocked_exit_days", 0)) + 1
                self._count(blocked)
            return False
        self._close_position(symbol, reason, signal_date, price)
        return True

    def _close_position(
        self,
        symbol: str,
        reason: str,
        signal_date: str,
        exit_price: float,
        *,
        sell_shares: float | None = None,
    ) -> None:
        pos = self.positions[symbol]
        held = float(pos["shares"])
        sold = held if sell_shares is None else min(float(sell_shares), held)
        exit_value = sold * exit_price * (1 - self._sell_cost)
        cost_basis = float(pos["entry_value"]) * (sold / held) if held > 0 else 0.0
        self.cash += exit_value
        pnl_amount = exit_value - cost_basis
        pnl_pct = pnl_amount / cost_basis if cost_basis > 0 else 0.0
        if sell_shares is None or sold >= held - 1e-6:
            self.positions.pop(symbol)
            # 挂单通道平仓: 联动结掉挂在本仓位上的脚本卖单 (挂单语义 = 同一卖出意图)。
            linked_ids = {id(order) for order in (pos.get("pending_exit_orders") or [])}
            for pending in list(self._pending):
                if id(pending) in linked_ids:
                    pending["status"] = "filled"
                    pending["filled"] = float(pending.get("requested") or sold)
                    pending["fill_price"] = float(exit_price)
                    pending["fill_day"] = self._day_str
                    self._complete(pending)
        else:
            # 部分卖出: 剩余仓位按比例缩减成本/股数, 持仓与挂单状态保留。
            pos["shares"] = held - sold
            pos["entry_value"] = float(pos["entry_value"]) - cost_basis
            pos["lots"] = pos["shares"] / 100
        self._sold_today.add(symbol)
        self.trades.append(
            TradeRecord(
                symbol=symbol,
                name=str(pos.get("name", "")),
                entry_date=pos["entry_date"],
                exit_date=self._day_str,
                entry_price=round(float(pos["entry_price"]), 4),
                exit_price=round(float(exit_price), 4),
                pnl_pct=round(float(pnl_pct), 6),
                duration=int(pos["hold_days"]),
                exit_reason=reason,
                shares=round(float(sold), 4),
                lots=round(float(sold / 100), 2),
                position_pct=round(float(pos.get("position_pct", 0.0)), 6),
                entry_value=round(float(cost_basis), 2),
                exit_value=round(float(exit_value), 2),
                pnl_amount=round(float(pnl_amount), 2),
                entry_score=None,
                entry_signal_date=None,
                exit_signal_date=signal_date,
                blocked_exit_days=int(pos.get("blocked_exit_days", 0)),
                entry_signal_id=None,
                exit_signal_id=None,
            )
        )

    def _risk_trigger(self, pos: dict, bar: DayBar) -> tuple[str, float] | None:
        """镜像矩阵组合撮合的风控触发线; 返回 (reason, override_price) 或 None。"""
        entry_price = float(pos["entry_price"])
        open_price = float(bar.open)
        low_price = float(bar.low)
        high_price = float(bar.high)
        peak_price = float(pos["max_high"])
        risk_lines: list[tuple[float, str]] = []
        config = self.config
        if config.stop_loss_pct is not None:
            risk_lines.append((entry_price * (1 - abs(config.stop_loss_pct)), "stop_loss"))
        if config.trailing_stop_pct is not None:
            risk_lines.append((peak_price * (1 - abs(config.trailing_stop_pct)), "trailing_stop"))
        activate = config.trailing_take_profit_activate_pct
        drawdown = config.trailing_take_profit_drawdown_pct
        if (
            activate is not None
            and drawdown is not None
            and peak_price > entry_price
            and peak_price / entry_price - 1 >= abs(float(activate))
        ):
            risk_lines.append((peak_price * (1 - abs(float(drawdown))), "trailing_take_profit"))
        valid_lines = [(line, reason) for line, reason in risk_lines if _valid_price(line)]
        if valid_lines:
            stop_price, reason = max(valid_lines, key=lambda item: item[0])
            if _valid_price(open_price) and open_price <= stop_price:
                return reason, open_price
            if _valid_price(low_price) and low_price <= stop_price:
                return reason, stop_price
        if config.take_profit_pct is not None:
            take_profit = entry_price * (1 + abs(float(config.take_profit_pct)))
            if _valid_price(take_profit):
                if _valid_price(open_price) and open_price >= take_profit:
                    return "take_profit", open_price
                if _valid_price(high_price) and high_price >= take_profit:
                    return "take_profit", take_profit
        return None

    def _mark_pending(self, order: dict, blocked: str) -> None:
        order["status"] = "pending"
        order["reject_reason"] = blocked
        self._count(blocked)

    def _finalize_reject(self, order: dict, reason: str) -> None:
        order["status"] = "rejected"
        order["reject_reason"] = reason
        self._count(reason)
        self._complete(order)

    def _complete(self, order: dict) -> None:
        """订单进入终态 (filled/rejected/expired): 从挂单队列摘除。"""
        with suppress(ValueError):
            self._pending.remove(order)


def _valid_price(value) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v > 0 and v == v and v not in (float("inf"), float("-inf"))
