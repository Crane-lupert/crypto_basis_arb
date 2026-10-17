"""Sensitivity grid (docs/strategy_spec.md section 8, spec v1.0.2): the only sanctioned variation, reported in full.

Axes: buffer x lookback x leverage x hold_to_expiry x fill_lag x cost tier (4 x 3 x 3 x 2 x 2 x 3 = 432 cells
per strategy). `mode="full"` runs the Cartesian product; `mode="small"` varies one axis at a time around the
pre-registered base cell (1 + 3 + 2 + 2 + 1 + 1 + 2 = 12 cells). On top of the grid, three HMRW replication
cells (HMRW's fee levels, no slippage, other axes at base) are run for S4 and S2 only. N_configs (grid cells
per strategy) is recorded so a reader can discount the best cell.

De-duplication (default on): a strategy's targets depend only on the parameters its rule reads
(`_TARGET_FIELDS`, checked against model/signals.py) and on its round-trip cost; the engine depends only on
(targets, costs, margin, notional, fill_lag_bars). Cells that a strategy cannot distinguish (e.g. S4 across
buffers) reuse one run. `dedupe=False` recomputes every cell (used by the tests to verify the shortcut).
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

# Binance taker schedules (bp of notional); delivery fee = the tier's futures taker fee.
# BNB discount: 25% on spot, 10% on USD-M futures (spec v1.0.2, research log item 11).
COST_TIERS: dict[str, CostModel] = {
    "VIP0": CostModel(spot_fee_bps=10.0, fut_fee_bps=5.0, slippage_bps=2.0, delivery_fee_bps=5.0),
    "VIP0_BNB": CostModel(spot_fee_bps=7.5, fut_fee_bps=4.5, slippage_bps=2.0, delivery_fee_bps=4.5),
    "VIP3": CostModel(spot_fee_bps=6.0, fut_fee_bps=3.2, slippage_bps=2.0, delivery_fee_bps=3.2),
}
# He-Manela-Ross-von Wachter fee levels (spot / futures bp, no slippage): replication cells, not grid cells.
REPLICATION_TIERS: dict[str, CostModel] = {
    "HMRW-low": CostModel(spot_fee_bps=2.25, fut_fee_bps=0.18, slippage_bps=0.0, delivery_fee_bps=0.18),
    "HMRW-mid": CostModel(spot_fee_bps=4.5, fut_fee_bps=0.72, slippage_bps=0.0, delivery_fee_bps=0.72),
    "HMRW-high": CostModel(spot_fee_bps=6.75, fut_fee_bps=1.44, slippage_bps=0.0, delivery_fee_bps=1.44),
}
ALL_TIERS: dict[str, CostModel] = {**COST_TIERS, **REPLICATION_TIERS}
REPLICATION_STRATEGIES = ("random_maturity", "funding_carry")

FULL_GRID: dict[str, tuple] = {
    "buffer": (0.0, 0.03, 0.06, 0.10),
    "lookback": (30, 90, 180),
    "leverage": (1.0, 2.0, 3.0),
    "hold_to_expiry": (False, True),
    "fill_lag": (0, 1),
    "costs": ("VIP0", "VIP0_BNB", "VIP3"),
}
GRID_MODES = ("small", "full")

GRID_METRICS = (
    "ann_excess_return", "excess_sharpe", "ann_return_raw", "sharpe_raw", "max_dd", "n_round_trips", "hit_rate",
    "avg_hold_hours", "time_in_position_frac", "turnover", "worst_30d", "pnl_total", "excess_pnl_total",
    "pnl_basis", "pnl_funding", "pnl_fees", "pnl_delivery", "pnl_liquidation", "pnl_resize", "n_liquidations",
    "n_resizes", "peak_utilization",
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
    tier = next((name for name, c in ALL_TIERS.items() if c == cfg.costs), None)
    return {
        "buffer": float(p.cc_entry_buffer_ann),
        "lookback": int(p.fc_lookback_prints),
        "leverage": float(m.perp_leverage),
        "hold_to_expiry": bool(p.cc_hold_to_expiry),
        "fill_lag": int(getattr(p, "fill_lag_bars", 0)),
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
        fill_lag_bars=int(cell["fill_lag"]),
    )
    margin = dataclasses.replace(base.margin, perp_leverage=float(cell["leverage"]), fut_leverage=float(cell["leverage"]))
    if cell["costs"] not in ALL_TIERS:
        raise ValueError(f"unknown cost tier {cell['costs']!r}; expected one of {tuple(ALL_TIERS)}")
    return dataclasses.replace(base, params=params, margin=margin, costs=ALL_TIERS[cell["costs"]])


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
    replication: bool = True,
) -> pd.DataFrame:
    """Run the sensitivity grid (+ HMRW replication cells); one row per (cell, strategy). Saves files if out_dir."""
    base_cfg = base_cfg or BacktestConfig()
    grid = dict(FULL_GRID if grid is None else grid)
    strategies = tuple(strategies or STRATEGIES)
    base = base_cell(base_cfg)
    cells = [(cell, "grid") for cell in iter_cells(grid, base, mode)]
    rep_strats = tuple(s for s in strategies if s in REPLICATION_STRATEGIES) if replication else ()
    if rep_strats:
        for tier in REPLICATION_TIERS:
            cell = {a: base.get(a) for a in grid}
            cell["costs"] = tier
            cells.append((cell, "replication"))
    plan = [(cid, cell, kind, cell_config(base_cfg, cell)) for cid, (cell, kind) in enumerate(cells)]
    n_grid = sum(1 for _, _, kind, _ in plan if kind == "grid")
    log.info("sensitivity grid %s: %d grid cells x %d strategies + %d replication cells x %d strategies",
             mode, n_grid, len(strategies), len(plan) - n_grid, len(rep_strats))

    prepared = prepare_panel(panel)
    enriched: dict = {}
    summaries: dict = {}
    by_cell: dict = {}
    for s in strategies:
        groups: dict = {}
        for cid, _, kind, cfg in plan:
            if kind == "replication" and s not in rep_strats:
                continue
            groups.setdefault(target_key(s, cfg, dedupe), []).append((cid, cfg))
        for tkey, members in groups.items():
            cfg0 = members[0][1]
            ekey = cfg0.params.fc_lookback_prints if dedupe else (cfg0.params, cfg0.costs)
            if ekey not in enriched:
                enriched[ekey] = signals.enrich(panel, cfg0)
            targets = signals.STRATEGY_FUNCS[s](enriched[ekey], cfg0)
            for cid, cfg in members:
                rkey = ((tkey, cfg.costs, cfg.margin, cfg.notional_per_leg, cfg.params.fill_lag_bars)
                        if dedupe else (s, cid))
                if rkey not in summaries:
                    res = run_backtest(panel, s, cfg, targets=targets, root=root, prepared=prepared)
                    summaries[rkey] = mt.summarize(res)
                by_cell[(cid, s)] = summaries[rkey]

    rows = []
    for cid, cell, kind, _ in plan:
        is_base = kind == "grid" and all(cell[a] == base[a] for a in cell)
        for s in strategies:
            if (cid, s) not in by_cell:
                continue
            sm = by_cell[(cid, s)]
            rows.append({"cell_id": cid, "kind": kind, "strategy": s, **cell, "is_base": is_base,
                         **{k: sm[k] for k in GRID_METRICS}})
    df = pd.DataFrame(rows)
    n_rep = len(plan) - n_grid
    df.attrs.update({
        "mode": mode, "n_configs": n_grid, "n_replication_cells": n_rep,
        "replication_strategies": list(rep_strats), "n_strategies": len(strategies),
        "n_backtests": len(df), "n_unique_runs": len(summaries),
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


def _pct(x, nd: int = 2) -> str:
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return "n/a"
    return f"{100.0 * xf:.{nd}f}%" if xf == xf and abs(xf) != float("inf") else "n/a"


def _stat(x: pd.Series, how: str) -> float:
    """median / min / max over the finite values (NaN if none; avoids numpy's empty-slice warning)."""
    v = pd.to_numeric(x, errors="coerce").astype(float)
    v = v[v.abs() != float("inf")].dropna()
    return float(getattr(v, how)()) if len(v) else float("nan")


def grid_summary_markdown(df: pd.DataFrame) -> str:
    """Per-strategy distribution of the headline (excess) metrics across GRID cells, with the base cell."""
    header = ["Strategy", "Base excess Sharpe", "Median", "Min", "Max", "Base excess ann. return", "Median", "Min",
              "Max", "Cells with excess return > 0"]
    lines = ["| " + " | ".join(header) + " |", "| --- |" + " ---: |" * (len(header) - 1)]
    g_all = df[df["kind"] == "grid"] if "kind" in df.columns else df
    for s, g in g_all.groupby("strategy", sort=False):
        base = g[g["is_base"]]
        bs = base["excess_sharpe"].iloc[0] if len(base) else float("nan")
        br = base["ann_excess_return"].iloc[0] if len(base) else float("nan")
        sh, ar = g["excess_sharpe"], g["ann_excess_return"]
        pos = f"{int((ar.astype(float) > 0).sum())}/{len(g)}"
        lines.append("| " + " | ".join([
            _LABELS.get(s, s), _f(bs, ".2f"), _f(_stat(sh, "median"), ".2f"), _f(_stat(sh, "min"), ".2f"),
            _f(_stat(sh, "max"), ".2f"), _pct(br), _pct(_stat(ar, "median")), _pct(_stat(ar, "min")),
            _pct(_stat(ar, "max")), pos,
        ]) + " |")
    return "\n".join(lines)


def replication_markdown(df: pd.DataFrame) -> str | None:
    """HMRW replication cells (S4, S2) next to the same strategies' base cell."""
    if "kind" not in df.columns or not (df["kind"] == "replication").any():
        return None
    sel = df[(df["kind"] == "replication") | (df["is_base"] & df["strategy"].isin(REPLICATION_STRATEGIES))]
    header = ["Strategy", "Costs", "Excess ann. return", "Excess Sharpe", "Raw Sharpe", "Round trips",
              "Time in position", "Liquidations"]
    lines = ["| " + " | ".join(header) + " |", "| --- | --- |" + " ---: |" * (len(header) - 2)]
    for _, r in sel.sort_values(["strategy", "cell_id"]).iterrows():
        label = f"{r['costs']} (base cell)" if r["is_base"] else str(r["costs"])
        lines.append("| " + " | ".join([
            _LABELS.get(r["strategy"], r["strategy"]), label, _pct(r["ann_excess_return"]), _f(r["excess_sharpe"], ".2f"),
            _f(r["sharpe_raw"], ".2f"), str(int(r["n_round_trips"])), _pct(r["time_in_position_frac"], 1),
            str(int(r["n_liquidations"])),
        ]) + " |")
    return "\n".join(lines)


def _tier_text(name: str) -> str:
    c = ALL_TIERS[name]
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
        "(docs/strategy_spec.md section 8); every cell is reported, in grid order, not ranked. Headline metrics "
        "are excess of the T-bill; raw figures are in sensitivity.csv.",
        "",
        f"- Mode `{a.get('mode')}`; axes: " + "; ".join(f"{k} {v}" for k, v in grid.items()),
        f"- **N_configs = {a.get('n_configs')}** grid cells per strategy x {a.get('n_strategies')} strategies, plus "
        f"{a.get('n_replication_cells')} HMRW replication cells for {', '.join(a.get('replication_strategies', [])) or 'none'}"
        f" ({a.get('n_backtests')} backtests, {a.get('n_unique_runs')} distinct engine runs after de-duplicating "
        "cells a strategy's rule cannot distinguish).",
        "- Pre-registered base cell (marked **base**): " + ", ".join(f"{k} = {v}" for k, v in base.items()),
        "- Cost tiers: " + "; ".join(_tier_text(t) for t in ALL_TIERS),
        "- Axis relevance: buffer moves the S1-S3 entry hurdles (S4's hurdle is the HMRW band edge, no buffer); "
        "lookback moves S2 and S3 (trailing funding window = lookback x 8h); hold_to_expiry sets both "
        "cc_hold_to_expiry (S1) and cal_hold_to_expiry (S3); leverage moves the capital and the liquidation "
        "threshold of every futures leg; fill_lag delays every fill by that many bars; costs move hurdles and "
        "execution costs of all strategies.",
        "",
        "## Distribution across grid cells (excess of the T-bill)",
        "",
        grid_summary_markdown(df),
    ]
    rep = replication_markdown(df)
    if rep:
        lines += ["", "## HMRW replication cells", "", rep]
    lines += ["", "## Full grid"]
    header = ["Cell", "Kind", "Buffer", "Lookback", "Leverage", "Hold to expiry", "Fill lag", "Costs",
              "Excess ann. return", "Excess Sharpe", "Raw Sharpe", "Max DD", "Round trips", "Time in position",
              "Liquidations", "Resizes", ""]
    for s, g in df.groupby("strategy", sort=False):
        lines += ["", f"### {_LABELS.get(s, s)}", "", "| " + " | ".join(header) + " |",
                  "| ---: | --- | ---: | ---: | ---: | --- | ---: | --- |" + " ---: |" * 8 + " --- |"]
        for _, r in g.iterrows():
            lines.append("| " + " | ".join([
                str(r["cell_id"]), str(r["kind"]), _f(100 * r["buffer"], ".0f") + "%", str(r["lookback"]),
                _f(r["leverage"], "g"), str(bool(r["hold_to_expiry"])), str(r["fill_lag"]), str(r["costs"]),
                _pct(r["ann_excess_return"]), _f(r["excess_sharpe"], ".2f"), _f(r["sharpe_raw"], ".2f"),
                _pct(r["max_dd"]), str(int(r["n_round_trips"])), _pct(r["time_in_position_frac"], 0),
                str(int(r["n_liquidations"])), str(int(r["n_resizes"])), "**base**" if r["is_base"] else "",
            ]) + " |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return csv_path, md_path
