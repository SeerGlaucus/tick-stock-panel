from __future__ import annotations

from datetime import date

from app.backtest.engine import MatcherConfig
from app.backtest.event_portfolio import DayBar, EventPortfolio


def _config(**kwargs) -> MatcherConfig:
    defaults = dict(
        initial_capital=100_000,
        fees_pct=0,
        slippage_bps=0,
        max_positions=3,
        max_exposure_pct=1.0,
    )
    defaults.update(kwargs)
    return MatcherConfig(**defaults)


def _bar(symbol: str, price: float = 10.0, **patch) -> DayBar:
    fields = dict(
        open=price,
        high=price,
        low=price,
        close=price,
        volume=100_000,
        has_volume=True,
        limit_up=False,
        limit_down=False,
        suspended=False,
        name=symbol,
    )
    fields.update(patch)
    return DayBar(symbol=symbol, **fields)


def _close_fill(bars: dict[str, DayBar]):
    """close_t 口径: 订单用当日 bar 的收盘价成交。"""

    def fill_info(order):
        bar = bars.get(order["symbol"])
        if bar is None:
            return None
        return bar, float(bar.close)

    return fill_info


def _exit_info(bars: dict[str, DayBar]):
    """风控退出用当日 bar 的收盘价。"""

    def exit_info(symbol):
        bar = bars.get(symbol)
        if bar is None:
            return None
        return bar, float(bar.close)

    return exit_info


def _run_day(
    pf: EventPortfolio, day_index: int, day: date, universe: set[str], bars: dict[str, DayBar]
) -> None:
    pf.begin_day(day, day_index, universe)
    pf.apply_risk_exits(bars, _exit_info(bars))
    pf.process_pending_orders(_close_fill(bars))
    pf.update_marks(bars)


# ── 买入 ────────────────────────────────────────────────────────


def test_buy_fills_at_close_with_lot_rounding():
    pf = EventPortfolio(_config())
    bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    order = pf.submit_buy("A", 1050.0)
    pf.process_pending_orders(_close_fill(bars))

    assert order["status"] == "filled"
    assert order["filled"] == 100  # floor(1050/10/100)*100, 零头回现金
    assert pf.positions["A"]["shares"] == 100
    assert pf.cash == 100_000 - 1000
    assert pf.positions["A"]["entry_bar"] == 0
    assert pf.positions["A"]["hold_days"] == 0


def test_buy_with_commission_rounds_to_zero_rejects_lot_size():
    pf = EventPortfolio(_config(commission_pct=0.0003))
    bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    order = pf.submit_buy("A", 1000.0)
    pf.process_pending_orders(_close_fill(bars))

    assert order["status"] == "rejected"
    assert order["reject_reason"] == "buy_lot_size"
    assert pf.cash == 100_000
    assert pf.reject_counts()["buy_lot_size"] == 1


def test_buy_rejections_not_in_universe_and_already_held():
    pf = EventPortfolio(_config())
    bars = {"A": _bar("A")}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)

    order = pf.submit_buy("B", 1000.0)  # 不在 universe
    assert order["status"] == "rejected"
    assert order["reject_reason"] == "buy_not_in_universe"

    pf.submit_buy("A", 1000.0)
    pf.process_pending_orders(_close_fill(bars))
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, bars)
    again = pf.submit_buy("A", 1000.0)
    assert again["status"] == "rejected"
    assert again["reject_reason"] == "buy_already_held"


def test_buy_same_day_reentry_blocked():
    pf = EventPortfolio(_config())
    bars = {"A": _bar("A")}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    pf.submit_buy("A", 1000.0)
    pf.process_pending_orders(_close_fill(bars))

    _run_day(pf, 1, date(2024, 1, 2), {"A"}, bars)
    pf.submit_sell("A", None)
    pf.process_pending_orders(_close_fill(bars))
    rebuy = pf.submit_buy("A", 1000.0)
    assert rebuy["status"] == "rejected"
    assert rebuy["reject_reason"] == "buy_same_day_reentry"


def test_buy_no_slot_when_positions_full():
    pf = EventPortfolio(_config(max_positions=1))
    bars = {"A": _bar("A"), "B": _bar("B")}
    _run_day(pf, 0, date(2024, 1, 1), {"A", "B"}, bars)
    pf.submit_buy("A", 1000.0)
    pf.submit_buy("B", 1000.0)
    pf.process_pending_orders(_close_fill(bars))

    assert "A" in pf.positions
    assert "B" not in pf.positions
    assert pf.reject_counts()["buy_no_slot"] == 1


def test_buy_exposure_cap_rejects_when_full():
    pf = EventPortfolio(_config(max_exposure_pct=0.5))
    bars = {"A": _bar("A", price=10.0), "B": _bar("B", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A", "B"}, bars)
    pf.submit_buy("A", 50_000.0)
    pf.submit_buy("B", 50_000.0)
    pf.process_pending_orders(_close_fill(bars))

    assert "A" in pf.positions
    assert pf.positions["A"]["shares"] == 5000  # 50000 满额
    assert "B" not in pf.positions
    assert pf.reject_counts()["buy_exposure"] == 1


def test_buy_small_cash_rejects_lot_size():
    pf = EventPortfolio(_config(initial_capital=500))
    bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    order = pf.submit_buy("A", 500.0)
    pf.process_pending_orders(_close_fill(bars))
    assert order["status"] == "rejected"
    assert order["reject_reason"] == "buy_lot_size"


# ── 卖出 ────────────────────────────────────────────────────────


def _buy(
    pf: EventPortfolio,
    symbol: str,
    amount: float,
    bars: dict[str, DayBar],
    day_index: int = 0,
    day: date = date(2024, 1, 1),
) -> None:
    pf.submit_buy(symbol, amount)
    pf.process_pending_orders(_close_fill(bars))


def test_sell_full_and_partial():
    pf = EventPortfolio(_config())
    bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    _buy(pf, "A", 2000.0, bars)

    _run_day(pf, 1, date(2024, 1, 2), {"A"}, bars)
    pf.submit_sell("A", 100.0)
    pf.process_pending_orders(_close_fill(bars))
    assert "A" in pf.positions
    assert pf.positions["A"]["shares"] == 100
    assert len(pf.trades) == 1
    assert pf.trades[0].shares == 100
    assert pf.trades[0].exit_reason == "signal"
    assert pf.cash == 100_000 - 2000 + 1000

    pf.submit_sell("A", None)
    pf.process_pending_orders(_close_fill(bars))
    assert "A" not in pf.positions
    assert len(pf.trades) == 2
    assert pf.cash == 100_000


def test_sell_rejections():
    pf = EventPortfolio(_config())
    bars = {"A": _bar("A")}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    _buy(pf, "A", 1000.0, bars)

    same_day = pf.submit_sell("A", None)  # 当日买入 T+1
    assert same_day["status"] == "rejected"
    assert same_day["reject_reason"] == "sell_t_plus_one"

    too_many = pf.submit_sell("A", 500.0)
    assert too_many["status"] == "rejected"
    assert too_many["reject_reason"] == "sell_invalid_shares"

    missing = pf.submit_sell("B", None)
    assert missing["status"] == "rejected"
    assert missing["reject_reason"] == "sell_no_position"


# ── 挂单生命周期 ────────────────────────────────────────────────


def test_limit_up_buy_pends_and_fills_next_day():
    pf = EventPortfolio(_config())
    day0_bars = {"A": _bar("A", price=11.0, open=11.0, high=11.0, low=11.0, limit_up=True)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0_bars)
    order = pf.submit_buy("A", 1100.0)
    pf.process_pending_orders(_close_fill(day0_bars))
    assert order["status"] == "pending"
    assert "A" not in pf.positions

    day1_bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, day1_bars)
    assert order["status"] == "filled"
    assert order["fill_price"] == 10.0
    assert pf.positions["A"]["shares"] == 100


def test_limit_down_sell_pends_and_fills_next_day():
    pf = EventPortfolio(_config())
    day0_bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0_bars)
    _buy(pf, "A", 1000.0, day0_bars)

    day1_bars = {"A": _bar("A", price=9.0, open=9.0, high=9.0, low=9.0, limit_down=True)}
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, day1_bars)
    order = pf.submit_sell("A", None)
    pf.process_pending_orders(_close_fill(day1_bars))
    assert order["status"] == "pending"
    assert "A" in pf.positions

    day2_bars = {"A": _bar("A", price=9.5)}
    _run_day(pf, 2, date(2024, 1, 3), {"A"}, day2_bars)
    assert order["status"] == "filled"
    assert order["fill_price"] == 9.5
    assert "A" not in pf.positions


def test_finalize_expires_pending_and_closes_positions():
    pf = EventPortfolio(_config())
    bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    pf.submit_buy("A", 1000.0)
    pf.process_pending_orders(_close_fill(bars))
    pf.submit_buy("A", 1000.0)  # already held → rejected (不会进挂单)
    day1 = _bar("A", price=9.0, open=9.0, high=9.0, low=9.0, limit_down=True)
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, {"A": day1})
    pending = pf.submit_sell("A", None)
    pf.process_pending_orders(_close_fill({"A": day1}))

    last = {"A": _bar("A", price=8.5)}
    pf.finalize(last)
    assert pending["status"] == "expired"
    assert "A" not in pf.positions
    assert pf.trades[-1].exit_reason == "end"
    assert pf.reject_counts()["pending_expired"] == 1


# ── 风控与退出优先级 ────────────────────────────────────────────


def test_stop_loss_beats_script_sell_same_day():
    pf = EventPortfolio(_config(stop_loss_pct=0.1))
    day0 = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0)
    _buy(pf, "A", 1000.0, day0)

    day1 = {"A": _bar("A", price=9.0, open=9.0, high=9.0, low=8.5, close=9.0)}
    pf.begin_day(date(2024, 1, 2), 1, {"A"})
    script_sell = pf.submit_sell("A", None)
    pf.apply_risk_exits(day1, _exit_info(day1))
    pf.process_pending_orders(_close_fill(day1))
    pf.update_marks(day1)

    assert len(pf.trades) == 1
    assert pf.trades[0].exit_reason == "stop_loss"
    assert pf.trades[0].exit_price == 9.0
    assert script_sell["status"] == "rejected"
    assert script_sell["reject_reason"] == "sell_no_position"


def test_pending_exit_beats_risk_on_next_day():
    pf = EventPortfolio(_config(stop_loss_pct=0.1))
    day0 = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0)
    _buy(pf, "A", 1000.0, day0)

    # day1: 跌停卖不出 (9.8 未破止损线 9.0) → 挂单 (reason=signal)
    day1 = {"A": _bar("A", price=9.8, open=9.8, high=9.8, low=9.8, limit_down=True)}
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, day1)
    order = pf.submit_sell("A", None)
    pf.process_pending_orders(_close_fill(day1))
    assert order["status"] == "pending"

    # day2: 止损线也触发, 但挂单优先 → 以挂单 reason=signal 平仓
    day2 = {"A": _bar("A", price=8.0, open=8.0, high=8.0, low=8.0, close=8.0)}
    _run_day(pf, 2, date(2024, 1, 3), {"A"}, day2)
    assert order["status"] == "filled"
    assert pf.trades[0].exit_reason == "signal"
    assert pf.trades[0].blocked_exit_days == 1


def test_risk_skipped_on_entry_day():
    pf = EventPortfolio(_config(stop_loss_pct=0.1))
    day0 = {"A": _bar("A", price=10.0, open=8.0, high=8.0, low=8.0, close=8.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0)
    _buy(pf, "A", 1000.0, day0)
    # 同一交易日入场后再评估风控 (顺序外调用): 入场日跳过
    pf.apply_risk_exits(day0, _exit_info(day0))
    assert "A" in pf.positions
    assert pf.trades == []


def test_max_hold_exit():
    pf = EventPortfolio(_config(max_hold_days=2))
    bars = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, bars)
    _buy(pf, "A", 1000.0, bars)
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, bars)
    assert "A" in pf.positions
    _run_day(pf, 2, date(2024, 1, 3), {"A"}, bars)
    assert "A" not in pf.positions
    assert pf.trades[0].exit_reason == "max_hold"
    assert pf.trades[0].duration == 2


def test_trailing_stop_uses_high_water_mark():
    pf = EventPortfolio(_config(trailing_stop_pct=0.05))
    day0 = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0)
    _buy(pf, "A", 1000.0, day0)
    day1 = {"A": _bar("A", price=12.0, open=10.0, high=12.0, low=11.8, close=12.0)}
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, day1)
    assert "A" in pf.positions
    day2 = {"A": _bar("A", price=11.3, open=12.0, high=12.0, low=11.3, close=11.3)}
    _run_day(pf, 2, date(2024, 1, 3), {"A"}, day2)
    assert pf.trades[0].exit_reason == "trailing_stop"
    assert pf.trades[0].exit_price == 11.4  # 12 * 0.95


def test_take_profit_fills_at_open_when_gap_up():
    pf = EventPortfolio(_config(take_profit_pct=0.1))
    day0 = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0)
    _buy(pf, "A", 1000.0, day0)
    day1 = {"A": _bar("A", price=11.2, open=11.2, high=11.5, low=11.0, close=11.2)}
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, day1)
    assert pf.trades[0].exit_reason == "take_profit"
    assert pf.trades[0].exit_price == 11.2


def test_trailing_take_profit_requires_activation_and_triggers():
    pf = EventPortfolio(
        _config(
            trailing_take_profit_activate_pct=0.10,
            trailing_take_profit_drawdown_pct=0.03,
        )
    )
    day0 = {"A": _bar("A", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0)
    _buy(pf, "A", 1000.0, day0)
    day1 = {"A": _bar("A", price=10.8, open=10.0, high=10.8, low=10.4, close=10.8)}
    _run_day(pf, 1, date(2024, 1, 2), {"A"}, day1)
    assert "A" in pf.positions  # 未激活
    day2 = {"A": _bar("A", price=12.0, open=12.0, high=12.0, low=11.9, close=12.0)}
    _run_day(pf, 2, date(2024, 1, 3), {"A"}, day2)
    assert "A" in pf.positions  # 峰值 12 激活, 当日未回撤
    day3 = {"A": _bar("A", price=11.5, open=12.0, high=12.0, low=11.5, close=11.5)}
    _run_day(pf, 3, date(2024, 1, 4), {"A"}, day3)
    assert pf.trades[0].exit_reason == "trailing_take_profit"
    assert pf.trades[0].exit_price == 11.64  # 12 * 0.97


# ── 资金与顺序 ──────────────────────────────────────────────────


def test_sell_proceeds_reusable_same_day_for_buy():
    pf = EventPortfolio(_config(max_positions=1))
    day0 = {"A": _bar("A", price=10.0), "B": _bar("B", price=10.0)}
    _run_day(pf, 0, date(2024, 1, 1), {"A", "B"}, day0)
    _buy(pf, "A", 10_000.0, day0)

    _run_day(pf, 1, date(2024, 1, 2), {"A", "B"}, day0)
    pf.submit_sell("A", None)
    pf.submit_buy("B", 10_000.0)
    pf.process_pending_orders(_close_fill(day0))

    assert "A" not in pf.positions
    assert "B" in pf.positions
    assert pf.cash == 100_000 - 10_000  # 回款复用于当日买入


def test_deterministic_repeat_runs():
    def scenario() -> EventPortfolio:
        pf = EventPortfolio(_config(stop_loss_pct=0.1))
        day0 = {"A": _bar("A", price=10.0), "B": _bar("B", price=12.0)}
        _run_day(pf, 0, date(2024, 1, 1), {"A", "B"}, day0)
        pf.submit_buy("A", 1000.0)
        pf.submit_buy("B", 2400.0)
        pf.process_pending_orders(_close_fill(day0))
        day1 = {
            "A": _bar("A", price=9.0, open=9.0, high=9.0, low=8.5, close=9.0),
            "B": _bar("B", price=12.0),
        }
        _run_day(pf, 1, date(2024, 1, 2), {"A", "B"}, day1)
        pf.finalize(day1)
        return pf

    first, second = scenario(), scenario()
    assert [
        (t.symbol, t.entry_date, t.exit_date, t.exit_price, t.exit_reason, t.shares)
        for t in first.trades
    ] == [
        (t.symbol, t.entry_date, t.exit_date, t.exit_price, t.exit_reason, t.shares)
        for t in second.trades
    ]
    assert first.cash == second.cash
    assert first.reject_counts() == second.reject_counts()


def test_orders_today_records_transitions():
    pf = EventPortfolio(_config())
    day0 = {"A": _bar("A", price=11.0, open=11.0, high=11.0, low=11.0, limit_up=True)}
    _run_day(pf, 0, date(2024, 1, 1), {"A"}, day0)
    order = pf.submit_buy("A", 1100.0)
    pf.process_pending_orders(_close_fill(day0))

    assert order["status"] == "pending"
    assert order["side"] == "buy"
    assert order["requested"] == 1100.0
    assert pf.orders_today == [order]
    assert pf.reject_counts()["buy_limit_up"] == 1
