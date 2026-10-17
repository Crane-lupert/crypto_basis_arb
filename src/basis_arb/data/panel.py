"""Aligned 1h research panel: spot + perpetual + funding + current/next quarterly + r + r_prime.

Schema contract: basis_arb.types.PANEL_REQUIRED_COLUMNS and docs/strategy_spec.md section 2.
Row ``ts`` holds the bar that ENDS at ts (it covers [ts - 1h, ts)), so funding stamps
(00/08/16 UTC) and quarterly expiries (08:00 UTC) fall exactly on the grid.

Rules implemented here
----------------------
* Grid: complete 1h UTC range from the first ts where BOTH spot and perp have a close to the last
  ts where both have a bar (leading rows without a spot or perp close are dropped).
* Gaps: spot/perp closes are forward-filled over at most ``max_ffill_hours`` (default 5) missing
  bars; high/low stay NaN on filled rows (no bar existed: the model treats NaN spot_high/perp_high
  as an exchange-outage row with no decision). Every filled row is logged at INFO. A longer gap
  raises ValueError.
* funding_rate: NaN except at the (hour-floored) stamps present in the funding history.
* Quarterlies: a contract is eligible at ts iff it has printed its first bar (listed <= ts) and
  ts < expiry; cq = the eligible contract with the nearest expiry, nq = the next one. At
  ts == expiry the old contract is already gone. Close/high/low are NaN where that contract had
  no bar; symbol is None (expiry NaT, dte NaN) where no eligible contract exists.
* r_usd: FRED DTB3 / 100; the observation for date d applies from (d + 1 day) 00:00 UTC and is
  forward-filled (rows before the first applicable observation, if any, take that observation so the
  column is finite everywhere); y_coin from config.RateInputs.

CLI:  python -m basis_arb.data.panel --root BTCUSDT --start 2020-01-01
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from basis_arb.config import DAYS_PER_YEAR, RateInputs
from basis_arb.data import binance, cache, rates
from basis_arb.types import PANEL_REQUIRED_COLUMNS, validate_panel

logger = logging.getLogger(__name__)

QUARTERLY_FIRST_EXPIRY = date(2021, 3, 1)  # Binance USD-M quarterlies begin with the 2021-03 contract
MAX_FFILL_HOURS = 5.0  # decision 2026-10-18: covers the 4-5h Binance spot outages of 2020-2021
_END_LAG_WARN = pd.Timedelta(hours=3)  # warn when the data end well before the requested end
_HOUR = pd.Timedelta(hours=1)
_DAY = pd.Timedelta(days=1)
_NAT = np.iinfo(np.int64).min

Quarterlies = Mapping[str, "tuple[datetime | pd.Timestamp, pd.DataFrame]"]
KlineGetter = Callable[[str, str, int, int], pd.DataFrame]


# --- time helpers ------------------------------------------------------------------------------
def _to_utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.as_unit("ns")


def _floor(ts: pd.Timestamp, step: pd.Timedelta) -> pd.Timestamp:
    return pd.Timestamp((ts.value // step.value) * step.value, unit="ns", tz="UTC")


def _ceil(ts: pd.Timestamp, step: pd.Timedelta) -> pd.Timestamp:
    return pd.Timestamp(-(-ts.value // step.value) * step.value, unit="ns", tz="UTC")


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.value // 1_000_000)


def _check_panel_step(step: pd.Timedelta, interval: str) -> None:
    if step > _HOUR or _HOUR.value % step.value != 0:
        raise ValueError(
            f"panel interval must divide 1h so funding stamps and expiries fall on the grid; got {interval!r}"
        )


# --- pure building blocks ----------------------------------------------------------------------
def _bars_by_end(klines: pd.DataFrame | None, step: pd.Timedelta, name: str) -> pd.DataFrame:
    """Kline frame (open_time keyed) -> close/high/low indexed by bar-END time ts = open_time + step."""
    cols = ("close", "high", "low")
    if klines is None or len(klines) == 0:
        return pd.DataFrame(
            {c: pd.Series([], dtype="float64") for c in cols}, index=pd.DatetimeIndex([], tz="UTC", name="ts")
        )
    ts = pd.DatetimeIndex(klines["open_time"]).tz_convert("UTC").as_unit("ns") + step
    bars = pd.DataFrame({c: klines[c].to_numpy(dtype="float64") for c in cols}, index=ts.rename("ts"))
    bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    misaligned = (bars.index.asi8 % step.value) != 0
    if misaligned.any():
        raise ValueError(
            f"{name}: {int(misaligned.sum())} bar(s) not aligned to the {step} grid "
            f"(first: {bars.index[misaligned][0]})"
        )
    return bars


def _nan_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start position, length) of each run of True in ``mask``."""
    padded = np.concatenate(([False], np.asarray(mask, dtype=bool), [False])).astype(np.int8)
    d = np.diff(padded)
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    return [(int(s), int(e - s)) for s, e in zip(starts, ends)]


def _fill_short_gaps(close: pd.Series, name: str, step: pd.Timedelta, max_ffill_hours: float) -> pd.Series:
    """Forward-fill runs of missing closes of at most ``max_ffill_hours``; raise on a longer run."""
    runs = _nan_runs(close.isna().to_numpy())
    if not runs:
        return close
    limit = int(pd.Timedelta(hours=max_ffill_hours) // step)

    def hours(n: int) -> float:
        return n * step / _HOUR

    too_long = [(close.index[i], n) for i, n in runs if n > limit]
    if too_long:
        detail = "; ".join(f"{ts:%Y-%m-%d %H:%M} UTC +{hours(n):g}h" for ts, n in too_long)
        raise ValueError(
            f"{name}_close has {len(too_long)} gap(s) longer than the {max_ffill_hours:g}h forward-fill limit "
            f"(first missing bar end, gap length): {detail}. The panel contract needs a non-null close on "
            f"every grid row; inspect the source data, or rebuild with a larger max_ffill_hours "
            f"(CLI --max-ffill-hours), which is logged."
        )
    over_default = [n for _, n in runs if hours(n) > MAX_FFILL_HOURS]
    detail = ", ".join(f"{close.index[i]:%Y-%m-%d %H:%M}+{hours(n):g}h" for i, n in runs)
    logger.warning(
        "%s_close: forward-filled %d gap(s), %d bar(s) in total, %d gap(s) longer than the default %gh "
        "(high/low left NaN on filled rows): %s",
        name, len(runs), sum(n for _, n in runs), len(over_default), MAX_FFILL_HOURS, detail,
    )
    filled_rows = close.index[close.isna()]  # every NaN row is filled: no run exceeds the limit
    logger.info(
        "%s_close: forward-filled rows (%d, UTC): %s",
        name, len(filled_rows), ", ".join(f"{t:%Y-%m-%d %H:%M}" for t in filled_rows),
    )
    return close.ffill(limit=limit)


def _funding_on_grid(funding: pd.DataFrame | None, grid: pd.DatetimeIndex) -> pd.Series:
    """Funding rate at each stamp present in the history, NaN elsewhere."""
    out = pd.Series(np.nan, index=grid, name="funding_rate", dtype="float64")
    if funding is None or len(funding) == 0:
        logger.warning("no funding prints in the panel window; funding_rate is all-NaN")
        return out
    stamps = pd.DatetimeIndex(funding["funding_time"]).tz_convert("UTC").as_unit("ns")
    f = pd.Series(funding["funding_rate"].to_numpy(dtype="float64"), index=stamps)
    f = f[~f.index.duplicated(keep="last")]
    f = f[(f.index >= grid[0]) & (f.index <= grid[-1])]
    off = ~f.index.isin(grid)
    if off.any():
        raise ValueError(f"{int(off.sum())} funding stamp(s) fall off the panel grid (first: {f.index[off][0]})")
    out.loc[f.index] = f.to_numpy()
    return out


def select_contracts(grid: pd.DatetimeIndex, contracts: pd.DataFrame | None) -> pd.DataFrame:
    """Per ts: cq = eligible contract with the nearest expiry, nq = the next eligible one.

    ``contracts`` has one row per contract with columns ``symbol``, ``expiry`` (tz-aware UTC) and
    optionally ``listed`` (first ts at which the contract has a bar; missing/NaT = always listed).
    A contract is eligible at ts iff ``listed <= ts < expiry``: at ts == expiry it is already gone.
    Returns columns cq_symbol, cq_expiry, nq_symbol, nq_expiry (None / NaT where no contract).
    """
    n = len(grid)
    out = pd.DataFrame(index=grid)
    if contracts is None or len(contracts) == 0:
        for k in ("cq", "nq"):
            out[f"{k}_symbol"] = np.full(n, None, dtype=object)
            out[f"{k}_expiry"] = pd.Series(pd.NaT, index=grid, dtype="datetime64[ns, UTC]")
        return out
    expiry = pd.DatetimeIndex(pd.to_datetime(contracts["expiry"], utc=True)).as_unit("ns")
    if "listed" in contracts.columns:
        listed = pd.DatetimeIndex(pd.to_datetime(contracts["listed"], utc=True)).as_unit("ns").asi8
    else:
        listed = np.full(len(contracts), _NAT, dtype=np.int64)
    c = pd.DataFrame(
        {"symbol": contracts["symbol"].astype(str).to_numpy(), "expiry_ns": expiry.asi8, "listed_ns": listed}
    )
    c = c.sort_values(["expiry_ns", "symbol"], kind="mergesort").reset_index(drop=True)
    if c["symbol"].duplicated().any():
        raise ValueError(f"duplicate contract symbols: {sorted(c.loc[c['symbol'].duplicated(), 'symbol'])}")
    if c["expiry_ns"].duplicated().any():
        raise ValueError("two contracts share an expiry; cq/nq would be ambiguous")
    t = grid.tz_convert("UTC").as_unit("ns").asi8[:, None]
    active = (t >= c["listed_ns"].to_numpy()[None, :]) & (t < c["expiry_ns"].to_numpy()[None, :])
    rank = np.cumsum(active, axis=1)
    count = rank[:, -1]
    symbols = c["symbol"].to_numpy(dtype=object)
    expiries = c["expiry_ns"].to_numpy(dtype=np.int64)
    for k, r in (("cq", 1), ("nq", 2)):
        has = count >= r
        pos = np.argmax(active & (rank == r), axis=1)
        out[f"{k}_symbol"] = np.where(has, symbols[pos], None)
        exp_ns = np.where(has, expiries[pos], _NAT).astype(np.int64)
        out[f"{k}_expiry"] = pd.Series(pd.DatetimeIndex(exp_ns.view("M8[ns]")).tz_localize("UTC"), index=grid)
    return out


def _quarterly_columns(quarterlies: Quarterlies, grid: pd.DatetimeIndex, step: pd.Timedelta) -> pd.DataFrame:
    """The 12 ``fut_cq_*`` / ``fut_nq_*`` columns on ``grid``."""
    bars: dict[str, pd.DataFrame] = {}
    rows = []
    for symbol, (expiry, klines) in quarterlies.items():
        exp = _to_utc(expiry)
        b = _bars_by_end(klines, step, symbol)
        b = b[b.index < exp]  # the bar ending AT expiry belongs to a contract that has left the panel
        if b.empty:
            continue
        bars[symbol] = b
        rows.append({"symbol": symbol, "expiry": exp, "listed": b.index[0]})
    sel = select_contracts(grid, pd.DataFrame(rows, columns=["symbol", "expiry", "listed"]))
    ts = pd.Series(grid, index=grid)
    out = pd.DataFrame(index=grid)
    for k in ("cq", "nq"):
        sym = sel[f"{k}_symbol"].to_numpy(dtype=object)
        vals = {f: np.full(len(grid), np.nan) for f in ("close", "high", "low")}
        for symbol, b in bars.items():
            m = sym == symbol
            if not m.any():
                continue
            sub = b.reindex(grid[m])
            for f, arr in vals.items():
                arr[m] = sub[f].to_numpy(dtype="float64")
        expiry = sel[f"{k}_expiry"]
        out[f"fut_{k}_symbol"] = sym
        out[f"fut_{k}_close"] = vals["close"]
        out[f"fut_{k}_high"] = vals["high"]
        out[f"fut_{k}_low"] = vals["low"]
        out[f"fut_{k}_expiry"] = expiry
        out[f"fut_{k}_dte_days"] = ((expiry - ts) / _DAY).astype("float64")
    return out


def assemble_panel(
    spot: pd.DataFrame,
    perp: pd.DataFrame,
    funding: pd.DataFrame | None,
    quarterlies: Quarterlies,
    r_usd: pd.Series | float,
    y_coin: float,
    *,
    start: Any = None,
    end: Any = None,
    interval: str = "1h",
    max_ffill_hours: float = MAX_FFILL_HOURS,
) -> pd.DataFrame:
    """Pure assembly of the panel from already-fetched frames (rules: module docstring).

    spot / perp / quarterly klines are frames as returned by ``binance.fetch_klines``; ``funding``
    as returned by ``binance.fetch_funding``; ``quarterlies`` maps symbol -> (expiry, klines).
    ``r_usd`` is the DTB3 decimal series (forward-filled onto the grid) or a constant.
    ``start`` / ``end`` bound the grid (first / last ts, inclusive).
    """
    step = pd.Timedelta(milliseconds=binance.interval_ms(interval))
    _check_panel_step(step, interval)
    s = _bars_by_end(spot, step, "spot")
    p = _bars_by_end(perp, step, "perp")
    if s.empty or p.empty:
        raise ValueError(f"no {'spot' if s.empty else 'perp'} bars in the requested window")
    first, last = max(s.index[0], p.index[0]), min(s.index[-1], p.index[-1])
    if start is not None:
        first = max(first, _ceil(_to_utc(start), step))
    if end is not None:
        last = min(last, _floor(_to_utc(end), step))
    if last < first:
        raise ValueError(f"spot and perp bars do not overlap in the requested window ({first} > {last})")
    grid = pd.date_range(first, last, freq=step, name="ts")
    spot_close, perp_close = s["close"].reindex(grid), p["close"].reindex(grid)
    if spot_close.isna().all() or perp_close.isna().all():
        raise ValueError(f"no {'spot' if spot_close.isna().all() else 'perp'} close inside {first} .. {last}")
    lead = max(spot_close.first_valid_index(), perp_close.first_valid_index())
    if lead > grid[0]:
        logger.info("dropping %d leading row(s) before %s without a spot or perp close", int((grid < lead).sum()), lead)
        grid = grid[grid >= lead]

    panel = pd.DataFrame(index=grid)
    for name, bars in (("spot", s), ("perp", p)):
        panel[f"{name}_close"] = _fill_short_gaps(bars["close"].reindex(grid), name, step, max_ffill_hours)
        panel[f"{name}_high"] = bars["high"].reindex(grid)
        panel[f"{name}_low"] = bars["low"].reindex(grid)
    panel["funding_rate"] = _funding_on_grid(funding, grid)
    fut = _quarterly_columns(quarterlies, grid, step)
    for col in fut.columns:
        panel[col] = fut[col]
    if isinstance(r_usd, pd.Series):
        r = rates.r_usd_on_grid(r_usd, grid)
        lead = r.isna()  # forward-filled, so NaN can only precede the first applicable DTB3 observation
        if lead.any():
            first_obs = r_usd.dropna().sort_index()
            if first_obs.empty:
                raise ValueError("DTB3 series has no observations")
            logger.warning(
                "r_usd: %d leading row(s) before the first DTB3 observation applies (%s) filled with that "
                "observation (contract: r_usd finite on every row)",
                int(lead.sum()), first_obs.index[0] + rates.PUBLICATION_LAG,
            )
            r = r.fillna(float(first_obs.iloc[0]))
        panel["r_usd"] = r
    else:
        panel["r_usd"] = float(r_usd)
    panel["y_coin"] = float(y_coin)
    panel = panel.loc[:, list(PANEL_REQUIRED_COLUMNS)]
    panel.index.name = "ts"
    validate_panel(panel)
    return panel


# --- fetch orchestration -----------------------------------------------------------------------
def _check_listed_calendar(root: str, generated: Sequence[datetime], listed: Sequence[tuple[str, datetime]]) -> None:
    """Every quarterly listed on exchangeInfo must be in the generated calendar with the same expiry."""
    gen = {binance.quarterly_symbol(root, e): _to_utc(e) for e in generated}
    if not listed:
        logger.warning("%s: exchangeInfo lists no quarterly contracts; calendar cross-check skipped", root)
        return
    for symbol, expiry in listed:
        if symbol not in gen:
            raise ValueError(
                f"{root}: listed quarterly {symbol} is not in the generated calendar "
                f"(last Friday of Mar/Jun/Sep/Dec, 08:00 UTC)"
            )
        if _to_utc(expiry) != gen[symbol]:
            raise ValueError(f"{symbol}: exchangeInfo deliveryDate {_to_utc(expiry)} != generated expiry {gen[symbol]}")
    logger.info("%s: exchangeInfo calendar cross-check ok (%s)", root, ", ".join(s for s, _ in listed))


def _fetch_quarterlies(
    root: str,
    generated: Sequence[datetime],
    t0: pd.Timestamp,
    t1: pd.Timestamp,
    step: pd.Timedelta,
    klines: KlineGetter,
) -> dict[str, tuple[datetime, pd.DataFrame]]:
    out: dict[str, tuple[datetime, pd.DataFrame]] = {}
    skipped: list[str] = []
    k0 = _ms(t0 - step)
    for expiry in generated:
        exp = _to_utc(expiry)
        symbol = binance.quarterly_symbol(root, expiry)
        if exp <= t0:
            skipped.append(f"{symbol} (expired before start)")
            continue
        try:
            bars = klines("um", symbol, k0, _ms(min(t1, exp) - step))
        except binance.BinanceInvalidSymbol:
            skipped.append(f"{symbol} (invalid symbol)")
            continue
        if bars.empty:
            skipped.append(f"{symbol} (no bars in window)")
            continue
        out[symbol] = (expiry, bars)
    if skipped:
        logger.info("%s: skipped %d quarterly symbol(s): %s", root, len(skipped), ", ".join(skipped))
    logger.info("%s: %d quarterly contract(s) with data: %s", root, len(out), ", ".join(out))
    have = [i for i, e in enumerate(generated) if binance.quarterly_symbol(root, e) in out]
    if have:
        holes = [binance.quarterly_symbol(root, generated[i]) for i in range(have[0], have[-1] + 1) if i not in have]
        if holes:
            # a hole would silently promote a farther contract to "current quarter"
            raise ValueError(f"{root}: quarterly calendar has contract(s) without data between listed ones: {holes}")
    return out


def _load_r_usd(rate_inputs: RateInputs | None) -> pd.Series | float:
    try:
        return rates.fetch_dtb3()
    except (requests.RequestException, RuntimeError, ValueError, OSError) as exc:
        fallback = (rate_inputs if rate_inputs is not None else RateInputs()).fallback_r_usd
        logger.error(
            "FRED DTB3 unavailable (%s: %s): r_usd is the CONSTANT fallback %.4f on every row "
            "(config.RateInputs.fallback_r_usd). Rebuild the panel once FRED is reachable.",
            type(exc).__name__, exc, fallback,
        )
        return float(fallback)


def build_panel(
    root: str,
    start: str = "2020-01-01",
    end: str | None = None,
    interval: str = "1h",
    client: binance.SupportsGet | None = None,
    *,
    data_dir: str | os.PathLike[str] | None = None,
    use_cache: bool = True,
    max_ffill_hours: float = MAX_FFILL_HOURS,
    rate_inputs: RateInputs | None = None,
) -> pd.DataFrame:
    """Fetch (through the parquet cache) and assemble the validated panel for ``root``.

    ``start`` / ``end`` are the first / last panel ts (UTC; end defaults to the last closed bar).
    Quarterlies: every generated expiry from 2021-03 to today + 2 years is tried; symbols with no
    data (not listed yet, or outside the window) are skipped and logged.
    """
    root = root.upper()
    step = pd.Timedelta(milliseconds=binance.interval_ms(interval))
    _check_panel_step(step, interval)
    t0 = _ceil(_to_utc(start), step)
    t1 = _floor(_to_utc(end) if end is not None else pd.Timestamp.now(tz="UTC"), step)
    if t1 < t0:
        raise ValueError(f"end {t1} is before start {t0}")
    http = client if client is not None else binance.BinanceClient()

    def klines(market: str, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        if use_cache:
            return cache.cached_klines(market, symbol, interval, start_ms, end_ms, client=http, data_dir=data_dir)
        return binance.fetch_klines(market, symbol, interval, start_ms, end_ms, client=http)

    logger.info("%s: building the %s panel %s -> %s", root, interval, t0, t1)
    k0, k1 = _ms(t0 - step), _ms(t1 - step)  # open times of the bars ending at t0 .. t1
    spot = klines("spot", root, k0, k1)
    perp = klines("um", root, k0, k1)
    f0, f1 = _ms(t0), _ms(t1 + _HOUR) - 1  # raw stamps run a few ms late: keep all that floor into [t0, t1]
    if use_cache:
        funding = cache.cached_funding(root, f0, f1, client=http, data_dir=data_dir)
    else:
        funding = binance.fetch_funding(root, f0, f1, client=http)
    generated = binance.quarterly_expiries(QUARTERLY_FIRST_EXPIRY)
    _check_listed_calendar(root, generated, binance.listed_quarterlies(root, client=http))
    quarterlies = _fetch_quarterlies(root, generated, t0, t1, step, klines)
    r_usd = _load_r_usd(rate_inputs)
    y_coin = rates.coin_yield(root, rate_inputs)
    panel = assemble_panel(
        spot, perp, funding, quarterlies, r_usd, y_coin,
        start=t0, end=t1, interval=interval, max_ffill_hours=max_ffill_hours,
    )
    if panel.index[-1] < t1 - _END_LAG_WARN:
        logger.warning(
            "%s: panel ends at %s, %s before the requested end %s", root, panel.index[-1], t1 - panel.index[-1], t1
        )
    logger.info("%s: panel has %d rows, %s -> %s", root, len(panel), panel.index[0], panel.index[-1])
    return panel


# --- persistence -------------------------------------------------------------------------------
def panel_path(root: str, interval: str = "1h", data_dir: str | os.PathLike[str] | None = None) -> Path:
    return cache.resolve_data_dir(data_dir) / f"panel_{root.upper()}_{interval}.parquet"


def save_panel(
    df: pd.DataFrame, root: str, interval: str = "1h", data_dir: str | os.PathLike[str] | None = None
) -> Path:
    """Validate, then write ``data/panel_{ROOT}_{interval}.parquet`` atomically; returns the path."""
    validate_panel(df)
    path = panel_path(root, interval, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", index=True)
    os.replace(tmp, path)
    return path


def load_panel(root: str, interval: str = "1h", data_dir: str | os.PathLike[str] | None = None) -> pd.DataFrame:
    path = panel_path(root, interval, data_dir)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; build it with: python -m basis_arb.data.panel --root {root}")
    df = pd.read_parquet(path, engine="pyarrow")
    validate_panel(df)
    return df


# --- coverage report ---------------------------------------------------------------------------
def coverage_stats(df: pd.DataFrame) -> dict[str, Any]:
    """Coverage and sanity statistics of a panel (used by the CLI and the build log)."""
    stamps = df.index[df["funding_rate"].notna()]
    cq_ok, nq_ok = df["fut_cq_close"].notna(), df["fut_nq_close"].notna()
    contracts = sorted(set(df.loc[cq_ok, "fut_cq_symbol"]) | set(df.loc[nq_ok, "fut_nq_symbol"]))
    dte = df["fut_cq_dte_days"]
    basis = (np.log(df["fut_cq_close"] / df["spot_close"]) * DAYS_PER_YEAR / dte).replace([np.inf, -np.inf], np.nan)
    b_all, b_7 = basis.dropna(), basis[dte >= 7].dropna()

    def summary(s: pd.Series) -> dict[str, float] | None:
        return {"min": float(s.min()), "median": float(s.median()), "max": float(s.max())} if len(s) else None

    def share_nan(close: str, symbol: str) -> float | None:
        listed = df[symbol].notna()
        return float(df.loc[listed, close].isna().mean()) if listed.any() else None

    return {
        "rows": len(df),
        "first_ts": df.index[0],
        "last_ts": df.index[-1],
        "spot_filled_bars": int(df["spot_high"].isna().sum()),
        "perp_filled_bars": int(df["perp_high"].isna().sum()),
        "funding_prints": len(stamps),
        "first_funding": stamps[0] if len(stamps) else None,
        "last_funding": stamps[-1] if len(stamps) else None,
        "quarterly_contracts": len(contracts),
        "quarterly_symbols": f"{contracts[0]} .. {contracts[-1]}" if contracts else None,
        "quarterly_first_ts": df.index[cq_ok][0] if cq_ok.any() else None,
        "quarterly_last_ts": df.index[cq_ok][-1] if cq_ok.any() else None,
        "fut_cq_close_nan_share": float(df["fut_cq_close"].isna().mean()),
        "fut_cq_close_nan_share_when_listed": share_nan("fut_cq_close", "fut_cq_symbol"),
        "fut_nq_close_nan_share_when_listed": share_nan("fut_nq_close", "fut_nq_symbol"),
        "cq_basis_ann": summary(b_all),
        "cq_basis_ann_dte_ge_7d": summary(b_7),
        "cq_basis_ann_dte_ge_7d_by_year": {
            int(y): {"median": float(g.median()), "max": float(g.max())} for y, g in b_7.groupby(b_7.index.year)
        },
        "r_usd_min": float(df["r_usd"].min()),
        "r_usd_max": float(df["r_usd"].max()),
        "y_coin": float(df["y_coin"].iloc[0]),
    }


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in value.items()) + "}"
    return str(value)


# --- CLI ---------------------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m basis_arb.data.panel", description="Build, validate and save the aligned research panel."
    )
    parser.add_argument("--root", required=True, help="underlying, e.g. BTCUSDT")
    parser.add_argument("--start", default="2020-01-01", help="first panel ts, UTC (default 2020-01-01)")
    parser.add_argument("--end", default=None, help="last panel ts, UTC (default: the last closed bar)")
    parser.add_argument("--interval", default="1h", help="bar interval; must divide 1h (default 1h)")
    parser.add_argument(
        "--max-ffill-hours", type=float, default=MAX_FFILL_HOURS,
        help=f"longest spot/perp gap to forward-fill (default {MAX_FFILL_HOURS:g}); longer gaps abort the build",
    )
    parser.add_argument("--data-dir", default=None, help="data directory (default: <repo>/data)")
    parser.add_argument("--no-cache", action="store_true", help="bypass the data/raw/ parquet cache")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        df = build_panel(
            args.root, start=args.start, end=args.end, interval=args.interval, data_dir=args.data_dir,
            use_cache=not args.no_cache, max_ffill_hours=args.max_ffill_hours,
        )
        validate_panel(df)
        path = save_panel(df, args.root, interval=args.interval, data_dir=args.data_dir)
    except (ValueError, binance.BinanceAPIError) as exc:
        logger.error("panel build failed: %s", exc)
        logger.debug("traceback", exc_info=True)
        return 2
    print(f"saved {path}")
    for key, value in coverage_stats(df).items():
        print(f"{key:38s} {_fmt(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
