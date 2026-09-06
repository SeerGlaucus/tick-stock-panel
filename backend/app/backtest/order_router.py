"""共享订单路由 - 交易可行性判定(停牌/涨跌停/价格有效性)。

矩阵撮合与面板撮合共用同一份判定规则, 杜绝两套口径漂移。

设计说明(Phase 0 重构):
- 费用/滑点模型不复制到本模块: 单一来源是 MatcherConfig.buy_cost_pct /
  sell_cost_pct, 所有撮合路径已共用同一 config 对象。
- 停牌判定保留两条路径的既有差异: 矩阵路径使用预计算的 matrix.tradable
  (零量或 NaN 量且一字价视为停牌), 面板路径使用 is_suspended_bar
  (零量且一字价视为停牌, NaN 量不判停牌)。共享的是判定后的下单规则。
"""

from __future__ import annotations

import numpy as np

# 一字板价差容差: 与既有实现一致 (max(|close| * 1e-4, 0.01))。
_PRICE_TOLERANCE = 1e-4
_MIN_SPREAD = 0.01


def valid_price(value) -> bool:
    """价格有效: 可转 float、大于 0 且有限。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return bool(v > 0 and np.isfinite(v))


def is_suspended_bar(
    open_,
    high,
    low,
    close,
    *,
    volume=0.0,
    has_volume: bool = False,
) -> bool:
    """面板路径停牌判定: 无有效价视为停牌; 零成交且全价同值视为停牌。

    NaN 量不判停牌 (float(nan or 0) <= 0 为 False), 与既有面板路径一致。
    """
    o = float(open_)
    h = float(high)
    low_v = float(low)
    c = float(close)
    if not any(valid_price(x) for x in (o, h, low_v, c)):
        return True
    if has_volume and float(volume or 0) <= 0:
        same_price = max(o, h, low_v, c) - min(o, h, low_v, c) <= max(
            abs(c) * _PRICE_TOLERANCE, _MIN_SPREAD
        )
        if same_price:
            return True
    return False


def one_price_limit(
    open_,
    high,
    low,
    close,
    *,
    direction: str,
    limit_up: bool = False,
    limit_down: bool = False,
    suspended: bool = False,
) -> bool:
    """一字涨停/跌停封死判定。suspended 为 True 时直接返回 False。"""
    if suspended:
        return False
    prices = (float(open_), float(high), float(low), float(close))
    if not all(valid_price(p) for p in prices):
        return False
    same_price = max(prices) - min(prices) <= max(abs(prices[3]) * _PRICE_TOLERANCE, _MIN_SPREAD)
    flag = limit_up if direction == "up" else limit_down
    return bool(flag) and same_price


def can_buy(
    *,
    suspended: bool,
    fill_price,
    open_,
    high,
    low,
    close,
    limit_up: bool = False,
    limit_down: bool = False,
) -> tuple[bool, str]:
    """买入可行性: (可买, 拒绝原因)。原因取值与既有 execution_stats 键一致。"""
    if suspended:
        return False, "buy_suspended"
    if not valid_price(fill_price):
        return False, "buy_invalid_price"
    if one_price_limit(
        open_,
        high,
        low,
        close,
        direction="up",
        limit_up=limit_up,
        limit_down=limit_down,
        suspended=suspended,
    ):
        return False, "buy_limit_up"
    return True, ""


def can_sell(
    *,
    suspended: bool,
    fill_price,
    open_,
    high,
    low,
    close,
    limit_up: bool = False,
    limit_down: bool = False,
) -> tuple[bool, str]:
    """卖出可行性: (可卖, 拒绝原因)。原因取值与既有 execution_stats 键一致。"""
    if suspended:
        return False, "sell_suspended"
    if not valid_price(fill_price):
        return False, "sell_invalid_price"
    if one_price_limit(
        open_,
        high,
        low,
        close,
        direction="down",
        limit_up=limit_up,
        limit_down=limit_down,
        suspended=suspended,
    ):
        return False, "sell_limit_down"
    return True, ""
