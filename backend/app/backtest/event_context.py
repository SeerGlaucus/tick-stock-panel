"""事件驱动回测的脚本上下文 (日频)。

只提供回测脚本运行期视图, 不参与数据加载与撮合。防未来函数契约:

- before_trading_start: history() 仅含 <=T-1 完成态日K; universe = T-1 候选面板。
- handle_data / after_trading_end: history() 含 T; universe = T 候选面板。
- history(n) 上限 META["history_bars"], 超限运行时报错。
- universe/history 的列由特征计划 (REQUIRED_FEATURES + 基础列) 决定,
  本模块不额外过滤, 未声明列天然不在面板内 (fail-closed 由加载链路保证)。

组合视图与下单走注入的 portfolio 对象 (见 EventPortfolio 的公开方法):

- .cash (float) / .total_value() (float) / .positions_frame() (pl.DataFrame)
- .orders_today (list[dict]) / .submit_buy(symbol, amount) / .submit_sell(symbol, shares)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import polars as pl

EventPhase = Literal["before_trading_start", "handle_data", "after_trading_end"]


@dataclass
class EventContext:
    """单次回测 run 内的脚本上下文; 每个 run / 优化 trial 独立实例。"""

    params: dict
    asset_type: str
    panel: pl.DataFrame  # 全窗口面板, 按 symbol,date 排序; 列 = 特征计划输出
    dates: list[date]  # 交易日序列 (升序, 面板 date 列的 unique 排序值)
    day_index: int  # 当前 T 在 dates 中的索引
    phase: EventPhase
    history_bars: int
    portfolio: Any  # EventPortfolio (只读组合视图 + 下单入口)
    state: dict = field(default_factory=dict)  # 脚本自由状态, 跨时相共享

    # ── 配置与时间 (只读) ──────────────────────────────────────

    @property
    def current_date(self) -> date:
        return self.dates[self.day_index]

    @property
    def previous_date(self) -> date | None:
        if self.day_index <= 0:
            return None
        return self.dates[self.day_index - 1]

    # ── 数据视图 (只读, 防未来函数) ──────────────────────────────

    def _visible_end(self) -> int:
        """当前时相可见的最后一个完成交易日索引。"""
        if self.phase == "before_trading_start":
            return self.day_index - 1
        return self.day_index

    @property
    def universe(self) -> pl.DataFrame:
        """当前时相可见的最后一个完成交易日的候选面板, 一行一标的。"""
        end = self._visible_end()
        if end < 0:
            return self.panel.clear()
        return self.panel.filter(pl.col("date") == self.dates[end])

    def history(self, n: int) -> pl.DataFrame:
        """最近 n 个完成交易日窗口 (按 symbol,date 排序); 是否含 T 由时相决定。"""
        if not isinstance(n, int) or isinstance(n, bool):
            raise ValueError(f"history(n) 的 n 必须是整数: {n!r}")
        if not 1 <= n <= self.history_bars:
            raise ValueError(
                f"history(n) 的 n 必须在 [1, {self.history_bars}] 内 (META history_bars): {n}"
            )
        end = self._visible_end()
        start = max(0, end - n + 1)
        if end < 0:
            return self.panel.clear()
        return self.panel.filter(pl.col("date").is_in(self.dates[start : end + 1]))

    # ── 下单 API (成交异步, 结果见 orders_today) ─────────────────

    def order_buy(self, symbol: str, amount: float) -> None:
        """按金额买入; 引擎向下取整手 (100 股), 零头回现金。"""
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"order_buy 的 symbol 必须是非空字符串: {symbol!r}")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError(f"order_buy 的 amount 必须是数字: {amount!r}")
        value = float(amount)
        if not value > 0 or value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"order_buy 的 amount 必须是有限正数: {amount!r}")
        self.portfolio.submit_buy(symbol, value)

    def order_sell(self, symbol: str, shares: float | None = None) -> None:
        """卖出; shares=None 表示清仓。"""
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"order_sell 的 symbol 必须是非空字符串: {symbol!r}")
        if shares is not None:
            if isinstance(shares, bool) or not isinstance(shares, (int, float)):
                raise ValueError(f"order_sell 的 shares 必须是数字: {shares!r}")
            value = float(shares)
            if not value > 0 or value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"order_sell 的 shares 必须是有限正数: {shares!r}")
            shares = value
        self.portfolio.submit_sell(symbol, shares)

    # ── 组合状态 (只读视图) ─────────────────────────────────────

    @property
    def cash(self) -> float:
        return float(self.portfolio.cash)

    @property
    def total_value(self) -> float:
        return float(self.portfolio.total_value())

    @property
    def positions(self) -> pl.DataFrame:
        return self.portfolio.positions_frame()

    @property
    def orders_today(self) -> list[dict]:
        return [dict(order) for order in self.portfolio.orders_today]
