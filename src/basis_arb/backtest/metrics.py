"""Performance metrics on a BacktestResult (docs/strategy_spec.md section 7).

Conventions
-----------
* Bars are labeled by their close time, so the bar closing at 00:00 UTC (covering 23:00-24:00) is
  attributed to the previous UTC day (`bar_day`).
* Daily P&L is in USDT; Sharpe = mean/std(ddof=1) * sqrt(365) on daily P&L (24/7 market), net of costs.
* Returns are on the fixed capital unit (no compounding: the notional per leg is fixed).
* `max_drawdown` returns a positive fraction of the running equity peak (0.05 = 5% peak-to-trough),
  with the starting capital included as the first point.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from basis_arb.config import HOURS_PER_YEAR

NAN = float("nan")
_HOUR = pd.Timedelta(hours=1)

# (name, first UTC day, last UTC day) - inclusive windows, research-log stress episodes
EPISODES: tuple[tuple[str, str, str], ...] = (
    ("Mar-2020", "2020-03-01", "2020-04-30"),
    ("May-2021", "2021-05-01", "2021-08-15"),
    ("Nov-2022 FTX", "2022-11-01", "2022-12-31"),
    ("Aug-2024", "2024-08-01", "2024-09-30"),
    ("Q1/Q2-2026 funding compression", "2026-01-31", "2026-05-31"),
)


# --- series helpers ----------------------------------------------------------------------------
def bar_day(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """UTC day each hourly bar belongs to (bar interval [ts - 1h, ts))."""
    return (pd.DatetimeIndex(index) - _HOUR).floor("D")


def hourly_pnl(equity: pd.Series, initial: float | None = None) -> pd.Series:
    """Bar-by-bar equity change; the first bar is measured from `initial` (default: no change)."""
    eq = pd.Series(equity, dtype=float)
    prev = eq.shift(1)
    if len(eq):
        prev.iloc[0] = eq.iloc[0] if initial is None else float(initial)
    return (eq - prev).rename("pnl")


def daily_pnl(equity: pd.Series, initial: float | None = None) -> pd.Series:
    """Daily P&L (USDT) by UTC day, calendar-complete (days without bars carry 0)."""
    h = hourly_pnl(equity, initial)
    if h.empty:
        return pd.Series(dtype=float, name="pnl")
    d = h.groupby(bar_day(h.index)).sum()
    full = pd.date_range(d.index[0], d.index[-1], freq="D")
    return d.reindex(full, fill_value=0.0).rename("pnl")


def sharpe(daily: pd.Series, periods: int = 365) -> float:
    """Annualized Sharpe of a daily P&L (or return) series: mean / std(ddof=1) * sqrt(periods)."""
    x = pd.Series(daily, dtype=float).dropna().to_numpy()
    if len(x) < 2:
        return NAN
    mu, sd = float(x.mean()), float(x.std(ddof=1))
    if not (math.isfinite(sd) and sd > 1e-12 * max(1.0, abs(mu))):
        return NAN  # flat or numerically constant series: Sharpe undefined
    return mu / sd * math.sqrt(periods)


def _with_start(equity, start: float | None) -> np.ndarray:
    x = pd.Series(equity, dtype=float).dropna().to_numpy()
    return x if start is None else np.concatenate([[float(start)], x])


def max_drawdown(equity: pd.Series, start: float | None = None) -> float:
    """Largest peak-to-trough decline as a positive fraction of the running peak (NaN if empty)."""
    x = _with_start(equity, start)
    if len(x) == 0:
        return NAN
    peak = np.maximum.accumulate(x)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - x) / peak, np.nan)
    return float(np.nanmax(dd)) if np.isfinite(dd).any() else NAN


def drawdown_series(equity: pd.Series, start: float | None = None) -> pd.Series:
    """equity / running peak - 1 (<= 0), aligned to `equity`."""
    eq = pd.Series(equity, dtype=float)
    x = eq.to_numpy(dtype=float)
    if start is not None:
        x = np.concatenate([[float(start)], x])
    peak = np.fmax.accumulate(x)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = x / peak - 1.0
    return pd.Series(dd[len(dd) - len(eq):], index=eq.index, name="drawdown")


def worst_window(daily: pd.Series, days: int = 30) -> float:
    """Worst rolling `days`-day sum of daily P&L (NaN if the series is shorter than the window)."""
    d = pd.Series(daily, dtype=float)
    if len(d) < days:
        return NAN
    return float(d.rolling(days).sum().min())


def years_covered(index: pd.DatetimeIndex) -> float:
    if len(index) == 0:
        return 0.0
    hours = (index[-1] - index[0]) / _HOUR + 1.0
    return float(hours / HOURS_PER_YEAR)


# --- result-level tables -----------------------------------------------------------------------
def _events_of(result, kind: str) -> pd.DataFrame:
    ev = result.events
    return ev[ev["event"] == kind] if len(ev) else ev


def summarize(result) -> dict:
    """Headline metrics of one BacktestResult (JSON-friendly scalars; NaN where undefined)."""
    eq, cap = result.equity, float(result.capital)
    years = years_covered(eq.index)
    daily = daily_pnl(eq, initial=cap)
    comp = result.pnl_components.sum()
    pnl_total = float(eq.iloc[-1] - cap) if len(eq) else 0.0
    trades = result.trades
    closed = trades[trades["exit_reason"] != "OPEN"]
    traded_notional = float(sum(f.qty * f.price for f in result.fills))
    ws = worst_window(daily, 30)
    return {
        "strategy": result.strategy,
        "root": result.root,
        "start": eq.index[0].isoformat() if len(eq) else None,
        "end": eq.index[-1].isoformat() if len(eq) else None,
        "n_bars": int(len(eq)),
        "years": years,
        "capital": cap,
        "ann_return_on_capital": pnl_total / cap / years if years > 0 else NAN,
        "total_return_on_capital": pnl_total / cap,
        "sharpe": sharpe(daily),
        "max_dd": max_drawdown(eq, start=cap),
        "n_round_trips": int(len(closed)),
        "n_open_trades": int(len(trades) - len(closed)),
        "hit_rate": float((closed["pnl_total"] > 0).mean()) if len(closed) else NAN,
        "avg_hold_hours": float(closed["hold_hours"].mean()) if len(closed) else NAN,
        "time_in_position_frac": float((result.position != 0).mean()) if len(result.position) else NAN,
        "turnover": traded_notional / cap / years if years > 0 else NAN,  # traded notional / capital per year
        "worst_30d": ws / cap if math.isfinite(ws) else NAN,  # worst 30-day P&L / capital
        "pnl_total": pnl_total,
        "pnl_basis": float(comp.get("basis", 0.0)),
        "pnl_funding": float(comp.get("funding", 0.0)),
        "pnl_fees": float(comp.get("fees", 0.0)),  # fees + slippage (negative)
        "pnl_delivery": float(comp.get("delivery", 0.0)),
        "pnl_liquidation": float(comp.get("liquidation", 0.0)),
        "n_liquidations": int(len(_events_of(result, "LIQUIDATION"))),
        "n_expiries": int(len(_events_of(result, "EXPIRY"))),
        "n_rolls": int(len(_events_of(result, "ROLL"))),
        "peak_utilization": float(result.margin_stats.get("peak_utilization", NAN)),
    }


def _window_stats(result, mask: np.ndarray) -> dict:
    """Metrics restricted to the contiguous bars selected by `mask`."""
    eq, cap = result.equity, float(result.capital)
    pos = np.flatnonzero(mask)
    if len(pos) == 0:
        return {
            "n_days": 0, "pnl": NAN, "ret_on_capital": NAN, "sharpe": NAN, "max_dd": NAN,
            "pnl_basis": NAN, "pnl_funding": NAN, "pnl_costs": NAN, "n_entries": 0, "n_round_trips": 0,
            "time_in_position": NAN, "n_liquidations": 0,
        }
    j0, j1 = int(pos[0]), int(pos[-1])
    start_val = float(eq.iloc[j0 - 1]) if j0 > 0 else cap
    sub = eq.iloc[j0: j1 + 1]
    t0, t1 = sub.index[0], sub.index[-1]
    comp = result.pnl_components.iloc[j0: j1 + 1].sum()
    trades, events = result.trades, result.events
    closed = trades[(trades["exit_reason"] != "OPEN") & (trades["exit_ts"] >= t0) & (trades["exit_ts"] <= t1)]
    entries = trades[(trades["entry_ts"] >= t0) & (trades["entry_ts"] <= t1)]
    n_liq = 0
    if len(events):
        n_liq = int(((events["event"] == "LIQUIDATION") & (events["ts"] >= t0) & (events["ts"] <= t1)).sum())
    pnl = float(sub.iloc[-1] - start_val)
    return {
        "n_days": int(bar_day(sub.index).nunique()),
        "pnl": pnl,
        "ret_on_capital": pnl / cap,
        "sharpe": sharpe(daily_pnl(sub, initial=start_val)),
        "max_dd": max_drawdown(sub, start=start_val),
        "pnl_basis": float(comp["basis"]),
        "pnl_funding": float(comp["funding"]),
        "pnl_costs": float(comp["fees"] + comp["delivery"] + comp["liquidation"]),
        "n_entries": int(len(entries)),
        "n_round_trips": int(len(closed)),
        "time_in_position": float((result.position.iloc[j0: j1 + 1] != 0).mean()),
        "n_liquidations": n_liq,
    }


def yearly_table(result) -> pd.DataFrame:
    """One row per UTC calendar year in the sample (index: year)."""
    years = bar_day(result.equity.index).year.to_numpy()
    rows = {int(y): _window_stats(result, years == y) for y in np.unique(years)}
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "year"
    return df


def episode_table(result, episodes=EPISODES) -> pd.DataFrame:
    """One row per stress episode (inclusive UTC-day windows); episodes outside the sample have n_days 0."""
    days = bar_day(result.equity.index)
    rows = {}
    for name, start, end in episodes:
        d0, d1 = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
        stats = _window_stats(result, np.asarray((days >= d0) & (days <= d1)))
        span = int((d1 - d0) / pd.Timedelta(days=1)) + 1
        rows[name] = {"start": start, "end": end, "coverage": stats["n_days"] / span, **stats}
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "episode"
    return df


__all__ = [
    "EPISODES", "bar_day", "hourly_pnl", "daily_pnl", "sharpe", "max_drawdown",
    "drawdown_series", "worst_window", "years_covered", "summarize", "yearly_table", "episode_table",
]
