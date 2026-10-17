"""USD financing rate r (FRED DTB3) and the coin-yield constant r_prime used by the fair-value model.

r: 3-month T-bill secondary-market rate (discount basis), FRED series DTB3, annual decimal
   (value / 100). The observation for date d applies from (d + 1 day) 00:00 UTC onward, never from d
   itself (the H.15 release publishes it on the next business day; decision 2026-10-18), and is
   forward-filled onto the panel grid.
r_prime (y_coin): constant staking proxy per base asset from config.RateInputs.coin_yield.
"""
from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable

import pandas as pd
import requests

from basis_arb.config import RateInputs

logger = logging.getLogger(__name__)

FRED_DTB3_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"
QUOTE_ASSETS: tuple[str, ...] = ("USDT", "USDC")
PUBLICATION_LAG = pd.Timedelta(days=1)  # DTB3 for date d becomes usable at (d + 1 day) 00:00 UTC
_DATE_COLUMNS = ("observation_date", "DATE")  # current FRED header, and the pre-2025 one


def parse_dtb3_csv(text: str) -> pd.Series:
    """FRED DTB3 CSV -> float Series (annual decimal) indexed by observation date at 00:00 UTC.

    Missing observations are encoded as "." (legacy) or an empty field (current); both are dropped.
    Any other non-numeric value raises.
    """
    df = pd.read_csv(io.StringIO(text), dtype=str, na_values=["."], keep_default_na=True)
    date_col = next((c for c in _DATE_COLUMNS if c in df.columns), None)
    if date_col is None or "DTB3" not in df.columns:
        raise ValueError(f"unexpected FRED DTB3 CSV header: {list(df.columns)}")
    values = pd.to_numeric(df["DTB3"], errors="raise").to_numpy(dtype="float64") / 100.0
    index = pd.DatetimeIndex(pd.to_datetime(df[date_col], format="%Y-%m-%d", utc=True), name="date")
    s = pd.Series(values, index=index, name="dtb3").dropna().sort_index()
    if s.empty:
        raise ValueError("FRED DTB3 CSV contains no observations")
    if s.index.has_duplicates:
        raise ValueError("FRED DTB3 CSV has duplicate observation dates")
    return s


def fetch_dtb3(
    *,
    url: str = FRED_DTB3_URL,
    session: requests.Session | None = None,
    timeout: float = 60.0,
    retries: int = 3,
    backoff_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.Series:
    """Download DTB3 from FRED (keyless CSV). Raises RuntimeError/ValueError on failure.

    The caller decides about the fallback constant (config.RateInputs.fallback_r_usd) and must log
    loudly when it uses it.
    """
    http = session if session is not None else requests.Session()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = http.get(url, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
        else:
            if resp.status_code == 200:
                return parse_dtb3_csv(resp.text)
            if resp.status_code < 500:
                raise RuntimeError(f"FRED DTB3 fetch failed: HTTP {resp.status_code} from {url}")
            last_error = RuntimeError(f"HTTP {resp.status_code}")
        if attempt < retries:
            delay = backoff_s * 2.0 ** (attempt - 1)
            logger.warning(
                "FRED DTB3 fetch attempt %d/%d failed (%s); retrying in %.0fs", attempt, retries, last_error, delay
            )
            sleep(delay)
    raise RuntimeError(f"FRED DTB3 fetch failed after {retries} attempts: {last_error}") from last_error


def r_usd_on_grid(dtb3: pd.Series, grid: pd.DatetimeIndex) -> pd.Series:
    """Forward-fill the daily DTB3 decimal onto ``grid`` without look-ahead.

    The observation for date d (indexed at d 00:00 UTC) applies from (d + 1 day) 00:00 UTC onward,
    never from d itself (next-business-day publication). Grid points before the first applicable
    observation are NaN.
    """
    if not isinstance(grid, pd.DatetimeIndex) or grid.tz is None:
        raise ValueError("grid must be a tz-aware DatetimeIndex")
    s = dtb3.dropna().sort_index()
    idx = pd.DatetimeIndex(s.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    if idx.has_duplicates:
        raise ValueError("DTB3 series has duplicate dates")
    s = pd.Series(s.to_numpy(dtype="float64"), index=(idx + PUBLICATION_LAG).as_unit("ns"))
    target = grid.tz_convert("UTC").as_unit("ns")
    out = s.reindex(target, method="ffill")
    return pd.Series(out.to_numpy(dtype="float64"), index=grid, name="r_usd")


def base_asset(root: str) -> str:
    """``BTCUSDT`` -> ``BTC``."""
    for quote in QUOTE_ASSETS:
        if root.endswith(quote) and len(root) > len(quote):
            return root[: -len(quote)]
    raise ValueError(f"cannot infer the base asset of {root!r} (expected a {'/'.join(QUOTE_ASSETS)} pair)")


def coin_yield(root: str, rate_inputs: RateInputs | None = None) -> float:
    """Annual coin yield r_prime for the base asset of ``root`` (config.RateInputs.coin_yield)."""
    table = (rate_inputs if rate_inputs is not None else RateInputs()).coin_yield
    base = base_asset(root)
    if base not in table:
        raise ValueError(f"no coin yield configured for {base} (config.RateInputs.coin_yield has {sorted(table)})")
    return float(table[base])
