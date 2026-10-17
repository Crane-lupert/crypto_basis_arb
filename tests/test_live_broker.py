"""live/broker.py, offline: paper fill math, HMAC signing, mocked testnet REST, no mainnet host in the source."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import pandas as pd
import pytest

from basis_arb.config import CostModel
from basis_arb.live import broker as broker_mod
from basis_arb.live.broker import (
    FUTURES_TESTNET,
    SPOT_TESTNET,
    BrokerError,
    Order,
    PaperBroker,
    TestnetBroker,
    UnsupportedOnTestnet,
    sign,
)
from basis_arb.live.feed import LiveQuote

TS = pd.Timestamp("2026-10-17 19:40:00", tz="UTC")
CQ_EXP = pd.Timestamp("2026-12-25 08:00", tz="UTC")
NQ_EXP = pd.Timestamp("2027-03-26 08:00", tz="UTC")


def make_quote(**kw) -> LiveQuote:
    base = dict(
        root="BTCUSDT", ts=TS, spot_bid=80_000.0, spot_ask=80_001.0, perp_bid=79_990.0, perp_ask=79_991.0,
        perp_mark=79_990.5, perp_index=80_000.5, last_funding_rate=0.0001,
        next_funding_time=pd.Timestamp("2026-10-18 00:00", tz="UTC"),
        cq_symbol="BTCUSDT_261225", cq_bid=81_000.0, cq_ask=81_002.0, cq_expiry=CQ_EXP,
        nq_symbol="BTCUSDT_270326", nq_bid=82_000.0, nq_ask=82_004.0, nq_expiry=NQ_EXP,
        server_time=TS, latency_ms=12.0,
    )
    base.update(kw)
    return LiveQuote(**base)


# --- paper -----------------------------------------------------------------------------------
def test_paper_buy_spot_at_ask_plus_slippage_with_spot_fee():
    f = PaperBroker(CostModel()).place(Order("funding_carry", "spot", "BTCUSDT", "buy", 0.125, "ENTER"), make_quote())
    assert f.price == pytest.approx(80_001.0 * (1 + 2e-4), rel=1e-12)
    assert f.fee == pytest.approx(0.125 * f.price * 10e-4, rel=1e-12)  # spot taker 10 bp
    assert (f.qty, f.side, f.leg, f.instrument, f.reason, f.ts) == (0.125, "buy", "spot", "BTCUSDT", "ENTER", TS)


def test_paper_sell_perp_at_bid_minus_slippage_with_futures_fee():
    f = PaperBroker(CostModel()).place(
        Order("funding_carry", "perp", "BTCUSDT-PERP", "sell", 0.125, "ENTER"), make_quote())
    assert f.price == pytest.approx(79_990.0 * (1 - 2e-4), rel=1e-12)
    assert f.fee == pytest.approx(0.125 * f.price * 5e-4, rel=1e-12)  # futures taker 5 bp


def test_paper_quarterlies_use_their_own_book_and_the_futures_fee():
    b = PaperBroker(CostModel())
    buy = b.place(Order("calendar", "fut", "BTCUSDT_261225", "buy", 0.2, "ENTER"), make_quote())
    sell = b.place(Order("calendar", "fut", "BTCUSDT_270326", "sell", 0.2, "ROLL"), make_quote())
    assert buy.price == pytest.approx(81_002.0 * 1.0002) and buy.fee == pytest.approx(0.2 * buy.price * 5e-4)
    assert sell.price == pytest.approx(82_000.0 * 0.9998) and sell.fee == pytest.approx(0.2 * sell.price * 5e-4)


def test_paper_fill_follows_the_cost_model_and_is_deterministic():
    costs = CostModel(spot_fee_bps=7.5, fut_fee_bps=2.0, slippage_bps=0.0)
    b, q = PaperBroker(costs), make_quote()
    o = Order("cash_and_carry", "spot", "BTCUSDT", "sell", 0.5, "EXIT")
    f1, f2 = b.place(o, q), b.place(o, q)
    assert f1 == f2  # same order + same quote -> identical fill
    assert f1.price == 80_000.0 and f1.fee == pytest.approx(0.5 * 80_000.0 * 7.5e-4)


@pytest.mark.parametrize(
    "order",
    [
        Order("funding_carry", "fut", "BTCUSDT_990101", "buy", 0.1, "ENTER"),  # not quoted
        Order("funding_carry", "spot", "BTCUSDT", "buy", 0.0, "ENTER"),
        Order("funding_carry", "spot", "BTCUSDT", "buy", math.nan, "ENTER"),
        Order("funding_carry", "spot", "BTCUSDT", "short", 0.1, "ENTER"),
    ],
)
def test_paper_rejects_bad_orders(order):
    with pytest.raises(BrokerError):
        PaperBroker().place(order, make_quote())


# --- signing + the no-mainnet guarantee -------------------------------------------------------
def test_sign_known_vector():
    query = "symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.001&timestamp=1700000000000"
    # computed once with hmac.new(b"s3cret", query.encode(), hashlib.sha256).hexdigest()
    assert sign(query, "s3cret") == "35fcd0c989e4c2fa66fd7ff2712e87b588413dbfe9c20447d95a02475e471c77"


def test_broker_source_contains_no_mainnet_host():
    src = Path(broker_mod.__file__).read_text(encoding="utf-8")
    assert "api.binance.com" not in src
    assert "fapi.binance.com" not in src
    assert "binance.com" not in src  # no real-money host of any kind (api1-4, dapi, papi, ...)
    assert set(re.findall(r"https?://([A-Za-z0-9.-]+)", src)) == {"testnet.binancefuture.com",
                                                                  "testnet.binance.vision"}
    assert broker_mod.TESTNET_BASES == (FUTURES_TESTNET, SPOT_TESTNET)


# --- testnet (mocked HTTP) --------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class Seq:
    """Successive answers for one route (the last one repeats)."""

    def __init__(self, *items):
        self.items = list(items)

    def next(self):
        return self.items.pop(0) if len(self.items) > 1 else self.items[0]


class FakeSession:
    """Records every request; answers from a {(method, host, path): payload | Seq | callable} table."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _answer(self, method: str, url: str):
        parts = urlsplit(url)
        val = self.routes[(method, parts.netloc, parts.path)]
        if isinstance(val, Seq):
            val = val.next()
        if callable(val):
            val = val(url)
        return val if isinstance(val, FakeResponse) else FakeResponse(200, val)

    def get(self, url, params=None, timeout=None):
        full = url + ("?" + urlencode(params) if params else "")
        self.calls.append(("GET", full, {}))
        return self._answer("GET", url)

    def request(self, method, url, headers=None, timeout=None):
        self.calls.append((method, url, dict(headers or {})))
        return self._answer(method, url)


FUT_HOST, SPOT_HOST = "testnet.binancefuture.com", "testnet.binance.vision"


def _lot(step: str) -> list[dict]:
    return [{"filterType": "LOT_SIZE", "stepSize": step, "minQty": step, "maxQty": "1000"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": step, "minQty": step, "maxQty": "120"}]


def make_routes(overrides: dict | None = None) -> dict:
    routes = {
        ("GET", FUT_HOST, "/fapi/v1/time"): {"serverTime": 1_792_266_000_000},
        ("GET", SPOT_HOST, "/api/v3/time"): {"serverTime": 1_792_266_000_000},
        ("GET", FUT_HOST, "/fapi/v1/exchangeInfo"): {"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "filters": _lot("0.001")},
            {"symbol": "BTCUSDT_261225", "status": "TRADING", "contractType": "CURRENT_QUARTER",
             "filters": _lot("0.001")},
            {"symbol": "BTCUSDT_270326", "status": "PENDING_TRADING", "contractType": "NEXT_QUARTER",
             "filters": _lot("0.001")},
        ]},
        ("GET", SPOT_HOST, "/api/v3/exchangeInfo"): {"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
             "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"}]}]},
        ("POST", FUT_HOST, "/fapi/v1/order"): lambda url: {
            "orderId": 1, "symbol": dict(parse_qsl(urlsplit(url).query))["symbol"], "status": "FILLED",
            "avgPrice": "79991.50", "executedQty": "0.125", "cumQuote": "9998.94", "updateTime": 1_792_266_000_123},
        ("POST", SPOT_HOST, "/api/v3/order"): {
            "symbol": "BTCUSDT", "status": "FILLED", "executedQty": "0.12500000", "cummulativeQuoteQty": "10000.25",
            "transactTime": 1_792_266_000_456,
            "fills": [{"price": "80002.00", "qty": "0.125", "commission": "0.0000125", "commissionAsset": "BTC"}]},
        ("GET", FUT_HOST, "/fapi/v2/balance"): [{"asset": "USDT", "balance": "15000.0"},
                                                 {"asset": "BTC", "balance": "0"}],
        ("GET", SPOT_HOST, "/api/v3/account"): {"balances": [{"asset": "BTC", "free": "1.0", "locked": "0"},
                                                              {"asset": "ETH", "free": "0", "locked": "0"}]},
    }
    routes.update(overrides or {})
    return routes


def posts(sess: FakeSession) -> list[tuple[str, str, dict]]:
    return [c for c in sess.calls if c[0] == "POST"]


def test_testnet_futures_order_is_signed_routed_and_parsed():
    sess = FakeSession(make_routes())
    tb = TestnetBroker("my-key", "my-secret", CostModel(), session=sess)
    fill = tb.place(Order("funding_carry", "perp", "BTCUSDT-PERP", "sell", 0.12599, "ENTER"), make_quote())
    (method, url, headers), = posts(sess)
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", FUT_HOST, "/fapi/v1/order")
    query, signature = parts.query.rsplit("&signature=", 1)
    assert signature == sign(query, "my-secret")
    p = dict(parse_qsl(query))
    assert (p["symbol"], p["side"], p["type"], p["newOrderRespType"]) == ("BTCUSDT", "SELL", "MARKET", "RESULT")
    assert p["quantity"] == "0.125"  # floored to the 0.001 step, never rounded up
    assert p["recvWindow"] == "5000" and p["timestamp"].isdigit()
    assert headers == {"X-MBX-APIKEY": "my-key"}
    assert all("my-secret" not in c[1] for c in sess.calls)
    assert fill.price == pytest.approx(79_991.5) and fill.qty == pytest.approx(0.125)
    assert fill.fee == pytest.approx(0.125 * 79_991.5 * 5e-4)  # futures responses carry no commission: modeled
    assert (fill.leg, fill.instrument, fill.side) == ("perp", "BTCUSDT-PERP", "sell")
    assert fill.ts == pd.Timestamp(1_792_266_000_123, unit="ms", tz="UTC")


def test_testnet_spot_order_parses_vwap_and_base_asset_commission():
    sess = FakeSession(make_routes())
    tb = TestnetBroker("my-key", "my-secret", session=sess)
    fill = tb.place(Order("funding_carry", "spot", "BTCUSDT", "buy", 0.125, "ENTER"), make_quote())
    (_, url, _), = posts(sess)
    parts = urlsplit(url)
    assert (parts.netloc, parts.path) == (SPOT_HOST, "/api/v3/order")
    assert dict(parse_qsl(parts.query.rsplit("&signature=", 1)[0]))["newOrderRespType"] == "FULL"
    assert fill.price == pytest.approx(10_000.25 / 0.125)
    assert fill.fee == pytest.approx(0.0000125 * 80_002.0)  # BTC commission converted at the fill price


def test_testnet_routes_listed_quarterly_and_refuses_unlisted_or_pending():
    sess = FakeSession(make_routes())
    tb = TestnetBroker("k", "s", session=sess)
    tb.place(Order("calendar", "fut", "BTCUSDT_261225", "sell", 0.2, "ENTER"), make_quote())
    (_, url, _), = posts(sess)
    assert urlsplit(url).netloc == FUT_HOST and "symbol=BTCUSDT_261225" in url
    for inst in ("BTCUSDT_270326", "BTCUSDT_990101"):  # PENDING_TRADING / not listed on the testnet
        assert tb.supports("fut", inst) is False
        with pytest.raises(UnsupportedOnTestnet):
            tb.place(Order("calendar", "fut", inst, "sell", 0.2, "ENTER"), make_quote())
    assert len(posts(sess)) == 1  # nothing was sent for the unsupported contracts


def test_testnet_round_qty_floors_to_the_venue_step():
    tb = TestnetBroker("k", "s", session=FakeSession(make_routes()))
    assert tb.round_qty("perp", "BTCUSDT-PERP", 0.12599) == pytest.approx(0.125)
    assert tb.round_qty("spot", "BTCUSDT", 0.1234567) == pytest.approx(0.12345)


def test_testnet_errors_and_repr_never_contain_credentials():
    routes = make_routes({("POST", FUT_HOST, "/fapi/v1/order"): FakeResponse(
        400, {"code": -2019, "msg": "Margin is insufficient."})})
    tb = TestnetBroker("my-key", "my-secret", session=FakeSession(routes))
    with pytest.raises(BrokerError) as ei:
        tb.place(Order("funding_carry", "perp", "BTCUSDT-PERP", "sell", 0.125, "ENTER"), make_quote())
    assert "-2019" in str(ei.value)
    assert "my-secret" not in str(ei.value) and "my-key" not in str(ei.value)
    assert "my-secret" not in repr(tb) and "my-key" not in repr(tb)


def test_testnet_resyncs_the_clock_once_on_recv_window_error():
    ok = make_routes()[("POST", FUT_HOST, "/fapi/v1/order")]
    routes = make_routes({("POST", FUT_HOST, "/fapi/v1/order"): Seq(
        FakeResponse(400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}), ok)})
    sess = FakeSession(routes)
    fill = TestnetBroker("k", "s", session=sess).place(
        Order("funding_carry", "perp", "BTCUSDT-PERP", "buy", 0.125, "EXIT"), make_quote())
    assert fill.qty == pytest.approx(0.125)
    assert len(posts(sess)) == 2
    assert sum(1 for c in sess.calls if c[1].startswith(FUTURES_TESTNET + "/fapi/v1/time")) == 2


def test_testnet_refuses_any_non_testnet_base():
    sess = FakeSession(make_routes())
    tb = TestnetBroker("k", "s", session=sess)
    with pytest.raises(BrokerError):
        tb._signed("POST", "https://example.org", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert sess.calls == []


def test_testnet_balances_check_uses_signed_account_endpoints():
    sess = FakeSession(make_routes())
    bal = TestnetBroker("k", "s", session=sess).check()
    assert bal == {"futures": {"USDT": 15_000.0}, "spot": {"BTC": 1.0}}
    signed = [c for c in sess.calls if "signature=" in c[1]]
    assert {urlsplit(c[1]).path for c in signed} == {"/fapi/v2/balance", "/api/v3/account"}


def test_testnet_requires_credentials():
    with pytest.raises(ValueError):
        TestnetBroker("", "secret")
    with pytest.raises(ValueError):
        TestnetBroker("key", "")
