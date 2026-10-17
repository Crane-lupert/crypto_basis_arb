"""Parquet cache for raw Binance series, with incremental (tail) updates.

Layout (under the repo's ``data/`` directory, git-ignored):
    data/raw/{market}_{symbol}_{interval}.parquet   klines keyed by open_time
    data/raw/funding_{symbol}.parquet               funding prints keyed by funding_time

Each file records the earliest start it was fetched from (parquet schema metadata), so a later
request with an earlier start backfills the head instead of silently returning less history.
Updates re-fetch from ``last timestamp - overlap`` (2 bars) and de-duplicate on the time key
(newest fetch wins), so the cached frame is always sorted and free of duplicate timestamps.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from basis_arb.config import FUNDING_INTERVAL_HOURS
from basis_arb.data import binance

logger = logging.getLogger(__name__)

_META_COVERED_START = b"basis_arb.covered_start_ms"

FetchFn = Callable[[int, "int | None"], pd.DataFrame]


def default_data_dir() -> Path:
    """``<repo>/data`` for the editable install (src/basis_arb/data/cache.py -> repo root)."""
    return Path(__file__).resolve().parents[3] / "data"


def resolve_data_dir(data_dir: str | os.PathLike[str] | None = None) -> Path:
    return Path(data_dir) if data_dir is not None else default_data_dir()


def kline_cache_path(market: str, symbol: str, interval: str, data_dir: str | os.PathLike[str] | None = None) -> Path:
    return resolve_data_dir(data_dir) / "raw" / f"{market}_{symbol}_{interval}.parquet"


def funding_cache_path(symbol: str, data_dir: str | os.PathLike[str] | None = None) -> Path:
    return resolve_data_dir(data_dir) / "raw" / f"funding_{symbol}.parquet"


def _ts_ms(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def _normalize(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    """De-duplicate on ``time_col`` keeping the LAST occurrence (newest fetch), then sort."""
    if time_col not in df.columns:
        raise ValueError(f"frame has no {time_col!r} column (columns: {list(df.columns)})")
    out = df.drop_duplicates(time_col, keep="last").sort_values(time_col, kind="mergesort")
    return out.reset_index(drop=True)


def _window(df: pd.DataFrame, time_col: str, start_ms: int, end_ms: int | None) -> pd.DataFrame:
    t = df[time_col]
    keep = t >= pd.Timestamp(start_ms, unit="ms", tz="UTC")
    if end_ms is not None:
        keep &= t <= pd.Timestamp(end_ms, unit="ms", tz="UTC")
    return df[keep].reset_index(drop=True)


def read_cache(path: str | os.PathLike[str]) -> tuple[pd.DataFrame, int | None]:
    """Return (frame, covered_start_ms or None)."""
    table = pq.read_table(path)
    meta = table.schema.metadata or {}
    covered = int(meta[_META_COVERED_START]) if _META_COVERED_START in meta else None
    return table.to_pandas(), covered


def write_cache(df: pd.DataFrame, path: str | os.PathLike[str], covered_start_ms: int) -> None:
    """Atomic write (temp file + os.replace) with the covered start recorded in the schema metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    meta = dict(table.schema.metadata or {})
    meta[_META_COVERED_START] = str(int(covered_start_ms)).encode()
    table = table.replace_schema_metadata(meta)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def load_or_fetch(
    path: str | os.PathLike[str],
    fetch: FetchFn,
    start_ms: int,
    end_ms: int | None = None,
    *,
    time_col: str,
    overlap_ms: int,
) -> pd.DataFrame:
    """Return rows with ``start_ms <= time_col <= end_ms``, fetching only what the cache lacks.

    ``fetch(start_ms, end_ms)`` must return a frame with ``time_col`` (bounds inclusive). With a
    cache present: the head ``[start_ms, covered_start)`` is fetched if the request starts earlier
    than any previous fetch; the tail is re-fetched from ``last - overlap_ms`` unless the cache
    already reaches ``end_ms``. Empty results are not written.
    """
    path = Path(path)
    start_ms = int(start_ms)
    end_ms = None if end_ms is None else int(end_ms)

    if not path.exists():
        fresh = _normalize(fetch(start_ms, end_ms), time_col)
        if not fresh.empty:
            write_cache(fresh, path, start_ms)
        logger.info("%s: fetched %d row(s) (no cache)", path.name, len(fresh))
        return _window(fresh, time_col, start_ms, end_ms)

    cached, covered = read_cache(path)
    cached = _normalize(cached, time_col)
    if covered is None:
        covered = _ts_ms(cached[time_col].iloc[0]) if not cached.empty else start_ms
    parts = [cached]
    new_covered = covered
    if start_ms < covered:
        head_end = covered - 1 if end_ms is None else min(end_ms, covered - 1)
        parts.append(fetch(start_ms, head_end))
        new_covered = start_ms
    if cached.empty:
        tail_start: int | None = new_covered
    else:
        last = _ts_ms(cached[time_col].iloc[-1])
        tail_start = None if (end_ms is not None and last >= end_ms) else max(new_covered, last - int(overlap_ms))
    if tail_start is not None and (end_ms is None or tail_start <= end_ms):
        parts.append(fetch(tail_start, end_ms))

    non_empty = [p for p in parts if not p.empty]
    merged = _normalize(pd.concat(non_empty, ignore_index=True), time_col) if non_empty else cached
    changed = (new_covered != covered) or (len(merged) != len(cached)) or not merged.equals(cached)
    if changed and not merged.empty:
        write_cache(merged, path, new_covered)
    logger.info(
        "%s: %d cached row(s), %d after update%s",
        path.name, len(cached), len(merged), "" if changed else " (unchanged)",
    )
    return _window(merged, time_col, start_ms, end_ms)


def cached_klines(
    market: binance.Market,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int | None = None,
    *,
    client: binance.SupportsGet | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> pd.DataFrame:
    """``binance.fetch_klines`` through the parquet cache (2-bar overlap on updates)."""
    step = binance.interval_ms(interval)

    def fetch(s: int, e: int | None) -> pd.DataFrame:
        return binance.fetch_klines(market, symbol, interval, s, e, client=client)

    path = kline_cache_path(market, symbol, interval, data_dir)
    return load_or_fetch(path, fetch, start_ms, end_ms, time_col="open_time", overlap_ms=2 * step)


def cached_funding(
    symbol: str,
    start_ms: int,
    end_ms: int | None = None,
    *,
    client: binance.SupportsGet | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> pd.DataFrame:
    """``binance.fetch_funding`` through the parquet cache (2 nominal funding intervals of overlap)."""

    def fetch(s: int, e: int | None) -> pd.DataFrame:
        return binance.fetch_funding(symbol, s, e, client=client)

    path = funding_cache_path(symbol, data_dir)
    overlap = 2 * FUNDING_INTERVAL_HOURS * 3_600_000
    return load_or_fetch(path, fetch, start_ms, end_ms, time_col="funding_time", overlap_ms=overlap)
