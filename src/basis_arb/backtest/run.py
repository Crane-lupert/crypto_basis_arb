"""Backtest CLI.

    python -m basis_arb.backtest.run --root BTCUSDT --panel data/panel_BTCUSDT_1h.parquet \
        --out reports/BTCUSDT [--strategies all] [--grid none|small|full]

Exit codes: 0 ok, 2 bad input (missing/invalid panel, unknown strategy).
Input is checked before pandas / matplotlib are imported, so bad invocations fail fast.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("basis_arb.backtest.run")

STRATEGY_NAMES = ("cash_and_carry", "funding_carry", "calendar", "random_maturity")  # = types.STRATEGIES


class InputError(Exception):
    """Bad CLI input; reported without a traceback."""


def load_panel(path: Path):
    """Read a panel parquet (index or column 'ts') and validate it against the schema contract."""
    import pandas as pd

    from basis_arb.types import validate_panel

    if not path.is_file():
        raise InputError(f"panel file not found: {path}")
    try:
        panel = pd.read_parquet(path)
    except Exception as exc:  # unreadable / not parquet
        raise InputError(f"cannot read panel {path}: {exc}") from exc
    if "ts" in panel.columns:
        panel = panel.set_index("ts")
    try:
        validate_panel(panel)
    except ValueError as exc:
        raise InputError(f"invalid panel {path}: {exc}") from exc
    return panel


def parse_strategies(text: str) -> tuple[str, ...]:
    if text.strip().lower() == "all":
        return STRATEGY_NAMES
    names = tuple(s.strip() for s in text.split(",") if s.strip())
    unknown = [s for s in names if s not in STRATEGY_NAMES]
    if unknown or not names:
        raise InputError(f"unknown strategies {unknown or text!r}; choose from {STRATEGY_NAMES} or 'all'")
    return names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m basis_arb.backtest.run", description="Run the pre-registered backtest.")
    ap.add_argument("--root", required=True, help="underlying symbol, e.g. BTCUSDT (labels instruments and the report)")
    ap.add_argument("--panel", required=True, help="panel parquet built by data/panel.py (index 'ts', UTC)")
    ap.add_argument("--out", required=True, help="output directory for the report")
    ap.add_argument("--strategies", default="all", help="'all' or a comma-separated list of strategy names")
    ap.add_argument("--grid", default="none", choices=("none", "small", "full"), help="sensitivity grid mode")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    panel_path = Path(args.panel)
    try:
        strategies = parse_strategies(args.strategies)
        if not panel_path.is_file():
            raise InputError(f"panel file not found: {panel_path} (build it with `python -m basis_arb.data.panel`)")
        panel = load_panel(panel_path)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from basis_arb.backtest import metrics as mt
    from basis_arb.backtest.engine import prepare_panel, run_backtest
    from basis_arb.backtest.report import headline_markdown, write_report
    from basis_arb.backtest.sensitivity import FULL_GRID, run_grid
    from basis_arb.config import BacktestConfig
    from basis_arb.model import signals

    t0 = time.time()
    cfg = BacktestConfig()
    out = Path(args.out)
    enriched = signals.enrich(panel, cfg)
    prepared = prepare_panel(panel)
    results = {}
    for s in strategies:
        targets = signals.STRATEGY_FUNCS[s](enriched, cfg)
        results[s] = run_backtest(panel, s, cfg, targets=targets, root=args.root, prepared=prepared)
        log.info("%s done (%.1fs)", s, time.time() - t0)

    sens = None
    if args.grid != "none":
        sens = run_grid(panel, args.root, cfg, FULL_GRID, mode=args.grid, strategies=strategies, out_dir=out)
        log.info("sensitivity grid (%s) done (%.1fs)", args.grid, time.time() - t0)

    paths = write_report(results, enriched, out, args.root, sensitivity=sens, panel_path=str(panel_path))
    summaries = {s: mt.summarize(r) for s, r in results.items()}
    print(f"{args.root}: {panel.index[0]} .. {panel.index[-1]} ({len(panel):,} bars), backtest")
    print(headline_markdown(summaries))
    if sens is not None:
        print(f"sensitivity: N_configs = {sens.attrs['n_configs']} grid cells x {len(strategies)} strategies"
              f" + {sens.attrs.get('n_replication_cells', 0)} HMRW replication cells")
    print(f"report: {paths['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
