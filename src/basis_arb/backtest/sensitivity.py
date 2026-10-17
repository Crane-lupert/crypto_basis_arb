"""Sensitivity grid (docs/strategy_spec.md section 8): the only sanctioned variation, reported in full.

Axes: buffer x lookback x leverage x hold_to_expiry x cost tier (4 x 3 x 3 x 2 x 3 = 216 cells per strategy).
`mode="full"` runs the Cartesian product; `mode="small"` varies one axis at a time around the
pre-registered base cell (1 + 3 + 2 + 2 + 1 + 2 = 11 cells). N_configs (cells per strategy) is recorded
so a reader can discount the best cell.

De-duplication (default on): a strategy's targets depend only on the parameters its rule reads
(`_TARGET_FIELDS`, checked against model/signals.py) and on its round-trip cost; the engine depends only
on (targets, costs, margin, notional). Cells that a strategy cannot distinguish (e.g. S4 across buffers)
reuse one run. `dedupe=False` recomputes every cell (used by the tests to verify the shortcut).
"""
from __future__ import annotations

import dataclasses
import itertools
import logging
from pathlib import Path

import pandas as pd

from basis_arb.backtest import metrics as mt
from basis_arb.backtest.engine import prepare_panel, run_backtest
from basis_arb.config import BacktestConfig, CostModel
from basis_arb.model import signals
from basis_arb.types import STRATEGIES, STRATEGY_LEGS

log = logging.getLogger(__name__)

# Binance taker schedules (bp of notional). The BNB tier applies the spec's 25% discount to both venues as
# written in docs/strategy_spec.md section 8. Delivery fee = the tier's futures taker fee.
COST_TIERS: dict[str, CostModel] = {
    "VIP0": CostModel(spot_fee_bps=10.0, fut_fee_bps=5.0, slippage_bps=2.0, delivery_fee_bps=5.0),
    "VIP0_BNB25": CostModel(spot_fee_bps=7.5, fut_fee_bps=3.75, slippage_bps=2.0, delivery_fee_bps=3.75),
    "VIP3": CostModel(spot_fee_bps=6.0, fut_fee_bps=3.2, slippage_bps=2.0, delivery_fee_bps=3.2),
}

FULL_GRID: dict[str, tuple] = {
    "buffer": (0.0, 0.03, 0.06, 0.10),
    "lookback": (30, 90, 180),
    "leverage": (1.0, 2.0, 3.0),
    "hold_to_expiry": (False, True),
    "costs": ("VIP0", "VIP0_BNB25", "VIP3"),
}
GRID_MODES = ("small", "full")

GRID_METRICS = (
    "ann_return_on_capital", "sharpe", "max_dd", "n_round_trips", "hit_rate", "avg_hold_hours",
    "time_in_position_frac", "turnover", "worst_30d", "pnl_total", "pnl_basis", "pnl_funding", "pnl_fees",
    "pnl_delivery", "pnl_liquidation", "n_liquidations", "peak_utilization",
)

# StrategyParams fields each target function reads (model/signals.py)
# (allow_reverse is listed although S1/S2/S4 currently ignore it: extra fields only cost de-duplication)
_TARGET_FIELDS: dict[str, tuple[str, ...]] = {
    "cash_and_carry": ("cc_entry_buffer_ann", "cc_exit_excess_ann", "cc_roll_days", "cc_hold_to_expiry",
                       "min_hold_bars", "allow_reverse"),
    "funding_carry": ("fc_lookback_prints", "fc_entry_buffer_ann", "fc_exit_excess_ann", "fc_expected_hold_days",
                      "min_hold_bars", "allow_reverse"),
    "calendar": ("fc_lookback_prints", "cal_entry_buffer_ann", "cal_exit_excess_ann", "cal_roll_days",
                 "cal_hold_to_expiry", "cal_allow_reverse", "min_hold_bars"),
    "random_maturity": ("rm_exit_dev_ann", "min_hold_bars", "allow_reverse"),
}

_LABELS = {
    "cash_and_carry": "S1 cash_and_carry", "funding_carry": "S2 funding_carry",
    "calendar": "S3 calendar", "random_maturity": "S4 random_maturity",
}


def base_cell(cfg: BacktestConfig) -> dict:
    """The grid cell that corresponds to `cfg` (the pre-registered configuration by default)."""
    p, m = cfg.params, cfg.margin
    if not (p.cc_entry_buffer_ann == p.fc_entry_buffer_ann == p.cal_entry_buffer_ann):
        log.warning("base config uses different entry buffers per strategy; the grid's buffer axis sets all three")
    if p.cc_hold_to_expiry != p.cal_hold_to_expiry:
        log.warning("base config holds S1 and S3 to expiry differently; the grid's hold_to_expiry axis sets both")
    if m.perp_leverage != m.fut_leverage:
        log.warning("base config uses different perp/quarterly leverage; the grid's leverage axis sets both")
    tier = next((name for name, c in COST_TIERS.items() if c == cfg.costs), None)
    return {
        "buffer": float(p.cc_entry_buffer_ann),
        "lookback": int(p.fc_lookback_prints),
        "leverage": float(m.perp_leverage),
        "hold_to_expiry": bool(p.cc_hold_to_expiry),
        "costs": tier,
    }


def cell_config(base: BacktestConfig, cell: dict) -> BacktestConfig:
    params = dataclasses.replace(
        base.params,
        cc_entry_buffer_ann=float(cell["buffer"]),
        fc_entry_buffer_ann=float(cell["buffer"]),
        cal_entry_buffer_ann=float(cell["buffer"]),
        fc_lookback_prints=int(cell["lookback"]),
        cc_hold_to_expiry=bool(cell["hold_to_expiry"]),
        cal_hold_to_expiry=bool(cell["hold_to_expiry"]),
    )
    margin = dataclasses.replace(base.margin, perp_leverage=float(cell["leverage"]), fut_leverage=float(cell["leverage"]))
    if cell["costs"] not in COST_TIERS:
        raise ValueError(f"unknown cost tier {cell['costs']!r}; expected one of {tuple(COST_TIERS)}")
    return dataclasses.replace(base, params=params, margin=margin, costs=COST_TIERS[cell["costs"]])


def iter_cells(grid: dict, base: dict, mode: str = "full"):
    """Yield grid cells (dicts axis -> value). `small` = base cell + one-axis-at-a-time deviations."""
    axes = list(grid)
    if mode == "full":
        for combo in itertools.product(*(grid[a] for a in axes)):
            yield dict(zip(axes, combo))
    elif mode == "small":
        yield {a: base[a] for a in axes}
        for a in axes:
            for v in grid[a]:
                if v != base[a]:
                    cell = {b: base[b] for b in axes}
                    cell[a] = v
                    yield cell
    else:
        raise ValueError(f"grid mode must be one of {GRID_MODES}, got {mode!r}")


def target_key(strategy: str, cfg: BacktestConfig, dedupe: bool = True) -> tuple:
    if not dedupe:
        return (strategy, cfg.params, cfg.costs)
    legs = tuple(leg for leg, _ in STRATEGY_LEGS[strategy])
    fields = tuple(getattr(cfg.params, f) for f in _TARGET_FIELDS[strategy])
    return (strategy, fields, cfg.costs.round_trip_frac(legs))


def run_grid(
    panel: pd.DataFrame,
    root: str,
    base_cfg: BacktestConfig | None = None,
    grid: dict | None = None,
    *,
    mode: str = "full",
    strategies: tuple[str, ...] | None = None,
    out_dir=None,
    dedupe: bool = True,
) -> pd.DataFrame:
    """Run the sensitivity grid; one row per (cell, strategy) with headline metrics. Saves files if out_dir."""
    base_cfg = base_cfg or BacktestConfig()
    grid = dict(FULL_GRID if grid is None else grid)
    strategies = tuple(strategies or STRATEGIES)
    base = base_cell(base_cfg)
    cells = list(iter_cells(grid, base, mode))
    plan = [(cid, cell, cell_config(base_cfg, cell)) for cid, cell in enumerate(cells)]
    log.info("sensitivity grid %s: %d cells x %d strategies", mode, len(cells), len(strategies))

    prepared = prepare_panel(panel)
    enriched: dict = {}
    summaries: dict = {}
    by_cell: dict = {}
    for s in strategies:
        groups: dict = {}
        for cid, _, cfg in plan:
            groups.setdefault(target_key(s, cfg, dedupe), []).append((cid, cfg))
        for tkey, members in groups.items():
            cfg0 = members[0][1]
            ekey = cfg0.params.fc_lookback_prints if dedupe else (cfg0.params, cfg0.costs)
            if ekey not in enriched:
                enriched[ekey] = signals.enrich(panel, cfg0)
            targets = signals.STRATEGY_FUNCS[s](enriched[ekey], cfg0)
            for cid, cfg in members:
                rkey = (tkey, cfg.costs, cfg.margin, cfg.notional_per_leg) if dedupe else (s, cid)
                if rkey not in summaries:
                    res = run_backtest(panel, s, cfg, targets=targets, root=root, prepared=prepared)
                    summaries[rkey] = mt.summarize(res)
                by_cell[(cid, s)] = summaries[rkey]

    rows = []
    for cid, cell, _ in plan:
        is_base = all(cell[a] == base[a] for a in cell)
        for s in strategies:
            sm = by_cell[(cid, s)]
            rows.append({"cell_id": cid, "strategy": s, **cell, "is_base": is_base, **{k: sm[k] for k in GRID_METRICS}})
    df = pd.DataFrame(rows)
    df.attrs.update({
        "mode": mode, "n_configs": len(cells), "n_strategies": len(strategies),
        "n_backtests": len(cells) * len(strategies), "n_unique_runs": len(summaries),
        "base_cell": base, "grid": {k: list(v) for k, v in grid.items()}, "root": root,
    })
    if not df["is_base"].any():
        log.warning("the base configuration is not a cell of this grid; no row is marked as base")
    if out_dir is not None:
        write_sensitivity(df, out_dir, root)
    return df


# --- output -------------------------------------------------------------------------------------
def _f(x, fmt: str) -> str:
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return "n/a"
    return format(xf, fmt) if xf == xf and abs(xf) != float("inf") else "n/a"


def _stat(x: pd.Series, how: str) -> float:
    """median / min / max over the finite values (NaN if none; avoids numpy's empty-slice warning)."""
    v = pd.to_numeric(x, errors="coerce").astype(float)
    v = v[v.abs() != float("inf")].dropna()
    return float(getattr(v, how)()) if len(v) else float("nan")


def grid_summary_markdown(df: pd.DataFrame) -> str:
    """Per-strategy distribution of Sharpe and annualized return across cells, with the base cell."""
    header = ["Strategy", "Base Sharpe", "Median Sharpe", "Min Sharpe", "Max Sharpe", "Base ann. return",
              "Median ann. return", "Min ann. return", "Max ann. return", "Cells with return > 0"]
    lines = ["| " + " | ".join(header) + " |", "| --- |" + " ---: |" * (len(header) - 1)]
    for s, g in df.groupby("strategy", sort=False):
        base = g[g["is_base"]]
        bs = base["sharpe"].iloc[0] if len(base) else float("nan")
        br = base["ann_return_on_capital"].iloc[0] if len(base) else float("nan")
        sh, ar = g["sharpe"], g["ann_return_on_capital"]
        pos = f"{int((ar.astype(float) > 0).sum())}/{len(g)}"
        lines.append("| " + " | ".join([
            _LABELS.get(s, s), _f(bs, ".2f"), _f(_stat(sh, "median"), ".2f"), _f(_stat(sh, "min"), ".2f"),
            _f(_stat(sh, "max"), ".2f"), _f(100 * br, ".2f") + "%", _f(100 * _stat(ar, "median"), ".2f") + "%",
            _f(100 * _stat(ar, "min"), ".2f") + "%", _f(100 * _stat(ar, "max"), ".2f") + "%", pos,
        ]) + " |")
    return "\n".join(lines)


def _tier_text(name: str) -> str:
    c = COST_TIERS[name]
    return (f"{name} = spot {c.spot_fee_bps:g} / futures {c.fut_fee_bps:g} / slippage {c.slippage_bps:g} / "
            f"delivery {c.delivery_fee_bps:g} bp")


def write_sensitivity(df: pd.DataFrame, out_dir, root: str) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path, md_path = out / "sensitivity.csv", out / "sensitivity.md"
    df.to_csv(csv_path, index=False, lineterminator="\n")
    a = df.attrs
    base = a.get("base_cell", {})
    grid = a.get("grid", {})
    lines = [
        f"# Sensitivity grid: {root}",
        "",
        "Backtest results. This grid is the only sanctioned variation of the pre-registered configuration "
        "(docs/strategy_spec.md section 8); every cell is reported, in grid order, not ranked.",
        "",
        f"- Mode `{a.get('mode')}`; axes: " + "; ".join(f"{k} {v}" for k, v in grid.items()),
        f"- **N_configs = {a.get('n_configs')}** cells per strategy x {a.get('n_strategies')} strategies = "
        f"{a.get('n_backtests')} backtests ({a.get('n_unique_runs')} distinct engine runs after de-duplicating "
        "cells a strategy's rule cannot distinguish).",
        "- Pre-registered base cell (marked **base**): " + ", ".join(f"{k} = {v}" for k, v in base.items()),
        "- Cost tiers: " + "; ".join(_tier_text(t) for t in COST_TIERS),
        "- Axis relevance: buffer moves the S1-S3 entry hurdles (S4's hurdle is the HMRW band edge, no buffer); "
        "lookback moves S2 and S3 (trailing funding window = lookback x 8h); hold_to_expiry sets both "
        "cc_hold_to_expiry (S1) and cal_hold_to_expiry (S3); leverage moves capital and the liquidation "
        "threshold of every futures leg; costs move hurdles and execution costs of all strategies.",
        "",
        "## Distribution across cells",
        "",
        grid_summary_markdown(df),
        "",
        "## Full grid",
    ]
    header = ["Cell", "Buffer", "Lookback", "Leverage", "Hold to expiry", "Costs", "Ann. return", "Sharpe",
              "Max DD", "Round trips", "Time in position", "Liquidations", ""]
    for s, g in df.groupby("strategy", sort=False):
        lines += ["", f"### {_LABELS.get(s, s)}", "", "| " + " | ".join(header) + " |",
                  "| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
        for _, r in g.iterrows():
            lines.append("| " + " | ".join([
                str(r["cell_id"]), _f(100 * r["buffer"], ".0f") + "%", str(r["lookback"]), _f(r["leverage"], "g"),
                str(bool(r["hold_to_expiry"])), str(r["costs"]), _f(100 * r["ann_return_on_capital"], ".2f") + "%",
                _f(r["sharpe"], ".2f"), _f(100 * r["max_dd"], ".2f") + "%", str(int(r["n_round_trips"])),
                _f(100 * r["time_in_position_frac"], ".0f") + "%", str(int(r["n_liquidations"])),
                "**base**" if r["is_base"] else "",
            ]) + " |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return csv_path, md_path
