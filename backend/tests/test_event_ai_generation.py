"""事件驱动策略 AI 生成链 (T2.2)。

覆盖:
- 后端 direction 守卫: event 后端非 long 拒绝 (400 语义的 ValueError);
- event 提示词挂载事件契约指南;
- AI 结构校验: event 代码必须含 handle_data 入口 (缺失触发结构修复)。
"""

from __future__ import annotations

import pytest

from app.api.strategy import BuildRequest, _build_prompt
from app.strategy.ai_generator import AIStrategyGenerator, _EVENT_ENTRYPOINT_ERROR

EVENT_CODE = """
import polars as pl

META = {
    "id": "ai_evt",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "scoring": {},
}
EXECUTION_BACKEND = "event"
REQUIRED_FEATURES = ["close"]


def handle_data(context):
    pass
"""

EVENT_CODE_NO_ENTRY = """
import polars as pl

META = {
    "id": "ai_evt",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "scoring": {},
}
EXECUTION_BACKEND = "event"
REQUIRED_FEATURES = ["close"]
"""


def _build_req(**kwargs) -> BuildRequest:
    base = dict(step=1, name="demo", rules="每日买动量 top5")
    base.update(kwargs)
    return BuildRequest(**base)


def test_event_ai_direction_guard_rejects_non_long():
    for direction in ("short", "monitor"):
        req = _build_req(execution_backend="event", direction=direction)
        with pytest.raises(ValueError, match="仅支持做多"):
            _build_prompt(req)


def test_event_ai_prompt_embeds_event_contract():
    req = _build_req(execution_backend="event", direction="long")
    prompt = _build_prompt(req)
    assert "事件驱动策略契约" in prompt
    assert "handle_data" in prompt
    assert "REQUIRED_FEATURES" in prompt
    assert "不得定义 filter" in prompt


def test_event_ai_entrypoint_validation():
    gen = AIStrategyGenerator()
    ok = gen.validate_code(EVENT_CODE)
    assert ok["valid"] is True, ok["error"]

    bad = gen.validate_code(EVENT_CODE_NO_ENTRY)
    assert bad["valid"] is False
    assert bad["error"] == _EVENT_ENTRYPOINT_ERROR
    assert AIStrategyGenerator.needs_structural_repair(bad) is True


def test_non_event_ai_path_unchanged():
    req = _build_req(execution_backend="polars_expr", direction="long")
    prompt = _build_prompt(req)
    assert "事件驱动策略契约" not in prompt
    assert "选股方向" in prompt
