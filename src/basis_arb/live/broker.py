"""Order routing: deterministic paper fills on live quotes, or HMAC-signed REST to the Binance TESTNET.

Hard rule (strategy_spec.md section 9): this module knows exactly two hosts, the futures testnet and
the spot testnet below. No real-money trading host appears anywhere in this file, the constructor takes
no base URL, and _signed() refuses any base that is not one of the two testnet constants, so an order
cannot reach a real-money venue from here by construction. tests/test_live_broker.py reads this file's
source and asserts that.

Common interface (duck-typed, used by live/daemon.py):
    place(order, quote) -> types.Fill        raise BrokerError (UnsupportedOnTestnet) on failure
    supports(leg, instrument) -> bool         can this venue trade the instrument right now?
    round_qty(leg, instrument, qty) -> float  quantity the venue will accept (floor to its step size)
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import logging
import math
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

import pandas as pd
import requests

from basis_arb.config import CostModel
from basis_arb.types import Fill, Leg, Side

if TYPE_CHECKING:  # type hints only; the broker never fetches market data itself
    from basis_arb.live.feed import LiveQuote

log = logging.getLogger(__name__)

FUTURES_TESTNET = "https://testnet.binancefuture.com"
SPOT_TESTNET = "https://testnet.binance.vision"
TESTNET_BASES = (FUTURES_TESTNET, SPOT_TESTNET)
RECV_WINDOW_MS = 5000
INFO_TTL_S = 3600.0
PRICE_SANITY_FRAC = 0.02  # warn when a testnet fill is > 2% away from the live (mainnet) mid


class BrokerError(RuntimeError):
    """Order not placed or not filled (the daemon unwinds and goes SAFE). Never contains credentials."""


class UnsupportedOnTestnet(BrokerError):
    """The instrument is not TRADING on the testnet (e.g. a quarterly that is not listed there)."""


@dataclass(frozen=True)
class Order:
    strategy: str
    leg: Leg
    instrument: str  # book name: BTCUSDT (spot), BTCUSDT-PERP, BTCUSDT_261225
    side: Side
    qty: float  # coin units, positive
    reason: str  # ENTER / EXIT / ROLL / KILL


def sign(query: str, secret: str) -> str:
    """HMAC-SHA256 of the exact query string with the API secret, hex encoded (Binance SIGNED endpoints)."""
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def _check_order(order: Order) -> None:
    if order.side not in ("buy", "sell"):
        raise BrokerError(f"bad side {order.side!r}")
    if order.leg not in ("spot", "perp", "fut"):
        raise BrokerError(f"bad leg {order.leg!r}")
    if not (isinstance(order.qty, (int, float)) and math.isfinite(order.qty) and order.qty > 0):
        raise BrokerError(f"bad qty {order.qty!r}")


# --- paper ------------------------------------------------------------------------------------
class PaperBroker:
    """Deterministic fills on the live book: buy at ask*(1+slippage), sell at bid*(1-slippage).

    fee = qty * fill price * fee_bps (spot_fee_bps for the spot leg, fut_fee_bps for perp/quarterly).
    """

    name = "paper"

    def __init__(self, costs: CostModel | None = None) -> None:
        self.costs = costs or CostModel()

    def supports(self, leg: str, instrument: str) -> bool:
        return True

    def round_qty(self, leg: str, instrument: str, qty: float) -> float:
        return float(qty)

    def place(self, order: Order, quote: LiveQuote) -> Fill:
        _check_order(order)
        try:
            bid, ask = quote.book(order.instrument)
        except KeyError as exc:
            raise BrokerError(f"no quote for {order.instrument}") from exc
        if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
            raise BrokerError(f"invalid book for {order.instrument}: {bid}/{ask}")
        slip = self.costs.slippage_bps / 1e4
        price = ask * (1.0 + slip) if order.side == "buy" else bid * (1.0 - slip)
        fee_bps = self.costs.spot_fee_bps if order.leg == "spot" else self.costs.fut_fee_bps
        fee = order.qty * price * fee_bps / 1e4
        return Fill(ts=quote.ts, strategy=order.strategy, leg=order.leg, instrument=order.instrument,
                    side=order.side, qty=float(order.qty), price=float(price), fee=float(fee), reason=order.reason)


# --- testnet ----------------------------------------------------------------------------------
def _require_testnet(base: str) -> None:
    if base not in TESTNET_BASES or urlsplit(base).scheme != "https":
        raise BrokerError("refusing a non-testnet host")


def _floor_step(qty: float, step: Decimal) -> Decimal:
    if step <= 0:
        return Decimal(repr(float(qty)))
    return (Decimal(repr(float(qty))) / step).to_integral_value(rounding=ROUND_DOWN) * step


def _fmt(x: Decimal) -> str:
    s = format(x.normalize(), "f")
    return s if s not in ("-0", "") else "0"


def _lot(filters: list[dict]) -> tuple[Decimal, Decimal]:
    """(step, min_qty) from LOT_SIZE / MARKET_LOT_SIZE (the coarser step wins)."""
    step, min_qty = Decimal(0), Decimal(0)
    for f in filters or []:
        if f.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            s, m = Decimal(str(f.get("stepSize", "0"))), Decimal(str(f.get("minQty", "0")))
            step, min_qty = max(step, s), max(min_qty, m)
    return step, min_qty


class TestnetBroker:
    """Signed MARKET orders on the Binance testnets (futures: perp + listed quarterlies; spot)."""

    name = "testnet"
    __test__ = False  # not a pytest test class

    def __init__(self, api_key: str, api_secret: str, costs: CostModel | None = None,
                 session: requests.Session | None = None, timeout: tuple[float, float] = (5.0, 15.0)) -> None:
        if not api_key or not api_secret:
            raise ValueError("testnet API key and secret are required")
        self._api_key = api_key
        self._api_secret = api_secret
        self.costs = costs or CostModel()
        self.session = session or requests.Session()
        self.timeout = timeout
        self._offset_ms: dict[str, int | None] = {b: None for b in TESTNET_BASES}
        self._fut: dict[str, dict] | None = None
        self._fut_at = 0.0
        self._spot: dict[str, tuple[float, dict]] = {}
        self._ids = itertools.count(1)

    def __repr__(self) -> str:
        return "TestnetBroker(<credentials hidden>)"

    # -- transport -----------------------------------------------------------------------------
    def _public(self, base: str, path: str, params: dict | None = None) -> Any:
        _require_testnet(base)
        try:
            resp = self.session.get(base + path, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BrokerError(f"GET {path}: transport error ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise BrokerError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _offset(self, base: str) -> int:
        if self._offset_ms.get(base) is None:
            path = "/fapi/v1/time" if base == FUTURES_TESTNET else "/api/v3/time"
            t0 = time.time() * 1000.0
            server = int(self._public(base, path)["serverTime"])
            t1 = time.time() * 1000.0
            self._offset_ms[base] = int(server - (t0 + t1) / 2.0)
        return int(self._offset_ms[base] or 0)

    def _signed(self, method: str, base: str, path: str, params: dict, _retry: bool = True) -> Any:
        _require_testnet(base)
        q = dict(params)
        q["recvWindow"] = RECV_WINDOW_MS
        q["timestamp"] = int(time.time() * 1000) + self._offset(base)
        query = urlencode(q)
        url = f"{base}{path}?{query}&signature={sign(query, self._api_secret)}"
        try:
            resp = self.session.request(method, url, headers={"X-MBX-APIKEY": self._api_key}, timeout=self.timeout)
        except requests.RequestException as exc:
            # from None: the exception text would carry the signed URL; for orders the state is unknown
            raise BrokerError(f"{method} {path}: transport error ({type(exc).__name__}); state unknown") from None
        if resp.status_code != 200:
            code, msg = None, resp.text[:200]
            try:
                body = resp.json()
                code, msg = body.get("code"), body.get("msg")
            except ValueError:
                pass
            if code == -1021 and _retry:  # timestamp outside recvWindow: resync the clock once, then retry
                self._offset_ms[base] = None
                return self._signed(method, base, path, params, _retry=False)
            raise BrokerError(f"{method} {path} -> HTTP {resp.status_code} code={code} msg={msg}")
        try:
            return resp.json()
        except ValueError:
            raise BrokerError(f"{method} {path}: invalid JSON body") from None

    # -- symbols -------------------------------------------------------------------------------
    @staticmethod
    def _venue(leg: str, instrument: str) -> tuple[str, str]:
        if leg == "spot":
            return SPOT_TESTNET, instrument
        if leg == "perp":
            return FUTURES_TESTNET, instrument.removesuffix("-PERP")
        return FUTURES_TESTNET, instrument

    def _futures_symbols(self) -> dict[str, dict]:
        if self._fut is None or time.monotonic() - self._fut_at > INFO_TTL_S:
            info = self._public(FUTURES_TESTNET, "/fapi/v1/exchangeInfo")
            out = {}
            for s in info.get("symbols", []):
                step, min_qty = _lot(s.get("filters", []))
                out[str(s["symbol"])] = {"status": s.get("status"), "contract_type": s.get("contractType"),
                                         "step": step, "min_qty": min_qty}
            self._fut, self._fut_at = out, time.monotonic()
        return self._fut

    def _spot_symbol(self, symbol: str) -> dict:
        hit = self._spot.get(symbol)
        if hit is None or time.monotonic() - hit[0] > INFO_TTL_S:
            info = self._public(SPOT_TESTNET, "/api/v3/exchangeInfo", {"symbol": symbol})
            syms = info.get("symbols") or []
            if not syms:
                raise UnsupportedOnTestnet(f"{symbol} is not listed on the spot testnet")
            s = syms[0]
            step, min_qty = _lot(s.get("filters", []))
            meta = {"status": s.get("status"), "base": s.get("baseAsset"), "quote": s.get("quoteAsset"),
                    "step": step, "min_qty": min_qty}
            self._spot[symbol] = (time.monotonic(), meta)
            return meta
        return hit[1]

    def _meta(self, leg: str, instrument: str) -> tuple[str, str, dict]:
        base, symbol = self._venue(leg, instrument)
        if base == SPOT_TESTNET:
            meta = self._spot_symbol(symbol)
        else:
            meta = self._futures_symbols().get(symbol)
            if meta is None:
                raise UnsupportedOnTestnet(f"{symbol} is not listed on the futures testnet")
        if meta.get("status") != "TRADING":
            raise UnsupportedOnTestnet(f"{symbol} is {meta.get('status')!r} on the testnet, not TRADING")
        return base, symbol, meta

    def supports(self, leg: str, instrument: str) -> bool:
        try:
            self._meta(leg, instrument)
            return True
        except UnsupportedOnTestnet:
            return False

    def round_qty(self, leg: str, instrument: str, qty: float) -> float:
        _, _, meta = self._meta(leg, instrument)
        return float(_floor_step(qty, meta["step"]))

    # -- orders --------------------------------------------------------------------------------
    def place(self, order: Order, quote: LiveQuote | None = None) -> Fill:
        _check_order(order)
        base, symbol, meta = self._meta(order.leg, order.instrument)
        qty = _floor_step(order.qty, meta["step"])
        if qty <= 0 or qty < meta["min_qty"]:
            raise BrokerError(f"{symbol}: qty {order.qty} below the testnet minimum {meta['min_qty']}")
        cid = f"cba-{int(time.time() * 1000)}-{next(self._ids)}"
        params = {"symbol": symbol, "side": order.side.upper(), "type": "MARKET", "quantity": _fmt(qty),
                  "newClientOrderId": cid}
        if base == SPOT_TESTNET:
            params["newOrderRespType"] = "FULL"
            resp = self._signed("POST", base, "/api/v3/order", params)
            fill = self._spot_fill(order, resp, meta, cid)
        else:
            params["newOrderRespType"] = "RESULT"
            resp = self._signed("POST", base, "/fapi/v1/order", params)
            fill = self._futures_fill(order, resp, cid)
        if abs(fill.qty - float(qty)) > 1e-12:
            log.warning("testnet partial fill %s: %.8f of %s", cid, fill.qty, _fmt(qty))
        if quote is not None:
            try:
                mid = quote.mid(order.instrument)
                if abs(fill.price / mid - 1.0) > PRICE_SANITY_FRAC:
                    log.warning("testnet fill %s at %.2f is %.1f%% from the live mid %.2f", cid, fill.price,
                                100 * (fill.price / mid - 1.0), mid)
            except KeyError:
                pass
        return fill

    def _spot_fill(self, order: Order, resp: dict, meta: dict, cid: str) -> Fill:
        executed = float(resp.get("executedQty", 0) or 0)
        quote_qty = float(resp.get("cummulativeQuoteQty", 0) or 0)
        if executed <= 0 or quote_qty <= 0:
            raise BrokerError(f"spot order {cid} not filled (status {resp.get('status')!r})")
        price = quote_qty / executed
        fee, estimated = 0.0, False
        for f in resp.get("fills") or []:
            c, asset = float(f.get("commission", 0) or 0), f.get("commissionAsset")
            if asset == meta.get("quote"):
                fee += c
            elif asset == meta.get("base"):
                fee += c * float(f.get("price", price) or price)
            elif c:
                estimated = True
        if estimated or not resp.get("fills"):
            fee = quote_qty * self.costs.spot_fee_bps / 1e4  # commission in a third asset (e.g. BNB): model it
        ts = pd.Timestamp(int(resp.get("transactTime") or time.time() * 1000), unit="ms", tz="UTC")
        return Fill(ts=ts, strategy=order.strategy, leg=order.leg, instrument=order.instrument, side=order.side,
                    qty=executed, price=price, fee=fee, reason=order.reason)

    def _futures_fill(self, order: Order, resp: dict, cid: str) -> Fill:
        executed = float(resp.get("executedQty", 0) or 0)
        avg = float(resp.get("avgPrice", 0) or 0)
        if executed <= 0 or avg <= 0:
            raise BrokerError(f"futures order {cid} not filled (status {resp.get('status')!r})")
        fee = executed * avg * self.costs.fut_fee_bps / 1e4  # order responses carry no commission: modeled
        ts = pd.Timestamp(int(resp.get("updateTime") or time.time() * 1000), unit="ms", tz="UTC")
        return Fill(ts=ts, strategy=order.strategy, leg=order.leg, instrument=order.instrument, side=order.side,
                    qty=executed, price=avg, fee=fee, reason=order.reason)

    # -- account -------------------------------------------------------------------------------
    def balances(self) -> dict:
        """Non-zero testnet balances: futures wallet (GET /fapi/v2/balance) and spot (GET /api/v3/account)."""
        fut = self._signed("GET", FUTURES_TESTNET, "/fapi/v2/balance", {})
        spot = self._signed("GET", SPOT_TESTNET, "/api/v3/account", {})
        f_bal = {b["asset"]: float(b["balance"]) for b in fut if float(b.get("balance", 0) or 0) != 0}
        s_bal = {}
        for b in spot.get("balances", []):
            amt = float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
            if amt != 0:
                s_bal[b["asset"]] = amt
        return {"futures": f_bal, "spot": s_bal}

    def check(self) -> dict:
        """Verify credentials and connectivity on both testnets (raises BrokerError)."""
        return self.balances()
