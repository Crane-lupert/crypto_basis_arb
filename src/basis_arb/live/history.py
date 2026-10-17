"""Minimal read-only history for the live daemon: the recent 1h panel from keyless mainnet public data.

DUPLICATION (merge later): src/basis_arb/data/ is the canonical fetch + panel layer used by the
backtest. This module re-implements only what live/ needs, so the daemon does not depend on data/
while that layer is being written: kline pagination, funding prints, listed + recently expired
quarterlies, FRED DTB3 and the panel assembly (contract selection rule of strategy_spec.md
section 2). Once data/panel.py exists, recent_panel() should delegate to it, and a test should
assert that both produce the same rows on an overlapping window.

Panel conventions (strategy_spec.md section 2): index `ts` = bar END (bar covers [ts-1h, ts)); only
complete bars (ts <= floor(now)); funding at the hour-floored stamp row, NaN elsewhere; at `ts`,
cq = the listed quarterly with the nearest expiry > ts, nq = the one after it; r_usd = FRED DTB3/100
with observation date d applied from d 00:00 UTC onward; y_coin = RateInputs.coin_yield[base].
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from basis_arb.config import DELIVERY_HOUR_UTC, QUARTER_MONTHS, RateInputs
from basis_arb.live._http import HttpError, PublicClient
from basis_arb.live.state import from_ms, to_ms, utc
from basis_arb.types import validate_panel

log = logging.getLogger(__name__)

SPOT_BASE = "https://api.binance.com"
UM_BASE = "https://fapi.binance.com"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
HOUR_MS = 3_600_000
DEFAULT_HOURS = 24 * 45  # 90 funding prints at 8h = 30 days, plus margin for the state machines
QUARTERLY_TYPES = ("CURRENT_QUARTER", "NEXT_QUARTER")
KLINE_COLUMNS = ("close", "high", "low")


def base_asset(root: str) -> str:
    for quote in ("USDT", "USDC"):
        if root.endswith(quote) and len(root) > len(quote):
            return root[: -len(quote)]
    raise ValueError(f"cannot derive the base asset of {root!r}")


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=float) for c in KLINE_COLUMNS},
                        index=pd.DatetimeIndex([], tz="UTC", name="ts"))


def fetch_klines(client: PublicClient, url: str, symbol: str, start: pd.Timestamp, end: pd.Timestamp,
                 limit: int) -> pd.DataFrame:
    """1h klines whose bar END lies in (start, end], paginated; index ts = open time + 1h."""
    cur, last_open = to_ms(start), to_ms(end) - HOUR_MS
    rows: list = []
    while cur <= last_open:
        page = client.get_json(url, {"symbol": symbol, "interval": "1h", "startTime": cur,
                                     "endTime": last_open, "limit": limit})
        if not isinstance(page, list):
            raise HttpError(url, 200, f"unexpected kline payload for {symbol}")
        if not page:
            break
        rows.extend(page)
        nxt = int(page[-1][0]) + HOUR_MS
        if nxt <= cur or len(page) < limit:
            break
        cur = nxt
    if not rows:
        return _empty_bars()
    idx = pd.DatetimeIndex(pd.to_datetime([int(r[0]) + HOUR_MS for r in rows], unit="ms", utc=True), name="ts")
    df = pd.DataFrame({"close": [float(r[4]) for r in rows], "high": [float(r[2]) for r in rows],
                       "low": [float(r[3]) for r in rows]}, index=idx)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[(df.index > start) & (df.index <= end)]


def funding_prints(root: str, start_ms: int, end_ms: int | None = None,
                   client: PublicClient | None = None) -> pd.DataFrame:
    """Settled funding prints with fundingTime in [start_ms, end_ms]; index = stamp floored to the hour.

    Columns: rate (decimal per period), mark_price (mark used by the exchange), funding_time_ms (raw).
    """
    client = client or PublicClient()
    url = UM_BASE + "/fapi/v1/fundingRate"
    out: list = []
    cur = int(start_ms)
    while True:
        params = {"symbol": root, "startTime": cur, "limit": 1000}
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        page = client.get_json(url, params)
        if not isinstance(page, list):
            raise HttpError(url, 200, "unexpected fundingRate payload")
        if not page:
            break
        out.extend(page)
        nxt = int(page[-1]["fundingTime"]) + 1
        if len(page) < 1000 or nxt <= cur:
            break
        cur = nxt
    if not out:
        return pd.DataFrame({"rate": pd.Series(dtype=float), "mark_price": pd.Series(dtype=float),
                             "funding_time_ms": pd.Series(dtype="int64")},
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    times = [int(r["fundingTime"]) for r in out]
    marks = []
    for r in out:
        try:
            marks.append(float(r.get("markPrice") or "nan"))
        except ValueError:
            marks.append(np.nan)
    idx = pd.DatetimeIndex(pd.to_datetime(times, unit="ms", utc=True), name="ts").floor("h")
    df = pd.DataFrame({"rate": [float(r["fundingRate"]) for r in out], "mark_price": marks,
                       "funding_time_ms": times}, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index()


@dataclass(frozen=True)
class Quarterly:
    symbol: str
    expiry: pd.Timestamp
    status: str = ""


def parse_quarterlies(exchange_info: dict, root: str) -> list[Quarterly]:
    """CURRENT_QUARTER / NEXT_QUARTER contracts of `root` from a /fapi/v1/exchangeInfo payload, by expiry."""
    out = []
    for s in exchange_info.get("symbols", []):
        ctype = str(s.get("contractType", "")).split(" ")[0]
        if s.get("pair") != root or ctype not in QUARTERLY_TYPES:
            continue
        out.append(Quarterly(symbol=str(s["symbol"]), expiry=from_ms(int(s["deliveryDate"])),
                             status=str(s.get("status", ""))))
    return sorted(out, key=lambda q: q.expiry)


def listed_quarterlies(root: str, client: PublicClient | None = None) -> list[Quarterly]:
    client = client or PublicClient()
    return parse_quarterlies(client.get_json(UM_BASE + "/fapi/v1/exchangeInfo"), root)


def quarter_expiry(year: int, month: int) -> pd.Timestamp:
    """08:00 UTC on the last Friday of (year, month)."""
    last = pd.Timestamp(year=year, month=month, day=calendar.monthrange(year, month)[1],
                        hour=DELIVERY_HOUR_UTC, tz="UTC")
    return last - pd.Timedelta(days=(last.weekday() - 4) % 7)


def quarterly_symbol(root: str, expiry: pd.Timestamp) -> str:
    return f"{root}_{expiry:%y%m%d}"


def expired_quarterlies(root: str, start: pd.Timestamp, before: pd.Timestamp) -> list[Quarterly]:
    """Quarterlies generated by the last-Friday rule with start < expiry < before (no longer in exchangeInfo)."""
    out = []
    for year in range(start.year, before.year + 1):
        for month in QUARTER_MONTHS:
            e = quarter_expiry(year, month)
            if start < e < before:
                out.append(Quarterly(symbol=quarterly_symbol(root, e), expiry=e, status="EXPIRED"))
    return out


def fred_dtb3(client: PublicClient, start: pd.Timestamp) -> pd.Series:
    """DTB3 observations from `start` as annual decimals, indexed by observation date (00:00 UTC)."""
    text = client.get_text(FRED_CSV, {"id": "DTB3", "cosd": f"{start:%Y-%m-%d}"})
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines or "DTB3" not in lines[0]:
        raise ValueError("unexpected FRED CSV header")
    dates, vals = [], []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) != 2 or parts[1].strip() in ("", "."):
            continue
        dates.append(pd.Timestamp(parts[0].strip(), tz="UTC"))
        vals.append(float(parts[1]) / 100.0)
    if not vals:
        raise ValueError("FRED DTB3 returned no observations")
    s = pd.Series(vals, index=pd.DatetimeIndex(dates, name="date"), dtype=float).sort_index()
    return s[~s.index.duplicated(keep="last")]


def r_usd_series(index: pd.DatetimeIndex, client: PublicClient, rates: RateInputs) -> tuple[pd.Series, str]:
    """Forward-filled DTB3/100 on `index` (no look-ahead); the configured fallback if FRED cannot be read."""
    try:
        obs = fred_dtb3(client, index[0] - pd.Timedelta(days=30))
        r = obs.reindex(index, method="ffill")
        if r.isna().any():
            log.warning("FRED DTB3 has no observation before %s; fallback r_usd=%.4f on %d rows",
                        index[0], rates.fallback_r_usd, int(r.isna().sum()))
            r = r.fillna(rates.fallback_r_usd)
        return r.astype(float), "fred"
    except (HttpError, ValueError) as exc:
        log.warning("FRED DTB3 unavailable (%s); using fallback r_usd=%.4f", exc, rates.fallback_r_usd)
        return pd.Series(rates.fallback_r_usd, index=index, dtype=float), "fallback"


def assemble_panel(spot: pd.DataFrame, perp: pd.DataFrame, funding: pd.DataFrame | pd.Series | None,
                   contracts: list[tuple[Quarterly, pd.DataFrame]], r_usd: pd.Series,
                   y_coin: float) -> pd.DataFrame:
    """Pure assembly of the panel contract (types.PANEL_REQUIRED_COLUMNS) from fetched parts."""
    idx = spot.index.intersection(perp.index).sort_values()
    if len(idx) == 0:
        raise ValueError("no overlapping spot/perp bars")
    idx = pd.DatetimeIndex(idx, name="ts")
    df = pd.DataFrame(index=idx)
    for name, src in (("spot", spot), ("perp", perp)):
        for col in KLINE_COLUMNS:
            df[f"{name}_{col}"] = src[col].reindex(idx).astype(float)
    fr = pd.Series(np.nan, index=idx, dtype=float)
    if funding is not None and len(funding):
        rate = funding["rate"] if isinstance(funding, pd.DataFrame) else funding
        rate = rate[rate.index.isin(idx)]
        fr.loc[rate.index] = rate.to_numpy(dtype=float)
    df["funding_rate"] = fr

    ordered = sorted(contracts, key=lambda c: c[0].expiry)
    first_bar = [kl.index.min() if len(kl) else None for _, kl in ordered]
    prices = [{c: kl[c].reindex(idx).to_numpy(dtype=float) for c in KLINE_COLUMNS} for _, kl in ordered]
    n = len(idx)
    cols = {k: {"symbol": [None] * n, "close": np.full(n, np.nan), "high": np.full(n, np.nan),
                "low": np.full(n, np.nan), "expiry": [pd.NaT] * n, "dte": np.full(n, np.nan)}
            for k in ("cq", "nq")}
    for i, ts in enumerate(idx):
        active = [j for j, (q, _) in enumerate(ordered)
                  if first_bar[j] is not None and first_bar[j] <= ts < q.expiry]
        for k, j in zip(("cq", "nq"), active[:2]):
            q, c = ordered[j][0], cols[k]
            c["symbol"][i] = q.symbol
            for col in KLINE_COLUMNS:
                c[col][i] = prices[j][col][i]
            c["expiry"][i] = q.expiry
            c["dte"][i] = (q.expiry - ts) / pd.Timedelta(days=1)
    for k in ("cq", "nq"):
        c = cols[k]
        df[f"fut_{k}_symbol"] = pd.Series(c["symbol"], index=idx, dtype=object)
        for col in KLINE_COLUMNS:
            df[f"fut_{k}_{col}"] = c[col]
        df[f"fut_{k}_expiry"] = pd.Series(pd.to_datetime(c["expiry"], utc=True), index=idx).astype(
            "datetime64[ns, UTC]")
        df[f"fut_{k}_dte_days"] = c["dte"]
    df["r_usd"] = r_usd.reindex(idx).astype(float).to_numpy()
    df["y_coin"] = float(y_coin)
    validate_panel(df)
    return df


def recent_panel(root: str, hours: int = DEFAULT_HOURS, client: PublicClient | None = None,
                 now=None, rates: RateInputs | None = None) -> pd.DataFrame:
    """The last `hours` complete 1h bars of the panel for `root` (passes types.validate_panel).

    `panel.attrs["r_usd_source"]` is "fred" or "fallback".
    """
    client = client or PublicClient()
    rates = rates or RateInputs()
    base = base_asset(root)
    if base not in rates.coin_yield:
        raise ValueError(f"no coin yield configured for {base} (config.RateInputs.coin_yield)")
    now = utc(now)
    end = now.floor("h")
    start = end - pd.Timedelta(hours=int(hours))

    spot = fetch_klines(client, SPOT_BASE + "/api/v3/klines", root, start, end, 1000)
    perp = fetch_klines(client, UM_BASE + "/fapi/v1/klines", root, start, end, 1500)
    funding = funding_prints(root, to_ms(start), to_ms(now), client)
    funding = funding[(funding.index > start) & (funding.index <= end)]

    listed = listed_quarterlies(root, client)
    first_listed = min((q.expiry for q in listed), default=end + pd.Timedelta(days=366))
    contracts: list[tuple[Quarterly, pd.DataFrame]] = []
    for q in expired_quarterlies(root, start, first_listed) + [q for q in listed if q.expiry > start]:
        try:
            kl = fetch_klines(client, UM_BASE + "/fapi/v1/klines", q.symbol, start, min(end, q.expiry), 1500)
        except HttpError as exc:
            if q.status != "EXPIRED":
                raise
            log.warning("skipping generated quarterly %s: %s", q.symbol, exc)
            continue
        contracts.append((q, kl))

    idx = spot.index.intersection(perp.index).sort_values()
    if len(idx) == 0:
        raise ValueError(f"no overlapping spot/perp bars for {root} in ({start}, {end}]")
    r_usd, source = r_usd_series(pd.DatetimeIndex(idx, name="ts"), client, rates)
    panel = assemble_panel(spot, perp, funding, contracts, r_usd, rates.coin_yield[base])
    expected = int(hours)
    if len(panel) < expected - 2:
        log.warning("%s panel has %d of %d hourly rows (gaps in spot/perp klines)", root, len(panel), expected)
    panel.attrs["r_usd_source"] = source
    panel.attrs["root"] = root
    return panel
