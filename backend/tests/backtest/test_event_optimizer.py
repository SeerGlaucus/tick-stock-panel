"""事件策略的优化/步进限制 (T2.1)。

- 优化器: mc_* 蒙特卡洛目标 fail-closed 拒绝; 大网格给出明确警告。
- 步进服务: event 走通用路径 (每折独立优化 + OOS), 不再 fail-closed。
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app.backtest.optimizer import OptimizeConfig, StrategyOptimizer
from app.backtest.walkforward import WalkForwardConfig, WalkForwardService, generate_folds
from app.strategy.engine import StrategyEngine

EVENT_SOURCE = """
import polars as pl

META = {
    "id": "evt_opt",
    "name": "evt_opt",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "p1", "label": "p1", "type": "int", "default": 5, "min": 0, "max": 99},
        {"id": "p2", "label": "p2", "type": "int", "default": 1, "min": 0, "max": 9},
    ],
}
EXECUTION_BACKEND = "event"
REQUIRED_FEATURES = ["close"]


def handle_data(context):
    pass
"""


def _engine(tmp_path) -> StrategyEngine:
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir(exist_ok=True)
    (strat_dir / "evt_opt.py").write_text(EVENT_SOURCE, encoding="utf-8")
    return StrategyEngine(strategy_dirs=[strat_dir])


def _cfg(**kwargs) -> OptimizeConfig:
    base = dict(
        strategy_id="evt_opt",
        symbols=None,
        start=date(2026, 1, 1),
        end=date(2026, 2, 1),
        param_grid={"p1": {"min": 0, "max": 9, "step": 1}},
        objective="sortino",
    )
    base.update(kwargs)
    return OptimizeConfig(**base)


def test_event_optimizer_rejects_monte_carlo_objective(tmp_path):
    optimizer = StrategyOptimizer(service=None, strategy_engine=_engine(tmp_path))
    with pytest.raises(ValueError, match="蒙特卡洛"):
        optimizer.optimize(_cfg(objective="mc_maxdd_p95"))


def test_event_optimizer_warns_on_large_grid(tmp_path, caplog):
    optimizer = StrategyOptimizer(service=None, strategy_engine=_engine(tmp_path))
    # p1(100) x p2(10) = 1000 组 > 事件上限 500 → 警告; 各组回测因 service=None
    # 被逐组隔离为 error 行, 不抛异常。
    grid = {
        "p1": {"min": 0, "max": 99, "step": 1},
        "p2": {"min": 0, "max": 9, "step": 1},
    }
    with caplog.at_level(logging.WARNING, logger="app.backtest.optimizer"):
        result = optimizer.optimize(_cfg(param_grid=grid))
    assert any("超过建议上限" in record.message for record in caplog.records)
    assert result  # 全 error 行也返回结构化结果


def test_walkforward_allows_event_generic_path(tmp_path):
    engine = _engine(tmp_path)
    service = WalkForwardService(optimizer=None, service=None, strategy_engine=engine)
    cfg = WalkForwardConfig(
        strategy_id="evt_opt",
        symbols=None,
        start=date(2025, 12, 1),
        end=date(2026, 3, 1),
        train_days=30,
        test_days=10,
        step_days=10,
        param_grid={"p1": {"min": 0, "max": 9, "step": 1}},
        objective="sortino",
    )
    folds = generate_folds(cfg.start, cfg.end, cfg.train_days, cfg.test_days, cfg.step_days)
    assert service._prepare_shared_matrix(cfg, folds) is None
