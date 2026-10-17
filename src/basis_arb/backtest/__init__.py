"""Event-driven 1h backtest: engine, metrics, report and the sensitivity grid.

Exports are resolved lazily (PEP 562) so that `python -m basis_arb.backtest.run` can reject bad input
before pandas / matplotlib are imported. `report`, `sensitivity` and `run` are imported explicitly.
"""
from __future__ import annotations

import importlib

_EXPORTS = {
    "BacktestResult": "engine", "run_backtest": "engine", "fills_frame": "engine", "prepare_panel": "engine",
    "EPISODES": "metrics", "daily_pnl": "metrics", "sharpe": "metrics", "max_drawdown": "metrics",
    "summarize": "metrics", "yearly_table": "metrics", "episode_table": "metrics",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'basis_arb.backtest' has no attribute {name!r}")
    return getattr(importlib.import_module(f"basis_arb.backtest.{module}"), name)
