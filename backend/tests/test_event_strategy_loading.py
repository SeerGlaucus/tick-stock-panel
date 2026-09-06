from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext, StrategyEngine


def _event_code(
    strategy_id: str = "event_demo",
    *,
    handle_data: bool = True,
    features: list[str] | None = None,
    timeframes: list[str] | None = None,
    history_bars: int | None = None,
    extra: str = "",
) -> str:
    features = features if features is not None else ["close"]
    timeframes = timeframes if timeframes is not None else ["1d"]
    tf_text = ", ".join(f'"{t}"' for t in timeframes)
    feat_text = ", ".join(f'"{f}"' for f in features)
    handler = "def handle_data(context):\n    pass\n" if handle_data else ""
    history_line = f'    "history_bars": {history_bars},\n' if history_bars is not None else ""
    return (
        "import polars as pl\n"
        "META = {\n"
        f'    "id": "{strategy_id}",\n'
        f'    "name": "{strategy_id}",\n'
        '    "asset_types": ["stock"],\n'
        f'    "timeframes": [{tf_text}],\n'
        f"{history_line}"
        "}\n"
        'EXECUTION_BACKEND = "event"\n'
        f"REQUIRED_FEATURES = [{feat_text}]\n"
        f"{handler}"
        f"{extra}"
    )


def test_event_strategy_loads_with_callbacks(tmp_path):
    code = _event_code() + "\ndef initialize(context):\n    pass\n"
    (tmp_path / "event_demo.py").write_text(code, encoding="utf-8")

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    s = engine.get("event_demo")
    assert s.execution_backend == "event"
    assert s.handle_data_fn is not None
    assert s.initialize_fn is not None
    assert s.before_trading_start_fn is None
    assert s.after_trading_end_fn is None
    assert s.required_features == frozenset({"close"})
    assert s.event_history_bars == 60


def test_event_strategy_custom_history_bars(tmp_path):
    (tmp_path / "event_demo.py").write_text(
        _event_code(history_bars=120), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert engine.get("event_demo").event_history_bars == 120


def test_list_strategies_exposes_event_backend(tmp_path):
    (tmp_path / "event_demo.py").write_text(_event_code(), encoding="utf-8")

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    metas = {m["id"]: m for m in engine.list_strategies()}
    assert metas["event_demo"]["execution_backend"] == "event"


def test_event_strategy_requires_handle_data(tmp_path):
    (tmp_path / "event_demo.py").write_text(
        _event_code(handle_data=False), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert not engine.has("event_demo")
    errors = engine.load_errors()
    assert len(errors) == 1
    assert "handle_data" in errors[0]["error"]


def test_event_strategy_requires_1d_timeframe(tmp_path):
    (tmp_path / "event_demo.py").write_text(
        _event_code(timeframes=["1m"]), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert not engine.has("event_demo")
    assert "1d" in engine.load_errors()[0]["error"]


def test_event_strategy_requires_required_features(tmp_path):
    (tmp_path / "event_demo.py").write_text(
        _event_code(features=[]), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert not engine.has("event_demo")
    assert "REQUIRED_FEATURES" in engine.load_errors()[0]["error"]


def test_event_strategy_rejects_entry_signals(tmp_path):
    (tmp_path / "event_demo.py").write_text(
        _event_code(extra='ENTRY_SIGNALS = ["signal_x"]\n'), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert not engine.has("event_demo")
    assert "ENTRY_SIGNALS" in engine.load_errors()[0]["error"]


def test_event_strategy_rejects_filter_function(tmp_path):
    (tmp_path / "event_demo.py").write_text(
        _event_code(extra="def filter(df, params):\n    return pl.lit(True)\n"),
        encoding="utf-8",
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert not engine.has("event_demo")
    assert "filter" in engine.load_errors()[0]["error"]


@pytest.mark.parametrize("history_bars", [0, 501])
def test_event_strategy_history_bars_bounds(tmp_path, history_bars):
    (tmp_path / "event_demo.py").write_text(
        _event_code(history_bars=history_bars), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert not engine.has("event_demo")
    assert "history_bars" in engine.load_errors()[0]["error"]


def test_run_rejects_event_strategy(tmp_path):
    (tmp_path / "event_demo.py").write_text(_event_code(), encoding="utf-8")

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    with pytest.raises(ValueError, match="仅支持回测验证"):
        engine.run(
            "event_demo",
            StrategyDataContext(
                asset_type="stock",
                timeframe="1d",
                as_of=date(2026, 1, 2),
                current=pl.DataFrame({"symbol": ["000001.SZ"]}),
            ),
        )


def test_event_strategy_does_not_break_other_backends(tmp_path):
    # 同一目录混放 event 与其他后端: 互不影响, 正常策略仍可运行。
    (tmp_path / "event_demo.py").write_text(_event_code(), encoding="utf-8")
    (tmp_path / "daily.py").write_text(
        'import polars as pl\n'
        'META = {"id": "daily", "name": "daily", '
        '"asset_types": ["stock"], "timeframes": ["1d"]}\n'
        'EXECUTION_BACKEND = "polars_expr"\n'
        'def filter(df, params):\n'
        "    return pl.lit(True)\n",
        encoding="utf-8",
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert engine.has("event_demo")
    assert engine.has("daily")
    result = engine.run(
        "daily",
        StrategyDataContext(
            asset_type="stock",
            timeframe="1d",
            as_of=date(2026, 1, 2),
            current=pl.DataFrame({"symbol": ["000001.SZ"]}),
        ),
        overrides={"basic_filter": {"enabled": False}},
    )
    assert result.total == 1


def _daily_context() -> StrategyDataContext:
    return StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 1, 2),
        current=pl.DataFrame({"symbol": ["000001.SZ"]}),
    )


def _write_mixed_strategies(tmp_path) -> None:
    (tmp_path / "event_demo.py").write_text(_event_code(), encoding="utf-8")
    (tmp_path / "daily.py").write_text(
        'import polars as pl\n'
        'META = {"id": "daily", "name": "daily", '
        '"asset_types": ["stock"], "timeframes": ["1d"]}\n'
        'EXECUTION_BACKEND = "polars_expr"\n'
        'def filter(df, params):\n'
        "    return pl.lit(True)\n",
        encoding="utf-8",
    )


def test_run_all_skips_event_strategies_by_default(tmp_path):
    _write_mixed_strategies(tmp_path)

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    results = engine.run_all(
        _daily_context(),
        overrides_map={"daily": {"basic_filter": {"enabled": False}}},
    )

    assert set(results) == {"daily"}
    assert results["daily"].total == 1


def test_run_all_explicit_event_strategy_is_fail_closed(tmp_path):
    _write_mixed_strategies(tmp_path)

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    with pytest.raises(ValueError, match="仅支持回测验证"):
        engine.run_all(_daily_context(), strategy_ids=["event_demo"])
