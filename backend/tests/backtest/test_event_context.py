from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.backtest.event_context import EventContext


def _panel(symbols: list[str], days: int = 3) -> pl.DataFrame:
    rows = []
    for sym in symbols:
        for i in range(days):
            rows.append(
                {
                    "symbol": sym,
                    "date": date(2024, 1, 1 + i),
                    "name": sym,
                    "close": 10.0 + i,
                }
            )
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _dates(days: int = 3) -> list[date]:
    return [date(2024, 1, 1 + i) for i in range(days)]


class FakePortfolio:
    def __init__(self) -> None:
        self.cash = 123.0
        self.submitted: list[tuple] = []
        self.orders_today = [{"symbol": "A", "status": "filled"}]

    def total_value(self) -> float:
        return 456.0

    def positions_frame(self) -> pl.DataFrame:
        return pl.DataFrame({"symbol": ["A"]})

    def submit_buy(self, symbol: str, amount: float) -> None:
        self.submitted.append(("buy", symbol, amount))

    def submit_sell(self, symbol: str, shares: float | None) -> None:
        self.submitted.append(("sell", symbol, shares))


def _context(
    panel: pl.DataFrame,
    *,
    phase: str = "handle_data",
    day_index: int = 1,
    history_bars: int = 60,
    portfolio=None,
) -> EventContext:
    return EventContext(
        params={"lookback": 5},
        asset_type="stock",
        panel=panel,
        dates=_dates(),
        day_index=day_index,
        phase=phase,  # type: ignore[arg-type]
        history_bars=history_bars,
        portfolio=portfolio or FakePortfolio(),
    )


def test_current_and_previous_date():
    context = _context(_panel(["A"]), day_index=1)
    assert context.current_date == date(2024, 1, 2)
    assert context.previous_date == date(2024, 1, 1)

    first = _context(_panel(["A"]), day_index=0)
    assert first.previous_date is None


def test_before_phase_universe_is_previous_day():
    context = _context(_panel(["A", "B"]), phase="before_trading_start", day_index=1)
    universe = context.universe
    assert universe["date"].unique().to_list() == [date(2024, 1, 1)]
    assert sorted(universe["symbol"].to_list()) == ["A", "B"]


def test_handle_phase_universe_is_current_day():
    context = _context(_panel(["A", "B"]), phase="handle_data", day_index=1)
    assert context.universe["date"].unique().to_list() == [date(2024, 1, 2)]


def test_before_phase_history_excludes_current_day():
    context = _context(_panel(["A"]), phase="before_trading_start", day_index=2)
    history = context.history(2)
    assert history["date"].unique().sort().to_list() == [
        date(2024, 1, 1),
        date(2024, 1, 2),
    ]


def test_handle_phase_history_includes_current_day():
    context = _context(_panel(["A"]), phase="handle_data", day_index=2)
    history = context.history(2)
    assert history["date"].unique().sort().to_list() == [
        date(2024, 1, 2),
        date(2024, 1, 3),
    ]


def test_first_day_before_phase_has_empty_views():
    panel = _panel(["A"])
    context = _context(panel, phase="before_trading_start", day_index=0)
    assert context.universe.is_empty()
    assert context.universe.columns == panel.columns
    assert context.history(3).is_empty()


def test_history_rejects_out_of_range():
    context = _context(_panel(["A"]), history_bars=10)
    for bad in (0, -1, 11, True, 2.5):
        with pytest.raises(ValueError, match="history"):
            context.history(bad)  # type: ignore[arg-type]


def test_order_buy_delegates_and_validates():
    portfolio = FakePortfolio()
    context = _context(_panel(["A"]), portfolio=portfolio)

    context.order_buy("600000.SH", 1000)
    assert portfolio.submitted == [("buy", "600000.SH", 1000.0)]

    for bad in (0, -1, float("nan"), float("inf"), "100", None):
        with pytest.raises(ValueError, match="amount"):
            context.order_buy("600000.SH", bad)
    with pytest.raises(ValueError, match="symbol"):
        context.order_buy("", 1000)


def test_order_sell_delegates_and_validates():
    portfolio = FakePortfolio()
    context = _context(_panel(["A"]), portfolio=portfolio)

    context.order_sell("600000.SH")
    context.order_sell("600000.SH", 100)
    assert portfolio.submitted == [
        ("sell", "600000.SH", None),
        ("sell", "600000.SH", 100.0),
    ]

    for bad in (0, -1, float("nan"), float("inf"), "100"):
        with pytest.raises(ValueError, match="shares"):
            context.order_sell("600000.SH", bad)
    with pytest.raises(ValueError, match="symbol"):
        context.order_sell("", 100)


def test_portfolio_views_delegate():
    context = _context(_panel(["A"]))
    assert context.cash == 123.0
    assert context.total_value == 456.0
    assert context.positions["symbol"].to_list() == ["A"]
    assert context.orders_today == [{"symbol": "A", "status": "filled"}]


def test_orders_rejected_outside_handle_data_phase():
    portfolio = FakePortfolio()
    for phase in ("before_trading_start", "after_trading_end"):
        context = _context(_panel(["A"]), phase=phase, portfolio=portfolio)
        with pytest.raises(ValueError, match="handle_data"):
            context.order_buy("600000.SH", 1000)
        with pytest.raises(ValueError, match="handle_data"):
            context.order_sell("600000.SH")
    assert portfolio.submitted == []


def test_state_is_shared_mutable_dict():
    context = _context(_panel(["A"]))
    assert context.state == {}
    context.state["count"] = 1
    assert context.state["count"] == 1
