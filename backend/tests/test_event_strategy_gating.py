"""事件策略能力门控 (fail-closed 规则层)。

覆盖三层防线的规则层:
- 监控规则创建拒绝 event 策略 (monitor_rules_api.save_rule);
- 叠加策略保存拒绝 event 子策略 (_save_composite_strategy)。
选股 run / run_all 的拒绝已在 test_event_strategy_loading 覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api import monitor_rules as monitor_rules_api
from app.api import strategy as strategy_api
from app.config import settings
from app.strategy.engine import StrategyEngine

EVENT_SOURCE = """
META = {
    "id": "evt_gate",
    "name": "evt_gate",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}
EXECUTION_BACKEND = "event"
REQUIRED_FEATURES = ["close"]


def handle_data(context):
    pass
"""


def _event_engine(tmp_path) -> StrategyEngine:
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir(exist_ok=True)
    (strat_dir / "evt_gate.py").write_text(EVENT_SOURCE, encoding="utf-8")
    return StrategyEngine(strategy_dirs=[strat_dir])


def _strategy_rule(**overrides) -> dict:
    rule = {
        "id": "r_evt",
        "name": "r_evt",
        "type": "strategy",
        "asset_type": "stock",
        "scope": "symbols",
        "symbols": ["000001.SZ"],
        "strategy_id": "evt_gate",
        "conditions": [],
        "cooldown_seconds": 0,
        "enabled": True,
    }
    rule.update(overrides)
    return rule


def test_monitor_rule_save_rejects_event_strategy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.resolve_asset_type.return_value = "stock"
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=repo,
                strategy_engine=_event_engine(tmp_path),
            )
        )
    )
    model = monitor_rules_api.RuleModel(**_strategy_rule())

    with pytest.raises(HTTPException) as exc_info:
        monitor_rules_api.save_rule(model, request)

    assert exc_info.value.status_code == 400
    assert "事件驱动" in exc_info.value.detail


def test_composite_save_rejects_event_child(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = _event_engine(tmp_path)
    repo = MagicMock()
    repo.store.data_dir = tmp_path
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                strategy_engine=engine,
                repo=repo,
            )
        )
    )
    req = strategy_api.StrategyCompositeSaveRequest(
        strategy_id="composite_gate",
        name="gate",
        children=[strategy_api.CompositeChildItem(strategy_id="evt_gate", weight=1.0)],
    )

    with pytest.raises(ValueError, match="事件驱动"):
        strategy_api._save_composite_strategy(req, request)
