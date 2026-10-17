"""Backtest report: summary.md, metrics.json, trades_{strategy}.csv and PNG figures (spec v1.0.2).

Headline figures are excess of the T-bill; raw figures come second. Figures are drawn with matplotlib's Agg
canvas directly (no pyplot, no global backend switch). Colors: categorical slots 1-4 of the reference
data-viz palette in fixed strategy order; text and reference lines use neutral ink so that color only
carries series identity.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from basis_arb.backtest import metrics as mt
from basis_arb.backtest.engine import RESIZE_THRESHOLD, BacktestResult
from basis_arb.config import FUNDING_INTERVAL_HOURS, KAPPA, BacktestConfig
from basis_arb.model import fair_value as fv
from basis_arb.types import STRATEGIES, STRATEGY_LEGS

log = logging.getLogger(__name__)

DPI = 120
LINE_W = 1.2  # ~2 px at 120 dpi
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT2 = "#52514e"
AXIS = "#b9b8b2"
GRID = "#e9e8e4"
STRATEGY_COLORS = {
    "cash_and_carry": "#2a78d6",
    "funding_carry": "#eb6834",
    "calendar": "#1baf7a",
    "random_maturity": "#eda100",
}
STRATEGY_LABELS = {
    "cash_and_carry": "S1 cash_and_carry",
    "funding_carry": "S2 funding_carry",
    "calendar": "S3 calendar",
    "random_maturity": "S4 random_maturity",
}
DEFAULT_SPEC_PATH = Path(__file__).resolve().parents[3] / "docs" / "strategy_spec.md"

ENGINE_ASSUMPTIONS = (
    "Headline = excess of the T-bill: the risk-free rate accrues hourly at r_usd/8760 on the previous bar's "
    "capital ((mean r of the day)/365 x capital for a full day), including flat days, because the capital is "
    "committed to the strategy. Raw figures (idle cash earns nothing) are reported second.",
    "Capital = capital actually required by the open position, re-measured each bar: spot value + futures "
    "notional / leverage (S3: both futures legs at their leverage); the target capital while flat. "
    "Hourly return = P&L / previous bar's capital; daily return = sum of hourly returns (no compounding).",
    "Equal coin quantity on both legs: qty = notional_per_leg / close of the first leg (spot for S1/S2/S4, "
    "perpetual for S3). Fills at close x (1 +/- slippage) with fees on the executed notional; the 'basis' "
    "component is marked at reference (close) prices, so slippage appears under fees.",
    "Bar order: mark, settle expiries, funding, margin monitor, trade, resize. Funding is paid to perp legs held "
    "before the bar's trades (a leg opened on a stamp bar does not receive that stamp; a leg closed on a stamp "
    "bar does). Decisions are filled fill_lag_bars bars later (0 pre-registered).",
    "Expiry: a held quarterly with expiry <= ts settles at spot_close with the delivery fee; the remaining leg "
    "is closed on the same bar at close +/- slippage (round trip exit_reason EXPIRY).",
    "Margin monitor on CLOSE prices: worst adverse close move since entry (or since the last resize). Isolated "
    "mode (S1/S2/S4 by default) liquidates a leg whose move exceeds 1/leverage - maintenance at that bar's close "
    "(+/- slippage, taker fee, liquidation penalty) and closes the other leg. S3 uses futures_pair_mode "
    "(cross_collateral: no per-leg liquidation). The intrabar high/low worst case never triggers; it is "
    "counted as a diagnostic. Utilization = adverse close move / (1/leverage - maintenance).",
    f"Resize: at a funding stamp, if |pair notional / target - 1| > {RESIZE_THRESHOLD:.0%} (pair notional = |qty| x "
    "close of the first leg), both legs are traded back to the target notional at close +/- slippage with "
    "fees (P&L component 'resize'); the futures margin is re-established, so the margin monitor restarts.",
    "After a forced close (expiry or liquidation) the engine stays flat until the target's (direction, "
    "contract) pair changes; it never re-enters on the bar of the forced close.",
    "A roll closes the old quarterly and opens the next one (same coin quantity) on the same bar; the "
    "spot/perp leg is untouched and the round trip continues (contract column 'A>B').",
    "A futures leg without a price on a bar keeps its last mark (closing at it logs STALE_PRICE; a resize waits "
    "for a priced stamp); an entry or roll into an unpriced contract is skipped (NO_PRICE) and retried.",
    "Positions open on the last bar are marked to market without exit costs (trades exit_reason OPEN; "
    "excluded from round-trip counts and hit rate).",
    "Daily figures by UTC day; the bar closing at 00:00 UTC belongs to the previous day. Sharpe = mean/std x "
    "sqrt(365) of daily returns; an identically zero series has Sharpe 0. Max drawdown is a fraction of the "
    "running equity peak.",
)


# --- formatting helpers -------------------------------------------------------------------------
def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _pct(x, nd: int = 2) -> str:
    return f"{100.0 * float(x):.{nd}f}%" if _finite(x) else "n/a"


def _num(x, nd: int = 2) -> str:
    return f"{float(x):,.{nd}f}" if _finite(x) else "n/a"


def _int(x) -> str:
    return f"{int(x)}" if _finite(x) else "n/a"


def _md_table(header: list[str], rows: list[list[str]], align: list[str] | None = None) -> str:
    align = align or ["l"] + ["r"] * (len(header) - 1)
    sep = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def _label(strategy: str) -> str:
    return STRATEGY_LABELS.get(strategy, strategy)


def _color(strategy: str, k: int) -> str:
    fallback = ("#e87ba4", "#008300", "#4a3aa7", "#e34948")
    return STRATEGY_COLORS.get(strategy, fallback[k % len(fallback)])


def _jsonable(obj):
    """Recursively convert to JSON-safe values (NaN/inf -> None, timestamps -> ISO strings)."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is pd.NaT:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (float, np.floating)):
        return float(obj) if math.isfinite(float(obj)) else None
    if obj is None or isinstance(obj, (int, str)):
        return obj
    return str(obj)


def _frame_records(df: pd.DataFrame, index_name: str) -> list[dict]:
    out = df.reset_index().rename(columns={"index": index_name})
    return [_jsonable(r) for r in out.to_dict(orient="records")]


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


# --- figures -------------------------------------------------------------------------------------
def _x(index: pd.DatetimeIndex) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx.to_numpy()


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=TEXT2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _new_figure(nrows: int = 1, height: float = 4.2, width: float = 10.0, sharey: bool = False):
    fig = Figure(figsize=(width, height), dpi=DPI, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    axes = fig.subplots(nrows, 1, sharex=True, sharey=sharey, squeeze=False)[:, 0]
    for ax in axes:
        _style(ax)
    return fig, axes


def _title(ax, text: str) -> None:
    ax.set_title(text, loc="left", fontsize=10, color=TEXT)


def _legend(ax, ncol: int = 2) -> None:
    """Legend above the plot area, right-aligned (titles are left-aligned), so it never covers data."""
    leg = ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), fontsize=8, ncol=ncol, frameon=False,
                    borderaxespad=0.2, handlelength=1.8, columnspacing=1.2)
    for t in leg.get_texts():
        t.set_color(TEXT2)


def _direct_labels(ax, items: list[tuple[float, str]]) -> None:
    """Label line ends in secondary ink, nudged apart so labels never overlap."""
    if not items:
        return
    lo, hi = ax.get_ylim()
    span = (hi - lo) or 1.0
    gap = 0.055 * span
    placed: list[float] = []
    for y, text in sorted(items):
        yy = y if not placed else max(y, placed[-1] + gap)
        placed.append(yy)
        ax.annotate(text, xy=(1.0, yy), xycoords=("axes fraction", "data"), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8, color=TEXT2, annotation_clip=False)


def _save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)


def plot_equity(results: dict[str, BacktestResult], path: Path, root: str) -> None:
    """Headline curve: cumulative excess return over the T-bill (sum of hourly excess returns)."""
    fig, (ax,) = _new_figure()
    ends = []
    for k, (name, res) in enumerate(results.items()):
        y = mt.hourly_frame(res)["excess_ret"].cumsum() * 100.0
        ax.plot(_x(y.index), y.to_numpy(), color=_color(name, k), linewidth=LINE_W, label=_label(name))
        if len(y):
            ends.append((float(y.iloc[-1]), f"{_label(name).split()[0]} {y.iloc[-1]:+.1f}%"))
    ax.axhline(0.0, color=AXIS, linewidth=0.8)
    ax.set_ylabel("cumulative excess return (%)", color=TEXT2, fontsize=8)
    _title(ax, f"{root}: cumulative return over the T-bill (backtest)")
    _legend(ax, ncol=2)
    _direct_labels(ax, ends)
    _save(fig, path)


def plot_drawdown(results: dict[str, BacktestResult], path: Path, root: str) -> None:
    """Small multiples (one panel per strategy, shared axes): worst drawdown of each UTC day."""
    fig, axes = _new_figure(nrows=len(results), height=1.9 * len(results) + 0.6, sharey=True)
    for k, ((name, res), ax) in enumerate(zip(results.items(), axes)):
        dd = mt.drawdown_series(res.equity, start=res.capital) * 100.0
        daily = dd.groupby(mt.bar_day(dd.index)).min()
        x, y, color = _x(daily.index), daily.to_numpy(), _color(name, k)
        ax.fill_between(x, y, 0.0, color=color, alpha=0.15, linewidth=0)
        ax.plot(x, y, color=color, linewidth=LINE_W)
        ax.axhline(0.0, color=AXIS, linewidth=0.8)
        _title(ax, f"{_label(name)}: max drawdown {mt.max_drawdown(res.equity, start=res.capital):.2%}")
        ax.set_ylabel("% from peak", color=TEXT2, fontsize=8)
    fig.suptitle(f"{root}: drawdown from the equity peak, worst value per UTC day (backtest)", x=0.01, ha="left",
                 fontsize=11, color=TEXT)
    _save(fig, path)


def _daily_mean(s: pd.Series) -> pd.Series:
    s = pd.Series(s, dtype=float)
    return s.groupby(mt.bar_day(s.index)).mean()


def plot_theory_vs_market(enriched: pd.DataFrame, cfg: BacktestConfig, path: Path, root: str) -> None:
    fig, axes = _new_figure(nrows=3, height=9.0)
    market, fair = STRATEGY_COLORS["cash_and_carry"], TEXT2

    ax = axes[0]
    if {"basis_cq_ann", "fair_basis_ann"} <= set(enriched.columns):
        b = enriched["basis_cq_ann"].astype(float)
        if "fut_cq_dte_days" in enriched.columns:
            b = b.where(enriched["fut_cq_dte_days"].astype(float) >= cfg.params.cc_roll_days)
        bd = _daily_mean(b) * 100.0
        fd = _daily_mean(enriched["fair_basis_ann"]) * 100.0
        ax.plot(_x(bd.index), bd.to_numpy(), color=market, linewidth=LINE_W, label="market b (current quarter)")
        ax.plot(_x(fd.index), fd.to_numpy(), color=fair, linewidth=LINE_W, linestyle="--", label="fair b* = r - y")
        _legend(ax, ncol=2)
    _title(ax, "Dated futures: basis vs cost of carry")
    ax.set_ylabel("% per year", color=TEXT2, fontsize=8)

    ax = axes[1]
    if {"funding_trailing_ann", "fair_funding_ann"} <= set(enriched.columns):
        f_hat = _daily_mean(enriched["funding_trailing_ann"]) * 100.0
        f_star = _daily_mean(enriched["fair_funding_ann"]) * 100.0
        window_days = cfg.params.fc_lookback_prints * FUNDING_INTERVAL_HOURS / 24.0
        ax.plot(_x(f_hat.index), f_hat.to_numpy(), color=market, linewidth=LINE_W,
                label=f"trailing f-hat ({window_days:g}-day window)")
        ax.plot(_x(f_star.index), f_star.to_numpy(), color=fair, linewidth=LINE_W, linestyle="--",
                label="equilibrium f* (HMRW)")
        _legend(ax, ncol=2)
    _title(ax, "Perpetual: funding vs equilibrium funding")
    ax.set_ylabel("% per year", color=TEXT2, fontsize=8)

    ax = axes[2]
    if "rm_dev_ann" in enriched.columns:
        rt = cfg.costs.round_trip_frac(tuple(leg for leg, _ in STRATEGY_LEGS["random_maturity"]))
        lo, hi = fv.no_arb_band_ann(rt, KAPPA)
        hourly = enriched["rm_dev_ann"].astype(float) * 100.0
        dev = _daily_mean(enriched["rm_dev_ann"]) * 100.0
        ax.plot(_x(hourly.index), hourly.to_numpy(), color=market, linewidth=0.5, alpha=0.35,
                label="rho - rho* (hourly)")
        ax.plot(_x(dev.index), dev.to_numpy(), color=market, linewidth=LINE_W, label="daily mean")
        ax.axhline(hi * 100.0, color=fair, linewidth=0.9, linestyle="--",
                   label=f"band [{lo * 100:.0f}%, {hi * 100:.0f}%], C = {rt * 1e4:.0f} bp")
        ax.axhline(lo * 100.0, color=fair, linewidth=0.9, linestyle="--")
        ax.axhline(0.0, color=AXIS, linewidth=0.8)
        _legend(ax, ncol=2)
    _title(ax, "Perpetual: premium deviation vs HMRW band")
    ax.set_ylabel("% per year", color=TEXT2, fontsize=8)
    fig.suptitle(f"{root}: theory vs market", x=0.01, ha="left", fontsize=11, color=TEXT)
    _save(fig, path)


# --- markdown --------------------------------------------------------------------------------------
def _spec_excerpt(spec_path: Path | None, sections: tuple[str, ...] = ("4", "6", "7")) -> str | None:
    path = spec_path or DEFAULT_SPEC_PATH
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    out, keep = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            num = line[3:].split(".", 1)[0].strip()
            keep = num in sections
        if keep:
            out.append(line.replace("## ", "#### ", 1) if line.startswith("## ") else line)
    return "\n".join(out).strip() or None


HEADLINE_HEADER = [
    "Strategy", "Excess ann. return", "Excess Sharpe", "Raw ann. return", "Raw Sharpe", "Max DD", "Round trips",
    "Hit rate", "Avg hold (h)", "Time in position", "Turnover (x/yr)", "Worst 30d", "Liquidations", "Resizes",
]


def _headline_rows(summaries: dict[str, dict]) -> list[list[str]]:
    rows = []
    for name, s in summaries.items():
        rows.append([
            _label(name), _pct(s["ann_excess_return"]), _num(s["excess_sharpe"]), _pct(s["ann_return_raw"]),
            _num(s["sharpe_raw"]), _pct(s["max_dd"]), _int(s["n_round_trips"]), _pct(s["hit_rate"], 0),
            _num(s["avg_hold_hours"], 0), _pct(s["time_in_position_frac"], 0), _num(s["turnover"], 1),
            _pct(s["worst_30d"]), _int(s["n_liquidations"]), _int(s["n_resizes"]),
        ])
    return rows


def headline_markdown(summaries: dict[str, dict]) -> str:
    return _md_table(HEADLINE_HEADER, _headline_rows(summaries))


def _window_cells(r) -> list[str]:
    return [
        _int(r["n_days"]), _num(r["pnl"]), _pct(r["ret"]), _pct(r["excess_ret"]), _num(r["sharpe_raw"]),
        _num(r["excess_sharpe"]), _pct(r["max_dd"]), _num(r["pnl_basis"]), _num(r["pnl_funding"]),
        _num(r["pnl_costs"]), _int(r["n_round_trips"]), _pct(r["time_in_position"], 0), _int(r["n_liquidations"]),
        _int(r["n_resizes"]),
    ]


_WINDOW_HEADER = ["Days", "P&L (USDT)", "Return", "Excess return", "Raw Sharpe", "Excess Sharpe", "Max DD", "Basis",
                  "Funding", "Costs", "Round trips", "Time in position", "Liquidations", "Resizes"]


def _yearly_md(df: pd.DataFrame) -> str:
    rows = [[str(y)] + _window_cells(r) for y, r in df.iterrows()]
    return _md_table(["Year"] + _WINDOW_HEADER, rows)


def _episode_md(df: pd.DataFrame) -> str:
    rows = []
    for name, r in df.iterrows():
        window = f"{r['start']} .. {r['end']}"
        if not r["n_days"]:
            rows.append([name, window, "0%", "not in sample"] + [""] * (len(_WINDOW_HEADER) - 1))
            continue
        rows.append([name, window, _pct(r["coverage"], 0)] + _window_cells(r))
    return _md_table(["Episode", "Window (UTC days)", "Coverage"] + _WINDOW_HEADER, rows)


def _decomposition_md(results: dict[str, BacktestResult], summaries: dict[str, dict]) -> str:
    rows = []
    for name, s in summaries.items():
        rows.append([
            _label(name), _num(results[name].capital, 0), _num(s["mean_capital"], 0), _num(s["pnl_basis"]),
            _num(s["pnl_funding"]), _num(s["pnl_fees"]), _num(s["pnl_delivery"]), _num(s["pnl_liquidation"]),
            _num(s["pnl_resize"]), _num(s["pnl_total"]), _num(-s["rf_charge_total"]), _num(s["excess_pnl_total"]),
        ])
    header = ["Strategy", "Target capital", "Mean capital", "Basis convergence", "Funding", "Fees + slippage",
              "Delivery fees", "Liquidation penalties", "Resize costs", "Total P&L", "T-bill on capital",
              "Excess P&L"]
    return _md_table(header, rows)


def _margin_md(results: dict[str, BacktestResult]) -> str:
    rows = []
    for name, res in results.items():
        m = res.margin_stats
        legs = [leg for leg, _ in STRATEGY_LEGS[name] if leg != "spot"]
        lev = ", ".join(f"{leg} {m[f'{leg}_leverage']:g}x" for leg in legs)
        rows.append([
            _label(name), m["mode"], lev, _num(m["peak_utilization"], 3), _int(m["bars_above_isolated_threshold"]),
            _int(m["intrabar_breach_bars"]), _num(m["peak_intrabar_utilization"], 3),
            _num(m["peak_pair_utilization"], 3), _int(m["n_liquidations"]), _int(m["n_resizes"]),
        ])
    header = ["Strategy", "Margin mode", "Leverage", "Peak utilization (close)", "Bars above isolated threshold",
              "Intrabar breach bars (diagnostic)", "Peak intrabar utilization", "Peak pair utilization",
              "Liquidations", "Resizes"]
    return _md_table(header, rows)


def _funding_md(diag: pd.DataFrame) -> str:
    rows = [[
        str(y), _int(r["n_prints"]), _pct(r["share_at_0.0001"], 1), _int(r["n_1h"]), _int(r["n_4h"]), _int(r["n_8h"]),
        _int(r["n_other"]),
    ] for y, r in diag.iterrows()]
    header = ["Year", "Funding prints", "Share exactly 0.000100", "1h gaps", "4h gaps", "8h gaps", "Other gaps"]
    return _md_table(header, rows)


def _assumptions_md(cfg: BacktestConfig, spec_path: Path | None) -> str:
    c, m = cfg.costs, cfg.margin
    lines = [
        "### Configuration used",
        "",
        f"- Notional per leg: {cfg.notional_per_leg:,.0f} USDT; costs: spot fee {c.spot_fee_bps:g} bp, futures fee "
        f"{c.fut_fee_bps:g} bp, slippage {c.slippage_bps:g} bp per leg, delivery fee {c.delivery_fee_bps:g} bp, "
        f"liquidation penalty {c.liquidation_penalty_bps:g} bp.",
        f"- Margin: spot-leg strategies {m.mode}, futures pair (S3) {getattr(m, 'futures_pair_mode', m.mode)}; perp "
        f"leverage {m.perp_leverage:g}x, quarterly leverage {m.fut_leverage:g}x, maintenance {m.maintenance_margin_frac:.2%}.",
        f"- Strategy parameters: {dataclasses.asdict(cfg.params)}",
        "",
        "### Engine implementation choices",
        "",
    ]
    lines += [f"- {a}" for a in ENGINE_ASSUMPTIONS]
    excerpt = _spec_excerpt(spec_path)
    if excerpt:
        lines += ["", "### Pre-registered spec excerpt (docs/strategy_spec.md, sections 4, 6 and 7, verbatim)", "",
                  excerpt]
    else:
        lines += ["", "(docs/strategy_spec.md not found next to the package; spec excerpt omitted.)"]
    return "\n".join(lines)


def write_report(
    results: dict[str, BacktestResult],
    enriched_panel: pd.DataFrame,
    out_dir,
    root: str,
    *,
    sensitivity: pd.DataFrame | None = None,
    panel_path: str | None = None,
    spec_path: Path | None = None,
) -> dict[str, Path]:
    """Write summary.md, metrics.json, trades_{strategy}.csv and the PNG figures into `out_dir`."""
    if not results:
        raise ValueError("write_report needs at least one BacktestResult")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    order = [s for s in STRATEGIES if s in results] + [s for s in results if s not in STRATEGIES]
    results = {s: results[s] for s in order}
    cfg = next(iter(results.values())).cfg
    summaries = {s: mt.summarize(r) for s, r in results.items()}
    yearly = {s: mt.yearly_table(r) for s, r in results.items()}
    episodes = {s: mt.episode_table(r) for s, r in results.items()}
    funding_diag = mt.funding_diagnostics(enriched_panel)
    paths: dict[str, Path] = {}

    for s, r in results.items():
        p = out / f"trades_{s}.csv"
        r.trades.to_csv(p, index=False, lineterminator="\n")
        paths[f"trades_{s}"] = p

    paths["equity_png"] = out / "equity_curves.png"
    plot_equity(results, paths["equity_png"], root)
    paths["drawdown_png"] = out / "drawdown.png"
    plot_drawdown(results, paths["drawdown_png"], root)
    paths["theory_png"] = out / "theory_vs_market.png"
    plot_theory_vs_market(enriched_panel, cfg, paths["theory_png"], root)

    first = next(iter(summaries.values()))
    n_configs, n_rep, grid_mode, grid_md, rep_md = 1, 0, "none", None, None
    if sensitivity is not None and len(sensitivity):
        from basis_arb.backtest.sensitivity import grid_summary_markdown, replication_markdown

        n_configs = int(sensitivity.attrs.get("n_configs", sensitivity["cell_id"].nunique()))
        n_rep = int(sensitivity.attrs.get("n_replication_cells", 0))
        grid_mode = str(sensitivity.attrs.get("mode", "?"))
        grid_md = grid_summary_markdown(sensitivity)
        rep_md = replication_markdown(sensitivity)

    md = [
        f"# Backtest report: {root}",
        "",
        "Backtest (simulated taker fills on historical hourly bars), not paper or live results.",
        f"Sample {first['start']} to {first['end']} ({first['n_bars']:,} hourly bars, {first['years']:.2f} years)"
        + (f", panel `{panel_path}`." if panel_path else "."),
        f"Target size: {cfg.notional_per_leg:,.0f} USDT per leg, equal coin quantity on both legs, resized to target "
        f"when the pair notional drifts more than {RESIZE_THRESHOLD:.0%}. All figures are net of fees, slippage, "
        "delivery fees, liquidation penalties and resize costs.",
        "",
        "## Headline (excess of the T-bill)",
        "",
        headline_markdown(summaries),
        "",
        "Excess ann. return = total excess return / years, where the excess return deducts the T-bill rate (panel "
        "r_usd) on the capital in use, flat days included. Raw figures assume idle cash earns nothing. Worst 30d = "
        "worst rolling 30-day sum of daily raw returns. Turnover = traded notional / mean capital per year. Round "
        "trips exclude a position still open on the last bar.",
        "",
        "## P&L decomposition (USDT, whole sample)",
        "",
        _decomposition_md(results, summaries),
        "",
        "## Margin",
        "",
        _margin_md(results),
        "",
        "Liquidation is decided on close prices; 'intrabar breach bars' counts bars whose high/low would have "
        "crossed the isolated threshold (diagnostic only, never triggers). Peak pair utilization = net loss of the "
        "two futures legs / their posted margin (S3).",
        "",
        "## Year by year",
        "",
        "Bars are labeled by close time, so the bar closing at 00:00 UTC on 1 January belongs to the previous year "
        "(a sample starting at 00:00 shows a one-bar row for that year). Returns are period sums (not "
        "annualized); round trips are counted in the year they close; costs = fees + slippage + delivery + "
        "liquidation + resize.",
    ]
    for s in results:
        md += ["", f"### {_label(s)}", "", _yearly_md(yearly[s])]
    md += ["", "## Stress episodes"]
    for s in results:
        md += ["", f"### {_label(s)}", "", _episode_md(episodes[s])]
    md += [
        "",
        "## Funding diagnostics",
        "",
        "Share of funding prints exactly equal to 0.000100 (Binance's interest component: the average premium sat "
        "inside the clamp band) and the cadence of prints (hours since the previous print), by year of the print.",
        "",
        _funding_md(funding_diag),
        "",
        "## Sensitivity grid",
        "",
    ]
    if grid_md is None:
        md.append("Not run for this report (`--grid none`): N_configs = 1 (the pre-registered cell only).")
    else:
        md += [
            f"Mode `{grid_mode}`: **N_configs = {n_configs}** grid cells per strategy x {len(results)} strategies"
            + (f", plus {n_rep} HMRW replication cells for S4 and S2" if n_rep else "")
            + ". Discount the best cell accordingly; the full grid is in `sensitivity.md` / `sensitivity.csv`.",
            "",
            grid_md,
        ]
        if rep_md:
            md += ["", "HMRW replication cells (HMRW's fee levels, no slippage; other settings pre-registered):", "",
                   rep_md]
    md += [
        "",
        "## Figures",
        "",
        "![Cumulative return over the T-bill](equity_curves.png)",
        "",
        "Cumulative sum of hourly excess returns (return on the capital in use minus the T-bill), net of all costs.",
        "",
        "![Drawdown](drawdown.png)",
        "",
        "Drawdown from the running equity peak (raw P&L); one panel per strategy, worst value of each UTC day.",
        "",
        "![Theory vs market](theory_vs_market.png)",
        "",
        "Top: annualized current-quarter basis b = ln(F/S)/tau vs the cost-of-carry fair basis b* = r - y (daily "
        "means; bars inside the 2-day roll window are masked because ln(F/S)/tau explodes as tau -> 0). Middle: "
        "trailing funding f-hat vs HMRW equilibrium funding f*. Bottom: HMRW premium deviation rho - rho* (hourly and "
        "daily mean) with the Prop. 2 no-arbitrage band; S4 enters when the hourly value is above the upper edge.",
        "",
        "## Assumptions",
        "",
        _assumptions_md(cfg, spec_path),
        "",
    ]
    paths["summary"] = out / "summary.md"
    _write_text(paths["summary"], "\n".join(md))

    payload = {
        "root": root,
        "panel": panel_path,
        "kind": "backtest",
        "headline": "excess of the T-bill (ann_excess_return, excess_sharpe); raw figures: ann_return_raw, sharpe_raw",
        "config": dataclasses.asdict(cfg),
        "n_configs": n_configs,
        "n_replication_cells": n_rep,
        "grid_mode": grid_mode,
        "funding_diagnostics": _frame_records(funding_diag, "year"),
        "strategies": {
            s: {
                "summary": summaries[s],
                "margin_stats": results[s].margin_stats,
                "yearly": _frame_records(yearly[s], "year"),
                "episodes": _frame_records(episodes[s], "episode"),
            }
            for s in results
        },
    }
    paths["metrics"] = out / "metrics.json"
    _write_text(paths["metrics"], json.dumps(_jsonable(payload), indent=2, sort_keys=False) + "\n")
    log.info("report written to %s", out)
    return paths
