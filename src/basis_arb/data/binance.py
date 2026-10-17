"""Binance public REST fetchers: spot, USD-M perpetual and USD-M quarterly delivery contracts.

Endpoints, page limits and contract facts were verified on 2026-10-18 (docs/data_sources.md).
Everything here is keyless market data; no order or account endpoint is reachable from this module.

Conventions
-----------
* Request cursors are integer milliseconds since the epoch; returned times are tz-aware UTC.
* Kline frames are keyed by ``open_time``: a bar covers ``[open_time, open_time + interval)``.
  Only CLOSED bars are returned (the bar still forming at fetch time is dropped).
* Fail loudly: connection errors and 5xx are retried with exponential backoff, 429/418 sleep for
  ``Retry-After``, any other non-200 raises. A fetch never returns a silently truncated frame.
* Every fetcher takes ``client=`` (anything with ``get(base, path, params)``), so tests can inject
  a fake server without network access.
"""
from __future__ import annotations

import logging
import time
from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol

import pandas as pd
import requests

from basis_arb.config import DELIVERY_HOUR_UTC, QUARTER_MONTHS

logger = logging.getLogger(__name__)

SPOT_BASE = "https://api.binance.com"
UM_BASE = "https://fapi.binance.com"

Market = Literal["spot", "um"]

# market -> (base URL, path, max rows per call)
KLINE_ENDPOINTS: dict[str, tuple[str, str, int]] = {
    "spot": (SPOT_BASE, "/api/v3/klines", 1000),
    "um": (UM_BASE, "/fapi/v1/klines", 1500),
}
FUNDING_PATH = "/fapi/v1/fundingRate"
FUNDING_LIMIT = 1000
PREMIUM_INDEX_PATH = "/fapi/v1/premiumIndexKlines"
PREMIUM_INDEX_LIMIT = 1500
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
QUARTERLY_CONTRACT_TYPES = ("CURRENT_QUARTER", "NEXT_QUARTER")
INVALID_SYMBOL_CODE = -1121

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

KLINE_COLUMNS: tuple[str, ...] = (
    "open_time", "open", "high", "low", "close", "volume", "quote_volume", "trades",
)
FUNDING_COLUMNS: tuple[str, ...] = ("funding_time", "funding_rate", "mark_price")

# Funding stamps arrive 0-47 ms after the hour in the 2019-09..2026-10 history (checked
# 2026-10-18); a stamp more than this far past the hour is unexpected and logged loudly.
FUNDING_STAMP_TOLERANCE_MS = 60_000


# --- errors ------------------------------------------------------------------------------------
class BinanceAPIError(RuntimeError):
    """A Binance request failed for good (non-retryable status, or the retry budget is spent)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        url: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.url = url
        self.params = dict(params or {})


class BinanceInvalidSymbol(BinanceAPIError):
    """HTTP 400 with code -1121: the symbol does not exist (e.g. a quarterly not listed yet)."""


class BinanceRateLimitError(BinanceAPIError):
    """HTTP 429/418 persisted past the retry budget, or Retry-After exceeds ``max_wait_s``."""


class SupportsGet(Protocol):
    def get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any: ...


# --- client ------------------------------------------------------------------------------------
class BinanceClient:
    """Thin GET client over a ``requests.Session`` with retry, backoff and rate-limit handling."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        max_wait_s: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.max_wait_s = max_wait_s
        self._sleep = sleep

    def close(self) -> None:
        self.session.close()

    def get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET ``base + path`` and return the decoded JSON body; raise on any final failure."""
        url = base.rstrip("/") + path
        query = dict(params or {})
        failures = 0
        while True:
            try:
                resp = self.session.get(url, params=query, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                failures += 1
                self._backoff_or_raise(failures, f"{type(exc).__name__} ({exc})", url, query, None, exc)
                continue
            status = int(resp.status_code)
            if status == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise BinanceAPIError(
                        f"HTTP 200 with a non-JSON body from {url}", status=200, url=url, params=query
                    ) from exc
            if status in (418, 429):
                failures += 1
                wait = _retry_after_seconds(resp)
                if wait is None:
                    wait = self._backoff_delay(failures)
                if failures > self.max_retries or wait > self.max_wait_s:
                    raise BinanceRateLimitError(
                        f"HTTP {status} (rate limit) from {url}: Retry-After {wait:.0f}s, attempt "
                        f"{failures} (max_retries={self.max_retries}, max_wait_s={self.max_wait_s:.0f})",
                        status=status, url=url, params=query,
                    )
                logger.warning(
                    "HTTP %d (rate limit) from %s; sleeping %.1fs (retry %d/%d)",
                    status, url, wait, failures, self.max_retries,
                )
                self._sleep(wait)
                continue
            if status >= 500:
                failures += 1
                self._backoff_or_raise(failures, f"HTTP {status}", url, query, status, None)
                continue
            code, msg = _error_body(resp)
            err = BinanceInvalidSymbol if code == INVALID_SYMBOL_CODE else BinanceAPIError
            raise err(
                f"HTTP {status} from {url} params={query}: code={code} msg={msg}",
                status=status, code=code, url=url, params=query,
            )

    def _backoff_delay(self, failures: int) -> float:
        return min(self.max_backoff_s, self.backoff_s * 2.0 ** (failures - 1))

    def _backoff_or_raise(
        self,
        failures: int,
        what: str,
        url: str,
        params: Mapping[str, Any],
        status: int | None,
        cause: BaseException | None,
    ) -> None:
        if failures > self.max_retries:
            raise BinanceAPIError(
                f"{what} from {url} params={dict(params)}; giving up after {failures} attempts",
                status=status, url=url, params=params,
            ) from cause
        delay = self._backoff_delay(failures)
        logger.warning("%s from %s; retry %d/%d in %.1fs", what, url, failures, self.max_retries, delay)
        self._sleep(delay)


def _retry_after_seconds(resp: Any) -> float | None:
    headers = getattr(resp, "headers", None) or {}
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _error_body(resp: Any) -> tuple[int | None, str]:
    try:
        body = resp.json()
    except ValueError:
        return None, str(getattr(resp, "text", "") or "")[:200]
    if not isinstance(body, dict):
        return None, str(body)[:200]
    try:
        code = int(body.get("code"))
    except (TypeError, ValueError):
        code = None
    return code, str(body.get("msg", ""))


# --- helpers -----------------------------------------------------------------------------------
def interval_ms(interval: str) -> int:
    """Length of a Binance kline interval in milliseconds (fixed-length intervals only)."""
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(f"unsupported interval {interval!r}; expected one of {sorted(INTERVAL_MS)}") from None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_ts(ms: int) -> pd.Timestamp:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC")


def _resolve(client: SupportsGet | None) -> SupportsGet:
    return client if client is not None else BinanceClient()


# --- klines ------------------------------------------------------------------------------------
def empty_klines() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": pd.Series([], dtype="datetime64[ns, UTC]"),
            **{c: pd.Series([], dtype="float64") for c in KLINE_COLUMNS[1:-1]},
            "trades": pd.Series([], dtype="int64"),
        }
    )


def parse_klines(rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """Binance kline arrays -> DataFrame[KLINE_COLUMNS] (prices/volumes float, trades int)."""
    if len(rows) == 0:
        return empty_klines()
    short = [i for i, r in enumerate(rows) if len(r) < 9]
    if short:
        raise ValueError(f"kline rows must have >= 9 fields; rows {short[:5]} are shorter")
    raw = pd.DataFrame(
        [list(r[:9]) for r in rows],
        columns=["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades"],
    )
    out = pd.DataFrame({"open_time": pd.to_datetime(raw["open_time"].astype("int64"), unit="ms", utc=True)})
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        out[col] = pd.to_numeric(raw[col], errors="raise").astype("float64")
    out["trades"] = pd.to_numeric(raw["trades"], errors="raise").astype("int64")
    return out


def _fetch_kline_like(
    base: str,
    path: str,
    limit: int,
    ident: Mapping[str, Any],
    interval: str,
    start_ms: int,
    end_ms: int | None,
    client: SupportsGet | None,
) -> pd.DataFrame:
    step = interval_ms(interval)
    http = _resolve(client)
    now = _now_ms()
    horizon = now if end_ms is None else min(int(end_ms), now)
    rows: list[Sequence[Any]] = []
    cursor = int(start_ms)
    pages = 0
    while cursor <= horizon:
        params: dict[str, Any] = {**ident, "interval": interval, "startTime": cursor, "limit": limit}
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        page = http.get(base, path, params)
        if not isinstance(page, list):
            raise BinanceAPIError(
                f"unexpected payload type {type(page).__name__} from {path}", url=base + path, params=params
            )
        pages += 1
        if not page:
            break
        rows.extend(page)
        last_open = int(page[-1][0])
        if end_ms is not None and last_open >= end_ms:
            break
        nxt = last_open + step
        if nxt <= cursor:
            raise BinanceAPIError(
                f"kline pagination made no progress at startTime={cursor} ({path} {dict(ident)})",
                url=base + path, params=params,
            )
        cursor = nxt
    df = parse_klines(rows)
    keep = df["open_time"] >= _ms_to_ts(start_ms)
    if end_ms is not None:
        keep &= df["open_time"] <= _ms_to_ts(end_ms)
    closed = (df["open_time"] + pd.Timedelta(milliseconds=step)) <= _ms_to_ts(now)
    n_open = int((keep & ~closed).sum())
    if n_open:
        logger.debug("%s %s: dropped %d bar(s) still forming at fetch time", path, dict(ident), n_open)
    df = df[keep & closed].drop_duplicates("open_time", keep="last").sort_values("open_time")
    df = df.reset_index(drop=True)
    logger.debug("%s %s %s: %d page(s), %d closed bar(s)", path, dict(ident), interval, pages, len(df))
    return df


def fetch_klines(
    market: Market,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int | None = None,
    *,
    client: SupportsGet | None = None,
) -> pd.DataFrame:
    """Closed klines with ``start_ms <= open_time <= end_ms`` (end defaults to now).

    market "spot" -> api.binance.com /api/v3/klines (1000 rows/call); "um" -> fapi.binance.com
    /fapi/v1/klines (1500 rows/call), which also serves quarterlies such as ``BTCUSDT_261225``
    (expired contracts included). Pagination advances ``startTime`` past the last bar received.
    """
    if market not in KLINE_ENDPOINTS:
        raise ValueError(f"market must be one of {sorted(KLINE_ENDPOINTS)}, got {market!r}")
    base, path, limit = KLINE_ENDPOINTS[market]
    return _fetch_kline_like(base, path, limit, {"symbol": symbol}, interval, start_ms, end_ms, client)


def fetch_premium_index_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int | None = None,
    *,
    client: SupportsGet | None = None,
) -> pd.DataFrame:
    """Premium-index klines (/fapi/v1/premiumIndexKlines); same frame shape as ``fetch_klines``."""
    return _fetch_kline_like(
        UM_BASE, PREMIUM_INDEX_PATH, PREMIUM_INDEX_LIMIT, {"symbol": symbol}, interval, start_ms, end_ms, client
    )


# --- funding -----------------------------------------------------------------------------------
def empty_funding() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "funding_time": pd.Series([], dtype="datetime64[ns, UTC]"),
            "funding_rate": pd.Series([], dtype="float64"),
            "mark_price": pd.Series([], dtype="float64"),
        }
    )


def parse_funding(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """fundingRate records -> DataFrame[funding_time (floored to the hour, UTC), funding_rate, mark_price].

    Raw stamps are sometimes a few ms late (e.g. 1792252800003); flooring puts them on the 1h grid.
    Early records carry an empty ``markPrice``, which becomes NaN.
    """
    if len(rows) == 0:
        return empty_funding()
    raw = pd.to_datetime(pd.Series([int(r["fundingTime"]) for r in rows], dtype="int64"), unit="ms", utc=True)
    floored = raw.dt.floor("h")
    late_ms = (raw - floored) / pd.Timedelta(milliseconds=1)
    odd = late_ms > FUNDING_STAMP_TOLERANCE_MS
    if odd.any():
        logger.warning(
            "%d funding stamp(s) are more than %d ms past the hour (first: %s); placed at the floored hour",
            int(odd.sum()), FUNDING_STAMP_TOLERANCE_MS, raw[odd].iloc[0],
        )
    out = pd.DataFrame(
        {
            "funding_time": floored,
            "funding_rate": pd.to_numeric(pd.Series([r["fundingRate"] for r in rows]), errors="raise").astype(
                "float64"
            ),
            "mark_price": pd.to_numeric(pd.Series([r.get("markPrice") for r in rows]), errors="coerce").astype(
                "float64"
            ),
        }
    )
    dup = out["funding_time"].duplicated(keep=False)
    if dup.any():
        conflicting = out[dup].groupby("funding_time")["funding_rate"].nunique()
        conflicting = conflicting[conflicting > 1]
        if len(conflicting):
            logger.warning(
                "%d funding hour(s) have conflicting prints (first: %s); keeping the last record",
                len(conflicting), conflicting.index[0],
            )
    out = out.drop_duplicates("funding_time", keep="last").sort_values("funding_time")
    return out.reset_index(drop=True)


def fetch_funding(
    symbol: str,
    start_ms: int,
    end_ms: int | None = None,
    *,
    client: SupportsGet | None = None,
) -> pd.DataFrame:
    """Settled funding prints with raw ``start_ms <= fundingTime <= end_ms`` (/fapi/v1/fundingRate).

    These are the actual stamps, so any 8h/4h/1h interval regime is reproduced as it happened.
    """
    http = _resolve(client)
    horizon = _now_ms() if end_ms is None else int(end_ms)
    rows: list[Mapping[str, Any]] = []
    cursor = int(start_ms)
    while cursor <= horizon:
        params: dict[str, Any] = {"symbol": symbol, "startTime": cursor, "limit": FUNDING_LIMIT}
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        page = http.get(UM_BASE, FUNDING_PATH, params)
        if not isinstance(page, list):
            raise BinanceAPIError(
                f"unexpected payload type {type(page).__name__} from {FUNDING_PATH}",
                url=UM_BASE + FUNDING_PATH, params=params,
            )
        if not page:
            break
        rows.extend(page)
        last = int(page[-1]["fundingTime"])
        if last >= horizon:
            break
        if last + 1 <= cursor:
            raise BinanceAPIError(
                f"funding pagination made no progress at startTime={cursor} ({symbol})",
                url=UM_BASE + FUNDING_PATH, params=params,
            )
        cursor = last + 1
    lo, hi = int(start_ms), (None if end_ms is None else int(end_ms))
    rows = [r for r in rows if int(r["fundingTime"]) >= lo and (hi is None or int(r["fundingTime"]) <= hi)]
    return parse_funding(rows)


# --- quarterly contract calendar ---------------------------------------------------------------
def listed_quarterlies(root: str, *, client: SupportsGet | None = None) -> list[tuple[str, datetime]]:
    """Currently listed quarterlies for ``root`` from /fapi/v1/exchangeInfo, sorted by expiry.

    Returns ``[(symbol, expiry_utc), ...]`` for contractType CURRENT_QUARTER / NEXT_QUARTER, where
    the expiry is ``deliveryDate`` (ms) as a tz-aware UTC datetime.
    """
    info = _resolve(client).get(UM_BASE, EXCHANGE_INFO_PATH, None)
    symbols = info.get("symbols") if isinstance(info, dict) else None
    if not isinstance(symbols, list):
        raise BinanceAPIError("unexpected /fapi/v1/exchangeInfo payload (no symbols list)")
    out: list[tuple[str, datetime]] = []
    for s in symbols:
        if s.get("contractType") not in QUARTERLY_CONTRACT_TYPES:
            continue
        sym = str(s.get("symbol", ""))
        if s.get("pair", sym.split("_")[0]) != root:
            continue
        out.append((sym, datetime.fromtimestamp(int(s["deliveryDate"]) / 1000.0, tz=UTC)))
    return sorted(out, key=lambda x: (x[1], x[0]))


def last_friday(year: int, month: int) -> date:
    last_day = date(year, month, monthrange(year, month)[1])
    return last_day - timedelta(days=(last_day.weekday() - 4) % 7)


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):  # includes pd.Timestamp; must be checked before `date`
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + years, day=28)


def quarterly_expiries(
    start: date | datetime | str = date(2021, 3, 1),
    end: date | datetime | str | None = None,
) -> list[datetime]:
    """08:00 UTC on the last Friday of Mar/Jun/Sep/Dec with ``start <= expiry date <= end``.

    ``end`` defaults to today (UTC) + 2 years. From 2021-03 this reproduces the Binance USD-M
    quarterly calendar (BTCUSDT_210326 ... BTCUSDT_270326, cross-checked 2026-10-18).
    """
    lo = _as_date(start)
    hi = _as_date(end) if end is not None else add_years(datetime.now(UTC).date(), 2)
    out: list[datetime] = []
    for year in range(lo.year, hi.year + 1):
        for month in QUARTER_MONTHS:
            d = last_friday(year, month)
            if lo <= d <= hi:
                out.append(datetime(d.year, d.month, d.day, DELIVERY_HOUR_UTC, tzinfo=UTC))
    return out


def quarterly_symbol(root: str, expiry: date | datetime) -> str:
    """``BTCUSDT`` + 2026-12-25 -> ``BTCUSDT_261225``."""
    return f"{root}_{expiry:%y%m%d}"
