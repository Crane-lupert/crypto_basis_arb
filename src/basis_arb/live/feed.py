"""Live quote snapshot (keyless mainnet public endpoints) and the live panel row (strategy_spec.md section 9).

Fail-secure: any non-200, missing field, non-positive or crossed book raises FeedError; the daemon
turns that into SAFE mode (no new entries). Quotes are never patched or guessed.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from basis_arb.live._http import PublicClient
from basis_arb.live.history import SPOT_BASE, UM_BASE, Quarterly, parse_quarterlies
from basis_arb.live.state import from_ms, iso, utc
from basis_arb.types import validate_panel

log = logging.getLogger(__name__)

EXCHANGE_INFO_TTL_S = 3600.0
EXPIRED_LISTING_REFRESH_S = 300.0


class FeedError(RuntimeError):
    """Quote unavailable or malformed (the daemon goes SAFE; nothing is guessed)."""


def perp_instrument(root: str) -> str:
    """Book/ledger name of the USD-M perpetual (types.Fill convention: BTCUSDT-PERP)."""
    return f"{root}-PERP"


@dataclass(frozen=True)
class LiveQuote:
    root: str
    ts: pd.Timestamp  # local UTC receipt time of the snapshot
    spot_bid: float
    spot_ask: float
    perp_bid: float
    perp_ask: float
    perp_mark: float
    perp_index: float
    last_funding_rate: float
    next_funding_time: pd.Timestamp
    cq_symbol: str | None
    cq_bid: float
    cq_ask: float
    cq_expiry: pd.Timestamp | None
    nq_symbol: str | None
    nq_bid: float
    nq_ask: float
    nq_expiry: pd.Timestamp | None
    server_time: pd.Timestamp  # oldest exchange-side timestamp (perp book, premiumIndex)
    latency_ms: float

    @property
    def spot_mid(self) -> float:
        return 0.5 * (self.spot_bid + self.spot_ask)

    @property
    def perp_mid(self) -> float:
        return 0.5 * (self.perp_bid + self.perp_ask)

    def book(self, instrument: str) -> tuple[float, float]:
        """(bid, ask) for a book instrument name; KeyError if this snapshot does not quote it."""
        if instrument == self.root:
            return self.spot_bid, self.spot_ask
        if instrument == perp_instrument(self.root):
            return self.perp_bid, self.perp_ask
        if self.cq_symbol is not None and instrument == self.cq_symbol:
            return self.cq_bid, self.cq_ask
        if self.nq_symbol is not None and instrument == self.nq_symbol:
            return self.nq_bid, self.nq_ask
        raise KeyError(f"no quote for {instrument}")

    def mid(self, instrument: str) -> float:
        bid, ask = self.book(instrument)
        return 0.5 * (bid + ask)

    def expiry_of(self, instrument: str) -> pd.Timestamp | None:
        if self.cq_symbol is not None and instrument == self.cq_symbol:
            return self.cq_expiry
        if self.nq_symbol is not None and instrument == self.nq_symbol:
            return self.nq_expiry
        return None

    def prices(self) -> dict[str, float]:
        out = {self.root: self.spot_mid, perp_instrument(self.root): self.perp_mid}
        for sym in (self.cq_symbol, self.nq_symbol):
            if sym is not None:
                out[sym] = self.mid(sym)
        return out

    def age_s(self, now: Any) -> float:
        """Seconds since the older of (receipt time, exchange time): stale exchange data counts as stale."""
        return float((utc(now) - min(self.ts, self.server_time)).total_seconds())

    def is_stale(self, now: Any, max_age_s: float) -> bool:
        return self.age_s(now) > max_age_s

    def to_event(self) -> dict:
        return {
            "ts": iso(self.ts), "server_time": iso(self.server_time), "latency_ms": round(self.latency_ms, 1),
            "spot_bid": self.spot_bid, "spot_ask": self.spot_ask, "perp_bid": self.perp_bid,
            "perp_ask": self.perp_ask, "perp_mark": self.perp_mark, "perp_index": self.perp_index,
            "last_funding_rate": self.last_funding_rate, "next_funding_time": iso(self.next_funding_time),
            "cq_symbol": self.cq_symbol, "cq_bid": self.cq_bid, "cq_ask": self.cq_ask,
            "cq_expiry": iso(self.cq_expiry), "nq_symbol": self.nq_symbol, "nq_bid": self.nq_bid,
            "nq_ask": self.nq_ask, "nq_expiry": iso(self.nq_expiry),
        }


def _num(payload: dict, key: str) -> float:
    if key not in payload:
        raise FeedError(f"missing field {key!r}")
    try:
        x = float(payload[key])
    except (TypeError, ValueError) as exc:
        raise FeedError(f"non-numeric field {key!r}={payload[key]!r}") from exc
    if not math.isfinite(x):
        raise FeedError(f"non-finite field {key!r}")
    return x


def _pos(payload: dict, key: str) -> float:
    x = _num(payload, key)
    if x <= 0:
        raise FeedError(f"non-positive field {key!r}={x}")
    return x


def _book(payload: Any, symbol: str) -> tuple[float, float, dict]:
    if not isinstance(payload, dict):
        raise FeedError(f"unexpected book payload for {symbol}")
    if payload.get("symbol") != symbol:
        raise FeedError(f"book symbol mismatch: asked {symbol}, got {payload.get('symbol')!r}")
    bid, ask = _pos(payload, "bidPrice"), _pos(payload, "askPrice")
    if bid > ask:
        raise FeedError(f"crossed book for {symbol}: bid {bid} > ask {ask}")
    return bid, ask, payload


class LiveFeed:
    """One snapshot per loop: spot + perp + quarterly bookTicker, premiumIndex, exchangeInfo (cached 1h)."""

    def __init__(self, client: PublicClient | None = None, exchange_info_ttl_s: float = EXCHANGE_INFO_TTL_S,
                 check_system_status: bool = True, clock: Callable[[], Any] | None = None) -> None:
        self.client = client or PublicClient(retries=0)  # fail fast: the next loop is the retry
        self.ttl_s = exchange_info_ttl_s
        self.check_system_status = check_system_status
        self.clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self._info: dict | None = None
        self._info_at: float | None = None

    def _exchange_info(self, force: bool = False) -> dict:
        age = None if self._info_at is None else time.monotonic() - self._info_at
        if force or self._info is None or age is None or age > self.ttl_s:
            info = self.client.get_json(UM_BASE + "/fapi/v1/exchangeInfo")
            if not isinstance(info, dict) or "symbols" not in info:
                raise FeedError("unexpected exchangeInfo payload")
            self._info, self._info_at = info, time.monotonic()
        return self._info

    def _quarterlies(self, root: str, now: pd.Timestamp) -> list[Quarterly]:
        qs = parse_quarterlies(self._exchange_info(), root)
        age = time.monotonic() - (self._info_at or 0.0)
        if any(q.expiry <= now for q in qs) and age > EXPIRED_LISTING_REFRESH_S:
            # a cached contract has expired: refresh the listing (throttled; some listings keep DELIVERING rows)
            qs = parse_quarterlies(self._exchange_info(force=True), root)
        return [q for q in qs if q.status == "TRADING" and q.expiry > now][:2]

    def snapshot(self, root: str) -> LiveQuote:
        t0 = time.monotonic()
        try:
            return self._snapshot(root, t0)
        except FeedError:
            raise
        except Exception as exc:  # HttpError, malformed payloads, anything else: fail secure
            raise FeedError(f"{type(exc).__name__}: {exc}") from exc

    def _snapshot(self, root: str, t0: float) -> LiveQuote:
        now = utc(self.clock())
        if self.check_system_status:
            status = self.client.get_json(SPOT_BASE + "/sapi/v1/system/status")
            if int(_num(status, "status")) != 0:
                raise FeedError(f"exchange maintenance: {status.get('msg')!r}")
        info = self._exchange_info()
        perp_meta = next((s for s in info["symbols"] if s.get("symbol") == root), None)
        if perp_meta is None or perp_meta.get("contractType") != "PERPETUAL":
            raise FeedError(f"{root} perpetual not listed")
        if perp_meta.get("status") != "TRADING":
            raise FeedError(f"{root} perpetual status {perp_meta.get('status')!r}")
        quarterlies = self._quarterlies(root, now)

        sb, sa, _ = _book(self.client.get_json(SPOT_BASE + "/api/v3/ticker/bookTicker", {"symbol": root}), root)
        pb, pa, pbook = _book(self.client.get_json(UM_BASE + "/fapi/v1/ticker/bookTicker", {"symbol": root}), root)
        prem = self.client.get_json(UM_BASE + "/fapi/v1/premiumIndex", {"symbol": root})
        if not isinstance(prem, dict) or prem.get("symbol") != root:
            raise FeedError("unexpected premiumIndex payload")
        mark, index = _pos(prem, "markPrice"), _pos(prem, "indexPrice")
        last_fr = _num(prem, "lastFundingRate")
        next_ft = int(_pos(prem, "nextFundingTime"))
        server_ms = min(int(_pos(pbook, "time")), int(_pos(prem, "time")))

        legs: list[tuple[str | None, float, float, pd.Timestamp | None]] = []
        for q in quarterlies:
            qb, qa, _ = _book(self.client.get_json(UM_BASE + "/fapi/v1/ticker/bookTicker", {"symbol": q.symbol}),
                              q.symbol)
            legs.append((q.symbol, qb, qa, q.expiry))
        while len(legs) < 2:
            legs.append((None, np.nan, np.nan, None))
        (cq_s, cq_b, cq_a, cq_e), (nq_s, nq_b, nq_a, nq_e) = legs

        return LiveQuote(
            root=root, ts=utc(self.clock()), spot_bid=sb, spot_ask=sa, perp_bid=pb, perp_ask=pa,
            perp_mark=mark, perp_index=index, last_funding_rate=last_fr, next_funding_time=from_ms(next_ft),
            cq_symbol=cq_s, cq_bid=cq_b, cq_ask=cq_a, cq_expiry=cq_e,
            nq_symbol=nq_s, nq_bid=nq_b, nq_ask=nq_a, nq_expiry=nq_e,
            server_time=from_ms(server_ms), latency_ms=(time.monotonic() - t0) * 1000.0,
        )


# --- live panel row ---------------------------------------------------------------------------
def live_row_ts(quote_ts: Any) -> pd.Timestamp:
    """The in-progress bar's END (bar covers [ts-1h, ts)): ceil to the hour, as in the backtest panel."""
    return utc(quote_ts).ceil("h")


def _missing(dtype: Any) -> Any:
    if isinstance(dtype, pd.DatetimeTZDtype) or (isinstance(dtype, np.dtype) and dtype.kind == "M"):
        return pd.NaT
    if dtype == object:
        return None
    return np.nan


def append_live_row(panel: pd.DataFrame, quote: LiveQuote) -> pd.DataFrame:
    """Append (or replace) the in-progress hour's row built from the live quote; output passes validate_panel.

    close = high = low = mid for spot, perp and the quoted quarterlies; funding_rate NaN unless a settled
    rate is already stored at that row (the daemon writes settled prints from /fapi/v1/fundingRate);
    r_usd / y_coin carried forward from the last panel row. The contract selection rule is re-applied at
    the row's ts (a quoted contract with expiry <= ts leaves the panel and nq shifts into cq).
    """
    validate_panel(panel)
    ts = live_row_ts(quote.ts)
    if ts < panel.index[-1]:
        raise ValueError(f"live row {ts} precedes the panel end {panel.index[-1]}")
    quoted = [(s, b, a, e) for s, b, a, e in ((quote.cq_symbol, quote.cq_bid, quote.cq_ask, quote.cq_expiry),
                                              (quote.nq_symbol, quote.nq_bid, quote.nq_ask, quote.nq_expiry))
              if s is not None and e is not None and utc(e) > ts]
    row: dict[str, Any] = {}
    for leg, mid in (("spot", quote.spot_mid), ("perp", quote.perp_mid)):
        row[f"{leg}_close"] = row[f"{leg}_high"] = row[f"{leg}_low"] = float(mid)
    settled = panel["funding_rate"].get(ts, np.nan) if ts in panel.index else np.nan
    row["funding_rate"] = float(settled) if pd.notna(settled) else np.nan
    for k, item in zip(("cq", "nq"), quoted + [None, None]):
        if item is None:
            row[f"fut_{k}_symbol"] = None
            row[f"fut_{k}_close"] = row[f"fut_{k}_high"] = row[f"fut_{k}_low"] = np.nan
            row[f"fut_{k}_expiry"] = pd.NaT
            row[f"fut_{k}_dte_days"] = np.nan
        else:
            sym, bid, ask, exp = item
            mid = 0.5 * (bid + ask)
            row[f"fut_{k}_symbol"] = sym
            row[f"fut_{k}_close"] = row[f"fut_{k}_high"] = row[f"fut_{k}_low"] = float(mid)
            row[f"fut_{k}_expiry"] = utc(exp)
            row[f"fut_{k}_dte_days"] = (utc(exp) - ts) / pd.Timedelta(days=1)
    row["r_usd"] = float(panel["r_usd"].iloc[-1])
    row["y_coin"] = float(panel["y_coin"].iloc[-1])
    if "perp_mark" in panel.columns:
        row["perp_mark"] = float(quote.perp_mark)

    idx = pd.DatetimeIndex([ts], name="ts")
    new = pd.DataFrame(
        {c: pd.Series([row.get(c, _missing(panel[c].dtype))], index=idx, dtype=panel[c].dtype)
         for c in panel.columns},
        index=idx,
    )
    out = pd.concat([panel[panel.index < ts], new])
    out.index.name = "ts"
    validate_panel(out)
    return out
