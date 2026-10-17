"""Performance metrics on a BacktestResult (docs/strategy_spec.md section 7, spec v1.0.2).

Conventions
-----------
* Bars are labeled by their close time, so the bar closing at 00:00 UTC (covering 23:00-24:00) is
  attributed to the previous UTC day (`bar_day`).
* Returns: hourly return = P&L_t / capital_{t-1}, where capital is the capital actually required by the open
  position (target capital while flat); daily return = sum of the day's hourly returns (no compounding).
* HEADLINE = excess of the risk-free rate: the T-bill accrues hourly at r_usd/8760 on capital_{t-1}, i.e.
  (mean r of the day)/365 x capital for a full day. Excess Sharpe = mean/std(ddof=1) x sqrt(365) of daily
  excess returns; annualized excess return = total excess return / years covered (= mean daily excess return
  x 365 when every day is complete). Raw figures come second.
* `sharpe` returns 0.0 for an identically zero series (e.g. P&L exactly equal to the risk-free accrual)
  and NaN for a constant non-zero series (undefined).
* `max_drawdown` returns a positive fraction of the running equity peak (0.05 = 5% peak-to-trough), with the
  starting capital included as the first point.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from basis_arb.config import HOURS_PER_YEAR

NAN = float("nan")
ZERO_TOL = 1e-12  # |values| below this are zero (returns are fractions of capital; P&L is in USDT)
FUNDING_INTEREST_PRINT = 0.0001  # Binance's 0.01% interest component: a print exactly at it = premium in the clamp band
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
    """Annualized Sharpe of a daily return (or P&L) series: mean / std(ddof=1) * sqrt(periods)."""
    x = pd.Series(daily, dtype=float).dropna().to_numpy()
    if len(x) < 2:
        return NAN
    mu, sd = float(x.mean()), float(x.std(ddof=1))
    if not math.isfinite(sd):
        return NAN
    if float(np.abs(x).max()) <= ZERO_TOL:
        return 0.0  # identically zero: no excess return and no risk
    if sd <= 1e-9 * abs(mu) or sd <= ZERO_TOL:
        return NAN  # constant non-zero series: Sharpe undefined
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
    """Worst rolling `days`-day sum of a daily series (NaN if the series is shorter than the window)."""
    d = pd.Series(daily, dtype=float)
    if len(d) < days:
        return NAN
    return float(d.rolling(days).sum().min())


def years_covered(index: pd.DatetimeIndex) -> float:
    if len(index) == 0:
        return 0.0
    hours = (index[-1] - index[0]) / _HOUR + 1.0
    return float(hours / HOURS_PER_YEAR)


# --- return frames -----------------------------------------------------------------------------
def hourly_frame(result) -> pd.DataFrame:
    """Per bar: pnl, capital_prev, ret, rf_ret, rf_pnl, excess_ret, excess_pnl."""
    eq = result.equity
    idx = eq.index
    cap = result.capital_t if len(result.capital_t) == len(eq) else pd.Series(result.capital, index=idx)
    cap_prev = cap.astype(float).shift(1)
    if len(cap_prev):
        cap_prev.iloc[0] = float(result.capital)
    r = result.r_usd if len(result.r_usd) == len(eq) else pd.Series(0.0, index=idx)
    pnl = hourly_pnl(eq, initial=result.capital)
    ret = pnl / cap_prev
    rf_ret = r.astype(float) / HOURS_PER_YEAR
    return pd.DataFrame({
        "pnl": pnl, "capital_prev": cap_prev, "ret": ret, "rf_ret": rf_ret, "rf_pnl": rf_ret * cap_prev,
        "excess_ret": ret - rf_ret, "excess_pnl": pnl - rf_ret * cap_prev,
    }, index=idx)


_DAILY_SUM_COLUMNS = ("pnl", "ret", "rf_ret", "rf_pnl", "excess_ret", "excess_pnl")


def daily_frame(result, hourly: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per UTC day (calendar-complete): sums of the hourly pnl / returns / risk-free accrual / excess."""
    h = hourly_frame(result) if hourly is None else hourly
    if h.empty:
        return pd.DataFrame(columns=list(_DAILY_SUM_COLUMNS), dtype=float)
    d = h[list(_DAILY_SUM_COLUMNS)].groupby(bar_day(h.index)).sum()
    full = pd.date_range(d.index[0], d.index[-1], freq="D")
    return d.reindex(full, fill_value=0.0)


# --- result-level tables -----------------------------------------------------------------------
def _events_of(result, kind: str) -> pd.DataFrame:
    ev = result.events
    return ev[ev["event"] == kind] if len(ev) else ev


def summarize(result) -> dict:
    """Headline metrics of one BacktestResult (JSON-friendly scalars; NaN where undefined).

    Headline: ann_excess_return, excess_sharpe (excess of the T-bill). Raw: ann_return_raw, sharpe_raw.
    """
    eq, cap = result.equity, float(result.capital)
    years = years_covered(eq.index)
    h = hourly_frame(result)
    d = daily_frame(result, h)
    comp = result.pnl_components.sum()
    pnl_total = float(eq.iloc[-1] - cap) if len(eq) else 0.0
    trades = result.trades
    closed = trades[trades["exit_reason"] != "OPEN"]
    traded_notional = float(sum(f.qty * f.price for f in result.fills))
    mean_cap = float(h["capital_prev"].mean()) if len(h) else cap
    ws = worst_window(d["ret"], 30) if len(d) else NAN
    ms = result.margin_stats
    return {
        "strategy": result.strategy,
        "root": result.root,
        "start": eq.index[0].isoformat() if len(eq) else None,
        "end": eq.index[-1].isoformat() if len(eq) else None,
        "n_bars": int(len(eq)),
        "years": years,
        "capital": cap,
        "mean_capital": mean_cap,
        # headline: excess of the risk-free rate
        "ann_excess_return": float(h["excess_ret"].sum() / years) if years > 0 else NAN,
        "excess_sharpe": sharpe(d["excess_ret"]),
        # raw (idle cash earns nothing)
        "ann_return_raw": float(h["ret"].sum() / years) if years > 0 else NAN,
        "sharpe_raw": sharpe(d["ret"]),
        "max_dd": max_drawdown(eq, start=cap),
        "worst_30d": ws,  # worst rolling 30-day sum of daily raw returns
        "n_round_trips": int(len(closed)),
        "n_open_trades": int(len(trades) - len(closed)),
        "hit_rate": float((closed["pnl_total"] > 0).mean()) if len(closed) else NAN,
        "avg_hold_hours": float(closed["hold_hours"].mean()) if len(closed) else NAN,
        "time_in_position_frac": float((result.position != 0).mean()) if len(result.position) else NAN,
        "turnover": traded_notional / mean_cap / years if years > 0 and mean_cap > 0 else NAN,
        "pnl_total": pnl_total,
        "excess_pnl_total": float(d["excess_pnl"].sum()) if len(d) else NAN,
        "rf_charge_total": float(d["rf_pnl"].sum()) if len(d) else NAN,
        "pnl_basis": float(comp.get("basis", 0.0)),
        "pnl_funding": float(comp.get("funding", 0.0)),
        "pnl_fees": float(comp.get("fees", 0.0)),  # fees + slippage (negative)
        "pnl_delivery": float(comp.get("delivery", 0.0)),
        "pnl_liquidation": float(comp.get("liquidation", 0.0)),
        "pnl_resize": float(comp.get("resize", 0.0)),
        "n_liquidations": int(len(_events_of(result, "LIQUIDATION"))),
        "n_resizes": int(len(_events_of(result, "RESIZE"))),
        "n_expiries": int(len(_events_of(result, "EXPIRY"))),
        "n_rolls": int(len(_events_of(result, "ROLL"))),
        "peak_utilization": float(ms.get("peak_utilization", NAN)),
        "peak_pair_utilization": float(ms.get("peak_pair_utilization", NAN)),
        "intrabar_breach_bars": int(ms.get("intrabar_breach_bars", 0)),
    }


WINDOW_COLUMNS = (
    "n_days", "pnl", "ret", "excess_ret", "sharpe_raw", "excess_sharpe", "max_dd", "pnl_basis", "pnl_funding",
    "pnl_costs", "n_entries", "n_round_trips", "time_in_position", "n_liquidations", "n_resizes",
)


def _window_stats(result, h: pd.DataFrame, d: pd.DataFrame, d0: pd.Timestamp, d1: pd.Timestamp) -> dict:
    """Metrics over the UTC days [d0, d1] (period sums, not annualized)."""
    days = bar_day(h.index)
    mask = np.asarray((days >= d0) & (days <= d1))
    pos = np.flatnonzero(mask)
    if len(pos) == 0:
        out = {k: NAN for k in WINDOW_COLUMNS}
        out.update({"n_days": 0, "n_entries": 0, "n_round_trips": 0, "n_liquidations": 0, "n_resizes": 0})
        return out
    eq, cap = result.equity, float(result.capital)
    j0, j1 = int(pos[0]), int(pos[-1])
    start_val = float(eq.iloc[j0 - 1]) if j0 > 0 else cap
    sub = eq.iloc[j0: j1 + 1]
    t0, t1 = sub.index[0], sub.index[-1]
    dsub = d.loc[(d.index >= d0) & (d.index <= d1)]
    comp = result.pnl_components.iloc[j0: j1 + 1].sum()
    trades, events = result.trades, result.events
    closed = trades[(trades["exit_reason"] != "OPEN") & (trades["exit_ts"] >= t0) & (trades["exit_ts"] <= t1)]
    entries = trades[(trades["entry_ts"] >= t0) & (trades["entry_ts"] <= t1)]
    n_liq = n_res = 0
    if len(events):
        inside = (events["ts"] >= t0) & (events["ts"] <= t1)
        n_liq = int((inside & (events["event"] == "LIQUIDATION")).sum())
        n_res = int((inside & (events["event"] == "RESIZE")).sum())
    costs = sum(float(comp.get(k, 0.0)) for k in ("fees", "delivery", "liquidation", "resize"))
    return {
        "n_days": int(bar_day(sub.index).nunique()),
        "pnl": float(h["pnl"].iloc[j0: j1 + 1].sum()),
        "ret": float(dsub["ret"].sum()),
        "excess_ret": float(dsub["excess_ret"].sum()),
        "sharpe_raw": sharpe(dsub["ret"]),
        "excess_sharpe": sharpe(dsub["excess_ret"]),
        "max_dd": max_drawdown(sub, start=start_val),
        "pnl_basis": float(comp.get("basis", 0.0)),
        "pnl_funding": float(comp.get("funding", 0.0)),
        "pnl_costs": costs,
        "n_entries": int(len(entries)),
        "n_round_trips": int(len(closed)),
        "time_in_position": float((result.position.iloc[j0: j1 + 1] != 0).mean()),
        "n_liquidations": n_liq,
        "n_resizes": n_res,
    }


def yearly_table(result) -> pd.DataFrame:
    """One row per UTC calendar year in the sample (index: year); returns are period sums."""
    h = hourly_frame(result)
    d = daily_frame(result, h)
    years = np.unique(bar_day(h.index).year.to_numpy())
    rows = {}
    for y in years:
        d0, d1 = pd.Timestamp(f"{y}-01-01", tz="UTC"), pd.Timestamp(f"{y}-12-31", tz="UTC")
        rows[int(y)] = _window_stats(result, h, d, d0, d1)
    df = pd.DataFrame.from_dict(rows, orient="index", columns=list(WINDOW_COLUMNS))
    df.index.name = "year"
    return df


def episode_table(result, episodes=EPISODES) -> pd.DataFrame:
    """One row per stress episode (inclusive UTC-day windows); episodes outside the sample have n_days 0."""
    h = hourly_frame(result)
    d = daily_frame(result, h)
    rows = {}
    for name, start, end in episodes:
        d0, d1 = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
        stats = _window_stats(result, h, d, d0, d1)
        span = int((d1 - d0) / pd.Timedelta(days=1)) + 1
        rows[name] = {"start": start, "end": end, "coverage": stats["n_days"] / span, **stats}
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "episode"
    return df


def funding_diagnostics(panel: pd.DataFrame) -> pd.DataFrame:
    """Per year of the print: number of funding prints, share exactly at 0.000100 (Binance's interest
    component, i.e. the average premium sat inside the clamp band), and the cadence histogram (hours since the
    previous print: 1h / 4h / 8h / other)."""
    fr = pd.to_numeric(panel["funding_rate"], errors="coerce").dropna()
    if fr.empty:
        return pd.DataFrame(columns=["n_prints", "share_at_0.0001", "n_1h", "n_4h", "n_8h", "n_other"])
    gap_h = fr.index.to_series().diff() / _HOUR
    year = fr.index.year
    rows = {}
    for y in np.unique(year):
        m = year == y
        g = gap_h[m].dropna()
        rows[int(y)] = {
            "n_prints": int(m.sum()),
            "share_at_0.0001": float(np.isclose(fr[m].to_numpy(), FUNDING_INTEREST_PRINT, rtol=0.0, atol=1e-12).mean()),
            "n_1h": int((g == 1).sum()),
            "n_4h": int((g == 4).sum()),
            "n_8h": int((g == 8).sum()),
            "n_other": int((~g.isin([1, 4, 8])).sum()),
        }
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "year"
    return df


__all__ = [
    "EPISODES", "WINDOW_COLUMNS", "bar_day", "hourly_pnl", "daily_pnl", "sharpe", "max_drawdown",
    "drawdown_series", "worst_window", "years_covered", "hourly_frame", "daily_frame", "summarize",
    "yearly_table", "episode_table", "funding_diagnostics",
]
