from __future__ import annotations

import math

import numpy as np
import pytest

from app.backtest.order_router import (
    can_buy,
    can_sell,
    is_suspended_bar,
    one_price_limit,
    valid_price,
)


class TestValidPrice:
    @pytest.mark.parametrize(
        "value",
        [None, "abc", math.nan, math.inf, -math.inf, 0, 0.0, -1.0, np.nan],
    )
    def test_invalid(self, value):
        assert not valid_price(value)

    @pytest.mark.parametrize(
        "value",
        [0.01, 10, 10.0, "10", np.float32(3.5), np.float64(2.0)],
    )
    def test_valid(self, value):
        assert valid_price(value)


class TestIsSuspendedBar:
    def test_no_valid_price_is_suspended(self):
        assert is_suspended_bar(0, 0, 0, 0, volume=0, has_volume=True)
        assert is_suspended_bar(np.nan, np.nan, np.nan, np.nan, volume=100, has_volume=True)

    def test_zero_volume_same_price_suspended(self):
        assert is_suspended_bar(10, 10, 10, 10, volume=0, has_volume=True)

    def test_zero_volume_with_spread_not_suspended(self):
        assert not is_suspended_bar(10, 11, 9.5, 10.2, volume=0, has_volume=True)

    def test_nan_volume_does_not_suspend(self):
        # 面板路径语义: float(nan or 0) -> nan, nan <= 0 为 False。
        assert not is_suspended_bar(10, 10, 10, 10, volume=np.nan, has_volume=True)

    def test_no_volume_column_ignores_volume(self):
        assert not is_suspended_bar(10, 10, 10, 10, volume=0, has_volume=False)


class TestOnePriceLimit:
    def test_suspended_returns_false(self):
        assert not one_price_limit(11, 11, 11, 11, direction="up", limit_up=True, suspended=True)

    def test_invalid_prices_return_false(self):
        assert not one_price_limit(0, 11, 11, 11, direction="up", limit_up=True)

    def test_up_flag_and_same_price(self):
        assert one_price_limit(11, 11, 11, 11, direction="up", limit_up=True, limit_down=False)

    def test_down_flag_and_same_price(self):
        assert one_price_limit(9, 9, 9, 9, direction="down", limit_up=False, limit_down=True)

    def test_direction_uses_matching_flag_only(self):
        # 一字价 + 跌停标志, 但方向为 up: limit_up 为 False → 不判定封板。
        assert not one_price_limit(11, 11, 11, 11, direction="up", limit_up=False, limit_down=True)

    def test_spread_not_same_price(self):
        assert not one_price_limit(11, 11.2, 10.9, 11.1, direction="up", limit_up=True)


class TestCanBuy:
    def test_suspended(self):
        ok, reason = can_buy(
            suspended=True,
            fill_price=10,
            open_=10,
            high=10,
            low=10,
            close=10,
        )
        assert (ok, reason) == (False, "buy_suspended")

    def test_invalid_fill(self):
        ok, reason = can_buy(
            suspended=False,
            fill_price=np.nan,
            open_=10,
            high=10,
            low=10,
            close=10,
        )
        assert (ok, reason) == (False, "buy_invalid_price")

    def test_limit_up(self):
        ok, reason = can_buy(
            suspended=False,
            fill_price=11,
            open_=11,
            high=11,
            low=11,
            close=11,
            limit_up=True,
            limit_down=False,
        )
        assert (ok, reason) == (False, "buy_limit_up")

    def test_ok(self):
        ok, reason = can_buy(
            suspended=False,
            fill_price=10,
            open_=10,
            high=10.5,
            low=9.8,
            close=10.2,
            limit_up=False,
            limit_down=False,
        )
        assert (ok, reason) == (True, "")


class TestCanSell:
    def test_suspended(self):
        ok, reason = can_sell(
            suspended=True,
            fill_price=10,
            open_=10,
            high=10,
            low=10,
            close=10,
        )
        assert (ok, reason) == (False, "sell_suspended")

    def test_invalid_fill(self):
        ok, reason = can_sell(
            suspended=False,
            fill_price=0,
            open_=10,
            high=10,
            low=10,
            close=10,
        )
        assert (ok, reason) == (False, "sell_invalid_price")

    def test_limit_down(self):
        ok, reason = can_sell(
            suspended=False,
            fill_price=9,
            open_=9,
            high=9,
            low=9,
            close=9,
            limit_up=False,
            limit_down=True,
        )
        assert (ok, reason) == (False, "sell_limit_down")

    def test_ok(self):
        ok, reason = can_sell(
            suspended=False,
            fill_price=10.2,
            open_=10,
            high=10.5,
            low=9.8,
            close=10.2,
            limit_up=False,
            limit_down=False,
        )
        assert (ok, reason) == (True, "")
