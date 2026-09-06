from __future__ import annotations

import threading
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import MatcherConfig
from app.backtest.event_engine import EventBacktestEngine
from app.strategy.engine import StrategyDef


def _strategy(
    handle_data,
    *,
    before=None,
    after=None,
    max_hold_days=None,
    stop_loss=None,
    history_bars: int = 60,
) -> StrategyDef:
    return StrategyDef(
        meta={"id": "event_test"},
        basic_filter={},
        entry_signals=[],
        exit_signals=[],
        stop_loss=stop_loss,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=max_hold_days,
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        execution_backend="event",
        handle_data_fn=handle_data,
        before_trading_start_fn=before,
        after_trading_end_fn=after,
        event_history_bars=history_bars,
    )


def _panel(days: list[date], symbols: list[str], price: float = 10.0) -> pl.DataFrame:
    rows = []
    for day in days:
        for symbol in symbols:
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "name": symbol,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 100_000,
                    "signal_limit_up": False,
                    "signal_limit_down": False,
                }
            )
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _matcher(**kwargs) -> MatcherConfig:
    defaults = dict(initial_capital=100_000, fees_pct=0, slippage_bps=0)
    defaults.update(kwargs)
    return MatcherConfig(**defaults)


def _dates(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def test_script_phases_run_in_order_with_warmup_history():
    warmup = _dates(3, date(2023, 12, 29))
    formal = _dates(3)
    start = formal[0]
    panel = _panel(warmup + formal, ["A"])

    def handle(context):
        context.state.setdefault("diary", []).append(
            (
                "handle",
                context.current_date,
                context.history(2)["date"].unique().sort().to_list(),
            )
        )

    def before(context):
        context.state.setdefault("diary", []).append(
            (
                "before",
                context.current_date,
                context.universe["date"].unique().to_list(),
            )
        )

    def after(context):
        context.state.setdefault("diary", []).append(("after", context.current_date))

    strategy = _strategy(handle, before=before, after=after)
    engine = EventBacktestEngine()
    result = engine.run(
        strategy,
        panel,
        {},
        _matcher(),
        start=start,
    )

    diary = result.state["diary"]
    # 每个正式交易日: before -> handle -> after; warmup 不进脚本时相。
    assert [item[0] for item in diary] == ["before", "handle", "after"] * 3
    # 首个正式日的 handle 时相 history(2) = 一个 warmup 日 + 当日。
    assert diary[1][2] == [date(2023, 12, 31), date(2024, 1, 1)]
    # before 时相 universe = T-1 候选面板。
    assert diary[0][2] == [date(2023, 12, 31)]


def test_buy_close_t_fills_same_day_close():
    days = _dates(3)
    start = days[0]

    def handle(context):
        if context.current_date == start:
            context.order_buy("A", 1000.0)

    result = EventBacktestEngine().run(
        _strategy(handle),
        _panel(days, ["A"]),
        {},
        _matcher(matching="close_t"),
        start=start,
    )

    assert len(result.trades) == 1
    assert result.trades[0].entry_date == start.isoformat()
    assert result.trades[0].entry_price == 10.0
    assert result.trades[0].exit_reason == "end"
    assert result.equity_curve[0]["cash"] == 99_000.0


def test_buy_open_t_plus_1_fills_next_open():
    days = _dates(3)
    start = days[0]
    rows = [
        {
            "symbol": "A",
            "date": days[0],
            "name": "A",
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
        {
            "symbol": "A",
            "date": days[1],
            "name": "A",
            "open": 10.4,
            "high": 10.6,
            "low": 10.1,
            "close": 10.5,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
        {
            "symbol": "A",
            "date": days[2],
            "name": "A",
            "open": 10.5,
            "high": 10.7,
            "low": 10.2,
            "close": 10.4,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
    ]
    panel = pl.DataFrame(rows)

    def handle(context):
        if context.current_date == start:
            context.order_buy("A", 1040.0)  # 10.4 开盘价下恰好一手

    result = EventBacktestEngine().run(
        _strategy(handle),
        panel,
        {},
        _matcher(entry_fill="open_t+1", exit_fill="close_t"),
        start=start,
    )

    assert result.trades[0].entry_date == days[1].isoformat()
    assert result.trades[0].entry_price == 10.4  # 次日开盘价


def test_stop_loss_exit_on_trigger_day():
    days = _dates(3)
    start = days[0]
    rows = [
        {
            "symbol": "A",
            "date": days[0],
            "name": "A",
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
        {
            "symbol": "A",
            "date": days[1],
            "name": "A",
            "open": 9.0,
            "high": 9.0,
            "low": 8.5,
            "close": 9.0,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
        {
            "symbol": "A",
            "date": days[2],
            "name": "A",
            "open": 9.0,
            "high": 9.0,
            "low": 8.8,
            "close": 8.9,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
    ]
    panel = pl.DataFrame(rows)

    def handle(context):
        if context.current_date == start:
            context.order_buy("A", 1000.0)

    result = EventBacktestEngine().run(
        _strategy(handle, stop_loss=0.1),
        panel,
        {},
        _matcher(matching="close_t", stop_loss_pct=0.1),
        start=start,
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == 9.0
    assert result.trades[0].exit_date == days[1].isoformat()


def test_limit_up_buy_pends_and_fills_next_day():
    days = _dates(3)
    start = days[0]
    rows = [
        {
            "symbol": "A",
            "date": days[0],
            "name": "A",
            "open": 11.0,
            "high": 11.0,
            "low": 11.0,
            "close": 11.0,
            "volume": 100_000,
            "signal_limit_up": True,
            "signal_limit_down": False,
        },
        {
            "symbol": "A",
            "date": days[1],
            "name": "A",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
        {
            "symbol": "A",
            "date": days[2],
            "name": "A",
            "open": 10.1,
            "high": 10.3,
            "low": 10.0,
            "close": 10.2,
            "volume": 100_000,
            "signal_limit_up": False,
            "signal_limit_down": False,
        },
    ]
    panel = pl.DataFrame(rows)

    def handle(context):
        if context.current_date == start:
            context.order_buy("A", 1100.0)

    result = EventBacktestEngine().run(
        _strategy(handle),
        panel,
        {},
        _matcher(matching="close_t"),
        start=start,
    )

    assert result.trades[0].entry_date == days[1].isoformat()
    assert result.trades[0].entry_price == 10.1  # 次日收盘成交
    assert result.reject_counts["buy_limit_up"] == 1


def test_cancel_stops_loop_and_finalizes():
    days = _dates(4)
    start = days[0]
    cancel = threading.Event()
    progress = []

    def handle(context):
        if context.current_date == start:
            context.order_buy("A", 1000.0)

    def progress_cb(message):
        progress.append(message)
        cancel.set()  # 首日后取消

    result = EventBacktestEngine().run(
        _strategy(handle),
        _panel(days, ["A"]),
        {},
        _matcher(matching="close_t"),
        start=start,
        progress_cb=progress_cb,
        cancel_event=cancel,
    )

    assert result.cancelled is True
    assert len(progress) == 1  # 只跑完一天
    assert result.trades[0].exit_reason == "end"
    assert result.trades[0].exit_date == start.isoformat()


def test_entry_end_tail_skips_script_but_fills_pending():
    days = _dates(4)
    start = days[0]
    entry_end = days[1]

    def handle(context):
        context.state["calls"] = context.state.get("calls", 0) + 1
        if context.current_date == entry_end:
            context.order_buy("A", 1000.0)  # open_t+1 → 尾部 days[2] 成交

    result = EventBacktestEngine().run(
        _strategy(handle),
        _panel(days, ["A"]),
        {},
        _matcher(entry_fill="open_t+1", exit_fill="close_t"),
        start=start,
        entry_end=entry_end,
    )

    assert result.state["calls"] == 2  # 尾部不再调用脚本
    assert result.trades[0].entry_date == days[2].isoformat()
    assert result.trades[0].exit_reason == "end"


def test_script_error_propagates_with_phase_name():
    days = _dates(2)
    start = days[0]

    def handle(context):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match=r"handle_data 执行失败.*boom"):
        EventBacktestEngine().run(
            _strategy(handle),
            _panel(days, ["A"]),
            {},
            _matcher(matching="close_t"),
            start=start,
        )


def test_progress_reported_per_day():
    days = _dates(3)
    start = days[0]
    progress = []

    def handle(context):
        pass

    EventBacktestEngine().run(
        _strategy(handle),
        _panel(days, ["A"]),
        {},
        _matcher(matching="close_t"),
        start=start,
        progress_cb=progress.append,
    )

    assert [m["day"] for m in progress] == [1, 2, 3]
    assert all(m["total"] == 3 for m in progress)
    assert [m["date"] for m in progress] == [d.isoformat() for d in days]


def test_universe_by_day_restricts_buys():
    days = _dates(3)
    start = days[0]

    def handle(context):
        if context.current_date == start:
            context.order_buy("B", 1000.0)

    result = EventBacktestEngine().run(
        _strategy(handle),
        _panel(days, ["A", "B"]),
        {},
        _matcher(matching="close_t"),
        start=start,
        universe_by_day={start.isoformat(): {"A"}},
    )

    assert result.trades == []
    assert result.reject_counts["buy_not_in_universe"] == 1


def test_sell_signal_and_same_day_reentry_blocked():
    days = _dates(4)
    start = days[0]

    def handle(context):
        if context.current_date == start:
            context.order_buy("A", 1000.0)
        if context.current_date == days[1]:
            context.order_sell("A", None)
            context.order_buy("A", 1000.0)  # 当日已卖出的再买入 → 拒绝

    result = EventBacktestEngine().run(
        _strategy(handle),
        _panel(days, ["A"]),
        {},
        _matcher(matching="close_t"),
        start=start,
    )

    assert result.trades[0].exit_reason == "signal"
    # 同日 handle_data 内卖后再买: 提交时持仓尚在 → buy_already_held 拒绝。
    assert result.reject_counts["buy_already_held"] == 1


def test_loader_integration_end_to_end(tmp_path):
    (tmp_path / "evt.py").write_text(
        "import polars as pl\n"
        'META = {"id": "evt", "name": "evt", "asset_types": ["stock"], '
        '"timeframes": ["1d"]}\n'
        'EXECUTION_BACKEND = "event"\n'
        'REQUIRED_FEATURES = ["close"]\n'
        "def handle_data(context):\n"
        "    if context.current_date.isoformat() == '2024-01-01':\n"
        "        context.order_buy('A', 1000.0)\n",
        encoding="utf-8",
    )
    from app.strategy.engine import StrategyEngine

    strategy = StrategyEngine(strategy_dirs=[tmp_path]).get("evt")
    days = _dates(3)
    result = EventBacktestEngine().run(
        strategy,
        _panel(days, ["A"]),
        {},
        _matcher(matching="close_t"),
        start=days[0],
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "end"
