"""Cost and latency diagnostics: who earns the basis? (backtest; no rule, threshold or cost assumption changed)

Reads the committed sensitivity grids (`reports/{ROOT}/sensitivity.csv`) and the panels, and writes
`{out}/summary.md` and `{out}/fee_curve.png`:

1. excess return against round-trip cost, from the cost-tier cells of the sanctioned grid (spec section 8)
   and the HMRW replication cells; these cells lower the fees but keep taker fills at the hourly close
2. one-bar fill lag against the pre-registered cell, every strategy (same grid)
3. the S1 fill-lag gap decomposed per round trip, with the one-hour basis move at the decision bars
4. one calendar year (default 2026, year to date) of every sanctioned cell: full-sample runs, sliced, so a
   position entered earlier is carried into the year exactly as in the full run
5. perpetual vs spot at hourly closes by year, and how often S4's entry condition holds at VIP0 / HMRW-low cost

Usage:
    python scripts/cost_latency_report.py --roots BTCUSDT ETHUSDT --out reports/cost_latency
"""
from __future__ import annotations

import argparse
import dataclasses
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from basis_arb.backtest import metrics as mt  # noqa: E402
from basis_arb.backtest import sensitivity as sens  # noqa: E402
from basis_arb.backtest.engine import prepare_panel, run_backtest  # noqa: E402
from basis_arb.config import KAPPA, BacktestConfig  # noqa: E402
from basis_arb.model import fair_value as fv  # noqa: E402
from basis_arb.model import signals  # noqa: E402
from basis_arb.types import STRATEGIES, STRATEGY_LEGS  # noqa: E402

LABELS = {"cash_and_carry": "S1 cash-and-carry", "funding_carry": "S2 funding carry",
          "calendar": "S3 calendar", "random_maturity": "S4 random-maturity"}
COLORS = {"cash_and_carry": "#2a78d6", "funding_carry": "#eb6834", "calendar": "#1baf7a",
          "random_maturity": "#eda100"}  # validated categorical slots 1-4 (light surface)
MARKERS = {"cash_and_carry": "o", "funding_carry": "s", "calendar": "^", "random_maturity": "D"}
TEXT, MUTED, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"


def round_trip_bp(strategy: str, tier: str) -> float:
    legs = tuple(leg for leg, _ in STRATEGY_LEGS[strategy])
    return sens.ALL_TIERS[tier].round_trip_frac(legs) * 1e4


def pct(x: float, nd: int = 2) -> str:
    return "n/a" if not np.isfinite(x) else f"{100.0 * x:.{nd}f}%"


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def base_axes(df: pd.DataFrame, base: dict, skip: tuple[str, ...]) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for axis, col in (("buffer", "buffer"), ("lookback", "lookback"), ("leverage", "leverage"),
                      ("hold_to_expiry", "hold_to_expiry"), ("fill_lag", "fill_lag"), ("costs", "costs")):
        if axis not in skip:
            m &= df[col] == base[axis]
    return m


# --- 1. cost curve -------------------------------------------------------------------------------
def cost_curve(grids: dict[str, pd.DataFrame], base: dict) -> pd.DataFrame:
    rows = []
    for root, g in grids.items():
        sub = g[base_axes(g, base, skip=("costs",))]
        for r in sub.itertuples(index=False):
            rows.append({"root": root, "strategy": r.strategy, "kind": r.kind, "costs": r.costs,
                         "round_trip_bp": round_trip_bp(r.strategy, r.costs),
                         "ann_excess_return": r.ann_excess_return, "excess_sharpe": r.excess_sharpe,
                         "n_round_trips": int(r.n_round_trips), "is_base": bool(r.is_base)})
    df = pd.DataFrame(rows)
    df["_order"] = df.strategy.map(STRATEGIES.index)
    return df.sort_values(["root", "_order", "round_trip_bp"]).drop(columns="_order").reset_index(drop=True)


def plot_cost_curve(cc: pd.DataFrame, path: Path) -> None:
    roots = list(dict.fromkeys(cc.root))
    fig, axes = plt.subplots(1, len(roots), figsize=(6.2 * len(roots), 4.6), sharey=True, facecolor=SURFACE)
    axes = np.atleast_1d(axes)
    for ax, root in zip(axes, roots):
        ax.set_facecolor(SURFACE)
        ax.axhline(0.0, color=MUTED, lw=1.0, zorder=1)
        ax.text(0.5, 0.15, "T-bill", color=MUTED, fontsize=8, va="bottom")
        for s in STRATEGIES:
            d = cc[(cc.root == root) & (cc.strategy == s)].sort_values("round_trip_bp")
            if d.empty:
                continue
            y = 100.0 * d.ann_excess_return.to_numpy()
            ax.plot(d.round_trip_bp, y, color=COLORS[s], lw=2, marker=MARKERS[s], ms=7,
                    mec=SURFACE, mew=1.5, label=LABELS[s], zorder=3)
            dy = {"funding_carry": 6, "random_maturity": -6}.get(s, 0)  # S2/S4 share their lowest-cost x
            ax.annotate(LABELS[s].split(" ")[0], (d.round_trip_bp.iloc[0], y[0]), xytext=(-6, dy),
                        textcoords="offset points", ha="right", va="center", fontsize=8, color=TEXT)
        ax.axvline(38.0, color=MUTED, lw=0.8, ls=":", zorder=1)
        ax.set_title(f"{root}", loc="left", fontsize=11, color=TEXT)
        ax.set_xlabel("round-trip cost per pair (bp of one leg's notional)", fontsize=9, color=MUTED)
        ax.set_xlim(-6, 42)
        ax.grid(axis="y", color="#e4e3df", lw=0.8)
        ax.tick_params(colors=MUTED, labelsize=8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c9c8c2")
    axes[0].set_ylabel("excess-of-T-bill annual return (%)", fontsize=9, color=MUTED)
    for ax in axes:
        lo, hi = ax.get_ylim()
        ax.text(38.4, hi - 0.04 * (hi - lo), "VIP0 taker\n(spot + futures)", fontsize=7.5, color=MUTED, va="top")
    axes[-1].legend(loc="upper right", bbox_to_anchor=(1.0, 0.86), fontsize=8, frameon=False)
    fig.suptitle("Backtest 2020-01 to 2026-10: excess return vs trading cost (taker fills at the hourly close)",
                 x=0.01, ha="left", fontsize=10, color=TEXT)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


# --- 2. fill lag (grid cells) ----------------------------------------------------------------------
def lag_table(grids: dict[str, pd.DataFrame], base: dict) -> pd.DataFrame:
    rows = []
    for root, g in grids.items():
        sub = g[base_axes(g, base, skip=("fill_lag",)) & (g.kind == "grid")]
        for s in STRATEGIES:
            d = sub[sub.strategy == s].set_index("fill_lag")
            rows.append({"root": root, "strategy": LABELS[s], "lag 0 (pre-registered)": pct(d.loc[0, "ann_excess_return"]),
                         "lag 1 bar": pct(d.loc[1, "ann_excess_return"]),
                         "change (pp)": f"{100 * (d.loc[1, 'ann_excess_return'] - d.loc[0, 'ann_excess_return']):+.2f}"})
    return pd.DataFrame(rows)


# --- 3. S1 lag decomposition ---------------------------------------------------------------------
def s1_lag_decomposition(panel: pd.DataFrame, prep, root: str, cfg: BacktestConfig) -> pd.DataFrame:
    lag = dataclasses.replace(cfg, params=dataclasses.replace(cfg.params, fill_lag_bars=1))
    t0 = run_backtest(panel, "cash_and_carry", cfg, root=root, prepared=prep).trades.reset_index(drop=True)
    t1 = run_backtest(panel, "cash_and_carry", lag, root=root, prepared=prep).trades.reset_index(drop=True)
    if len(t0) != len(t1):
        raise RuntimeError("lagged run has a different number of round trips; decomposition by trip is undefined")
    idx = panel.index

    def ln_basis_bp(ts: pd.Timestamp, symbol: str) -> float:
        row = panel.loc[ts]
        for k in ("cq", "nq"):
            if row[f"fut_{k}_symbol"] == symbol:
                return float(np.log(row[f"fut_{k}_close"] / row["spot_close"]) * 1e4)
        return float("nan")

    def one_hour_move(ts: pd.Timestamp, symbol: str) -> float:
        i = idx.get_loc(ts)
        return ln_basis_bp(idx[i + 1], symbol) - ln_basis_bp(ts, symbol) if i + 1 < len(idx) else float("nan")

    rows = []
    for i in range(len(t0)):
        entry, exit_ = pd.Timestamp(t0.entry_ts[i]), pd.Timestamp(t0.exit_ts[i])
        contracts = str(t0.contract[i]).split(">")
        rows.append({"entry": entry.strftime("%Y-%m-%d %H:00"), "exit": exit_.strftime("%Y-%m-%d %H:00"),
                     "contract": t0.contract[i], "pnl lag 0": t0.pnl_total[i], "pnl lag 1": t1.pnl_total[i],
                     "diff": t1.pnl_total[i] - t0.pnl_total[i],
                     "entry 1h basis move (bp)": one_hour_move(entry, contracts[0]),
                     "exit 1h basis move (bp)": one_hour_move(exit_, contracts[-1])})
    return pd.DataFrame(rows)


# --- 4. one year of every sanctioned cell -------------------------------------------------------
def year_slice(panel: pd.DataFrame, prep, root: str, base_cfg: BacktestConfig, year: int) -> pd.DataFrame:
    base = sens.base_cell(base_cfg)
    grid_cells = list(sens.iter_cells(sens.FULL_GRID, base, "small"))
    rows = []
    for s in STRATEGIES:
        cells = [dict(c, kind="grid") for c in grid_cells]
        if s in sens.REPLICATION_STRATEGIES:
            cells += [dict(base, costs=t, kind="replication") for t in sens.REPLICATION_TIERS]
        cache: dict = {}
        for c in cells:
            cfg = sens.cell_config(base_cfg, c)
            key = sens.target_key(s, cfg) + (cfg.costs, cfg.margin, cfg.notional_per_leg, cfg.params.fill_lag_bars)
            if key not in cache:
                res = run_backtest(panel, s, cfg, root=root, prepared=prep)
                cache[key] = mt.yearly_table(res).loc[year]
            y = cache[key]
            rows.append({"root": root, "strategy": s, "kind": c["kind"],
                         "is_base": c["kind"] == "grid" and all(c[a] == base[a] for a in base),
                         "ret": y["ret"], "excess_ret": y["excess_ret"], "n_entries": int(y["n_entries"]),
                         "n_days": int(y["n_days"])})
    return pd.DataFrame(rows)


def premium_by_year(panel: pd.DataFrame, root: str, cfg: BacktestConfig) -> pd.DataFrame:
    """Perpetual vs spot at hourly closes: median ln(P/S) in bp and the share of bars where S4's entry condition
    (premium deviation above the upper band edge) holds at VIP0 and at HMRW-low cost."""
    p = signals.enrich(panel, cfg)
    p = p[~p["stale_bar"]] if "stale_bar" in p else p
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["random_maturity"])
    upper = {t: fv.no_arb_band_ann(sens.ALL_TIERS[t].round_trip_frac(legs), KAPPA)[1] for t in ("VIP0", "HMRW-low")}
    rows = []
    for year, d in p.groupby(p.index.year):
        dev = d["rm_dev_ann"].dropna()
        rows.append({"root": root, "year": int(year),
                     "median perp-spot gap (bp)": f"{np.median(d['perp_prem_ann'].dropna()) / KAPPA * 1e4:+.1f}",
                     "bars with perp above spot": pct((d["perp_close"] > d["spot_close"]).mean(), 1),
                     "bars above band, VIP0": pct((dev > upper["VIP0"]).mean(), 3),
                     "bars above band, HMRW-low": pct((dev > upper["HMRW-low"]).mean(), 3)})
    return pd.DataFrame(rows)


def year_summary(ys: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (root, s, kind), d in ys.groupby(["root", "strategy", "kind"], sort=False):
        b = d[d.is_base]
        rows.append({"root": root, "strategy": LABELS[s], "cells": kind if kind == "grid" else "HMRW replication",
                     "N": len(d),
                     "base return": pct(b.ret.iloc[0]) if len(b) else "",
                     "base excess": pct(b.excess_ret.iloc[0]) if len(b) else "",
                     "best return": pct(d.ret.max()), "best excess": pct(d.excess_ret.max()),
                     "cells with excess > 0": f"{int((d.excess_ret > 0).sum())}/{len(d)}",
                     "max entries in year": int(d.n_entries.max())})
    return pd.DataFrame(rows)


def git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roots", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="reports/cost_latency")
    ap.add_argument("--year", type=int, default=2026)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cfg = BacktestConfig()
    base = sens.base_cell(cfg)
    grids = {r: pd.read_csv(Path(args.reports) / r / "sensitivity.csv") for r in args.roots}
    cc = cost_curve(grids, base)
    plot_cost_curve(cc, out / "fee_curve.png")
    lag = lag_table(grids, base)

    decomp, years, prem = {}, [], []
    for root in args.roots:
        panel = pd.read_parquet(Path(args.data) / f"panel_{root}_1h.parquet")
        prep = prepare_panel(panel)
        decomp[root] = s1_lag_decomposition(panel, prep, root, cfg)
        years.append(year_slice(panel, prep, root, cfg, args.year))
        prem.append(premium_by_year(panel, root, cfg))
    ys = pd.concat(years, ignore_index=True)
    ys.to_csv(out / f"year_{args.year}_cells.csv", index=False)
    cc.to_csv(out / "cost_curve.csv", index=False)

    lines = [
        "# Cost and latency diagnostics",
        "",
        "Backtest diagnostics, not paper or live results. No rule, threshold or cost assumption was changed: every",
        "cell below is a cell of the sanctioned sensitivity grid (`docs/strategy_spec.md` section 8) or one of its",
        "HMRW replication cells, so `N_configs` is unchanged (12 grid cells per strategy + 3 replication cells for",
        "S2 and S4). Generated by `scripts/cost_latency_report.py` on the engine and model code of commit",
        f"`{git_head()}`; full-sample cell metrics are read from `reports/{{ROOT}}/sensitivity.csv` (commit `36f7433`).",
        "",
        "**Read the fee cells correctly.** The cost-tier and HMRW cells lower the fees but keep the same execution",
        "model: taker fills at the hourly close (HMRW cells without slippage). They are not maker fills. A liquidity",
        "provider earning these fee levels with resting quotes also faces fill uncertainty, queue position and",
        "adverse selection, none of which this backtest measures.",
        "",
        "## 1. Excess return against round-trip cost (full sample 2020-01-01 to 2026-10-17)",
        "",
        "![excess return vs round-trip cost](fee_curve.png)",
        "",
        "Round-trip cost = 2 x (fee + slippage) on both legs, in bp of one leg's notional (spec section 4). Only the",
        "cost axis varies; every other axis is at the pre-registered value.",
        "",
    ]
    tbl = cc.assign(strategy=cc.strategy.map(LABELS), round_trip_bp=cc.round_trip_bp.map(lambda x: f"{x:.1f}"),
                    ann_excess_return=cc.ann_excess_return.map(pct),
                    excess_sharpe=cc.excess_sharpe.map(lambda x: f"{x:.2f}"),
                    is_base=cc.is_base.map(lambda b: "**base**" if b else ""))
    tbl = tbl.rename(columns={"round_trip_bp": "round trip (bp)", "ann_excess_return": "excess ann. return",
                              "excess_sharpe": "excess Sharpe", "n_round_trips": "round trips", "is_base": ""})
    lines += [md_table(tbl[["root", "strategy", "costs", "round trip (bp)", "excess ann. return", "excess Sharpe",
                            "round trips", ""]]), ""]

    lines += ["## 2. One-bar fill lag (full sample)", "",
              "Same decisions, filled one hourly bar later (`fill_lag_bars = 1`), against the pre-registered cell.", "",
              md_table(lag), ""]

    lines += ["## 3. S1 fill-lag gap per round trip", "",
              "Pre-registered cell vs the same cell with one-bar fill lag; `diff` = lagged minus same-bar P&L (USDT,",
              "10,000 USDT per leg). The basis moves are the one-hour change of ln(F/S) in bp from the decision bar",
              "to the next bar, on the contract entered (entry) and the contract held at exit (exit).", ""]
    for root, d in decomp.items():
        total = d["diff"].sum()
        top = d.reindex(d["diff"].abs().sort_values(ascending=False).index).head(3)
        rest = d.drop(top.index)["diff"].sum()
        moves = pd.concat([d["entry 1h basis move (bp)"], d["exit 1h basis move (bp)"]]).abs()
        lines += [f"### {root}", "",
                  f"Total lag gap {total:,.0f} USDT over {len(d)} round trips; the three largest trips contribute "
                  f"{top['diff'].sum():,.0f} USDT and the other {len(d) - 3} trips {rest:+,.0f} USDT. Median absolute "
                  f"one-hour basis move at the decision bars: {moves.median():.1f} bp (round-trip cost at VIP0: 38 bp).",
                  "",
                  md_table(d.assign(**{c: d[c].map(lambda x: f"{x:,.1f}") for c in
                                       ("pnl lag 0", "pnl lag 1", "diff", "entry 1h basis move (bp)",
                                        "exit 1h basis move (bp)")})), ""]

    n_days = int(ys.n_days.max())
    lines += [f"## 4. Calendar year {args.year} (year to date, {n_days} days) of every sanctioned cell", "",
              "Full-sample runs of each cell, sliced to the year (a position entered earlier is carried in). Returns are",
              "period sums, not annualized; excess deducts the T-bill on committed capital, flat days included.",
              f"Per-cell rows: `year_{args.year}_cells.csv`.", "",
              md_table(year_summary(ys)), ""]

    lines += ["## 5. Perpetual vs spot at hourly closes, by year", "",
              "S4 sells a perpetual premium (long spot / short perpetual); the reverse needs a short spot leg and is off",
              "(spec section 5). `bars above band` = share of non-stale hourly bars where the premium deviation",
              "`rho - rho*` exceeds the upper edge `kappa * ln(1 + C)` of the no-arbitrage band at that cost tier.", "",
              md_table(pd.concat(prem, ignore_index=True)), ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out / 'summary.md'} and {out / 'fee_curve.png'}")


if __name__ == "__main__":
    main()
