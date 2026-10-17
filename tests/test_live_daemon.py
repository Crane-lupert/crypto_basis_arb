"""live/ state, history, feed and daemon logic, offline (fake feed / broker / history / funding), plus one
network smoke test of the real paper loop (marked `network`)."""
from __future__ import annotations

import copy
import json
import logging
import math
from collections import Counter
from urllib.parse import urlsplit

import numpy as np
import pandas as pd
import pytest

from basis_arb.config import BacktestConfig
from basis_arb.live import daemon as daemon_mod
from basis_arb.live import history
from basis_arb.live._http import HttpError
from basis_arb.live.broker import PaperBroker
from basis_arb.live.daemon import Daemon, MissingCredentials, StartupRefused, main, run
from basis_arb.live.feed import FeedError, LiveFeed, LiveQuote, append_live_row
from basis_arb.live.state import (
    EVENT_KINDS,
    Book,
    Ledger,
    StateLock,
    StateLocked,
    state_paths,
    strategy_capital,
    to_ms,
    utc,
)
from basis_arb.model import signals
from basis_arb.types import Fill, validate_panel
from tests.conftest import make_panel

ROOT = "BTCUSDT"
START = pd.Timestamp("2025-01-01", tz="UTC")
CQ_EXP = START + pd.Timedelta(days=60, hours=8)  # make_panel's first quarterly
NQ_EXP = CQ_EXP + pd.Timedelta(days=91)
Q3_EXP = NQ_EXP + pd.Timedelta(days=91)


# =============================================================================================
# state
# =============================================================================================
def test_book_round_trips_through_json(tmp_path):
    cfg = BacktestConfig()
    now = pd.Timestamp("2026-10-17 19:40:05.123456", tz="UTC")
    book = Book.new(ROOT, "paper", ("funding_carry", "cash_and_carry", "calendar"), cfg, now)
    assert book.equity_0 == pytest.approx(15_000 + 15_000 + 10_000)  # spec section 6 capital
    assert strategy_capital("calendar", cfg) == pytest.approx(10_000)
    book.apply_fill(Fill(now, "funding_carry", "spot", ROOT, "buy", 0.2, 50_010.0, 10.0, "ENTER"), mid=50_000.0)
    book.apply_fill(Fill(now, "funding_carry", "perp", "BTCUSDT-PERP", "sell", 0.2, 49_990.0, 5.0, "ENTER"),
                    mid=50_000.0)
    book.apply_fill(Fill(now, "cash_and_carry", "fut", "BTCUSDT_261225", "sell", 0.2, 51_000.0, 5.1, "ENTER"),
                    mid=51_001.0, expiry=pd.Timestamp("2026-12-25 08:00", tz="UTC"))
    book.apply_funding("funding_carry", 1.25)
    book.closed_perp.append({"strategy": "calendar", "instrument": "BTCUSDT-PERP", "qty": 0.1,
                             "entry_ts": "2026-10-17T10:00:00+00:00", "exit_ts": "2026-10-17T19:00:00+00:00"})
    book.halted = {"switch": "DRAWDOWN", "detail": "x", "ts": "2026-10-17T19:40:05+00:00", "unflattened": []}
    book.last_decision_bar = now.floor("h")
    book.next_funding_ms = to_ms("2026-10-18 00:00")
    book.mark_to_market({ROOT: 50_100.0, "BTCUSDT-PERP": 50_050.0, "BTCUSDT_261225": 51_200.0})

    text = book.to_json()
    back = Book.from_json(text)
    assert back.to_json() == text
    s = back.strategies["funding_carry"]
    assert [(p.leg, p.qty) for p in s.positions] == [("spot", 0.2), ("perp", -0.2)]
    assert s.positions[0].entry_ts == now and s.funding == 1.25 and s.target == 1
    assert back.strategies["cash_and_carry"].positions[0].expiry == pd.Timestamp("2026-12-25 08:00", tz="UTC")
    assert back.halted["switch"] == "DRAWDOWN" and back.last_decision_bar == now.floor("h")

    path = tmp_path / ROOT / "book.json"
    back.save(path)
    assert Book.load(path).to_json() == text
    assert [p.name for p in path.parent.iterdir()] == ["book.json"]  # no temp file left behind
    assert "NaN" not in path.read_text(encoding="utf-8")


def test_book_accounting_matches_the_mark_convention():
    cfg = BacktestConfig()
    now = pd.Timestamp("2026-10-17 19:00", tz="UTC")
    book = Book.new(ROOT, "paper", ("funding_carry",), cfg, now)
    book.apply_fill(Fill(now, "funding_carry", "spot", ROOT, "buy", 0.2, 50_010.0, 10.0, "ENTER"), mid=50_000.0)
    book.apply_fill(Fill(now, "funding_carry", "perp", "BTCUSDT-PERP", "sell", 0.2, 49_990.0, 5.0, "ENTER"),
                    mid=50_000.0)
    mtm = book.mark_to_market({ROOT: 50_000.0, "BTCUSDT-PERP": 50_000.0})
    pnl = mtm["strategies"]["funding_carry"]["pnl"]
    assert pnl["fees"] == pytest.approx(15.0) and pnl["slippage"] == pytest.approx(0.2 * 10 + 0.2 * 10)
    assert pnl["basis"] == pytest.approx(0.0, abs=1e-9)  # at the entry mids, only costs are lost
    assert mtm["equity"] == pytest.approx(15_000 - 15.0 - 4.0)
    # close both legs 100 USDT higher: delta-neutral -> only costs again
    book.apply_fill(Fill(now, "funding_carry", "spot", ROOT, "sell", 0.2, 50_100.0, 10.0, "EXIT"), mid=50_100.0)
    book.apply_fill(Fill(now, "funding_carry", "perp", "BTCUSDT-PERP", "buy", 0.2, 50_100.0, 5.0, "EXIT"),
                    mid=50_100.0)
    mtm = book.mark_to_market({})
    assert book.strategies["funding_carry"].positions == [] and book.strategies["funding_carry"].target == 0
    assert mtm["equity"] == pytest.approx(15_000 - 30.0 - 4.0)
    assert book.closed_perp and book.closed_perp[0]["qty"] == pytest.approx(-0.2)


def test_ledger_appends_one_strict_json_object_per_line(tmp_path):
    led = Ledger(tmp_path / ROOT / "events.jsonl")
    led.append({"kind": "HEARTBEAT", "loop": 1, "x": np.float64("nan"), "n": np.int64(3), "ok": np.bool_(True)})
    led.append({"kind": "SNAPSHOT", "ts": pd.Timestamp("2026-10-17 19:40", tz="UTC"),
                "when": pd.Timestamp("2026-10-17 21:40", tz="Asia/Seoul"), "vals": [1.0, float("inf")]})
    with pytest.raises(ValueError):
        led.append({"kind": "NOT_A_KIND"})
    lines = led.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    recs = [json.loads(ln) for ln in lines]
    assert recs[0]["kind"] == "HEARTBEAT" and recs[0]["x"] is None and recs[0]["n"] == 3 and recs[0]["ok"] is True
    assert recs[1]["ts"] == "2026-10-17T19:40:00+00:00" and recs[1]["when"] == "2026-10-17T12:40:00+00:00"
    assert recs[1]["vals"] == [1.0, None]
    assert all(r["ts"].endswith("+00:00") for r in recs) and "NaN" not in "".join(lines)
    assert set(EVENT_KINDS) == {"HEARTBEAT", "SNAPSHOT", "DECISION", "FILL", "FUNDING_ACCRUAL", "EXPIRY", "KILL",
                                "SAFE", "HALT", "ERROR"}


def test_state_lock_is_exclusive(tmp_path):
    path = tmp_path / ROOT / "daemon.lock"
    with StateLock(path), pytest.raises(StateLocked):
        StateLock(path).acquire()
    with StateLock(path):  # released -> acquirable again
        pass


# =============================================================================================
# history (fake public client)
# =============================================================================================
HOUR_MS = 3_600_000
H_NOW = pd.Timestamp("2026-10-17 19:37:12", tz="UTC")
EXPIRED = pd.Timestamp("2026-09-25 08:00", tz="UTC")
LISTED = {"BTCUSDT_261225": (pd.Timestamp("2026-06-26 08:00", tz="UTC"), pd.Timestamp("2026-12-25 08:00", tz="UTC")),
          "BTCUSDT_270326": (EXPIRED, pd.Timestamp("2027-03-26 08:00", tz="UTC"))}
FRED_CSV = ("observation_date,DTB3\n2026-08-03,3.70\n2026-09-10,3.90\n2026-09-11,\n2026-09-14,.\n"
            "2026-10-15,4.10\n")


class FakePublic:
    """Stands in for _http.PublicClient: deterministic klines / funding / exchangeInfo / FRED by URL path."""

    def __init__(self, now=H_NOW, fred_text=FRED_CSV, fred_error=False):
        self.now_ms = to_ms(now)
        self.fred_text, self.fred_error = fred_text, fred_error
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def spot_px(open_ms: int) -> float:
        return 80_000.0 + (open_ms - to_ms("2026-01-01")) / HOUR_MS * 0.01

    def _window(self, symbol: str) -> tuple[int, int]:
        if symbol == "BTCUSDT_260925":  # expired; Binance still serves it, plus one zero-volume bar after expiry
            return to_ms("2026-03-27 08:00"), to_ms(EXPIRED)
        if symbol in LISTED:
            onboard, expiry = LISTED[symbol]
            return to_ms(onboard), to_ms(expiry) - HOUR_MS
        return to_ms("2026-01-01"), self.now_ms

    def _klines(self, path: str, params: dict) -> list:
        sym = params["symbol"]
        first, last = self._window(sym)
        start = max(int(params["startTime"]), first)
        end = min(int(params.get("endTime", self.now_ms)), last, self.now_ms)
        start = -(-start // HOUR_MS) * HOUR_MS
        rows = []
        for o in range(start, end + 1, HOUR_MS):
            s = self.spot_px(o)
            if path == "/api/v3/klines":
                px = s
            elif sym == ROOT:
                px = s * 1.0001
            else:
                expiry = to_ms(EXPIRED) if sym == "BTCUSDT_260925" else to_ms(LISTED[sym][1])
                dte = (expiry - (o + HOUR_MS)) / 86_400_000
                px = s * math.exp(0.06 * max(dte, 0.0) / 365.0)
            rows.append([o, f"{px:.2f}", f"{px * 1.001:.2f}", f"{px * 0.999:.2f}", f"{px:.2f}", "1",
                         o + HOUR_MS - 1, "1", 1, "1", "1", "0"])
            if len(rows) >= int(params["limit"]):
                break
        return rows

    def _funding(self, params: dict) -> list:
        out = []
        t = to_ms("2026-01-01")
        while t <= self.now_ms:
            ft = t + (3 if (t // HOUR_MS) % 2 else 0)  # real stamps carry a few ms of jitter
            if ft >= int(params["startTime"]) and ft <= int(params.get("endTime", self.now_ms)):
                out.append({"symbol": ROOT, "fundingTime": ft, "fundingRate": "0.00010000", "markPrice": "80000.0"})
            t += 8 * HOUR_MS
        return out[: int(params["limit"])]

    def get_json(self, url, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        path = urlsplit(url).path
        if path in ("/api/v3/klines", "/fapi/v1/klines"):
            return self._klines(path, params)
        if path == "/fapi/v1/fundingRate":
            return self._funding(params)
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [
                {"symbol": ROOT, "pair": ROOT, "contractType": "PERPETUAL", "deliveryDate": 4133404800000,
                 "status": "TRADING"},
                {"symbol": "BTCUSDT_261225", "pair": ROOT, "contractType": "CURRENT_QUARTER",
                 "deliveryDate": to_ms(LISTED["BTCUSDT_261225"][1]), "status": "TRADING"},
                {"symbol": "BTCUSDT_270326", "pair": ROOT, "contractType": "NEXT_QUARTER",
                 "deliveryDate": to_ms(LISTED["BTCUSDT_270326"][1]), "status": "TRADING"},
                {"symbol": "ETHUSDT_261225", "pair": "ETHUSDT", "contractType": "CURRENT_QUARTER",
                 "deliveryDate": to_ms(LISTED["BTCUSDT_261225"][1]), "status": "TRADING"},
            ]}
        raise AssertionError(f"unexpected GET {url}")

    def get_text(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if self.fred_error:
            raise HttpError(url, 503, "service unavailable")
        return self.fred_text


def test_recent_panel_satisfies_the_contract_and_the_selection_rule():
    client = FakePublic()
    p = history.recent_panel(ROOT, hours=24 * 45, client=client, now=H_NOW)
    validate_panel(p)
    assert len(p) == 24 * 45 and p.index[-1] == H_NOW.floor("h")  # complete bars only (19:00, not 20:00)
    assert p.attrs["r_usd_source"] == "fred"
    spot_calls = [c for c in client.calls if urlsplit(c[0]).path == "/api/v3/klines"]
    assert len(spot_calls) == 2  # 1080 bars at limit 1000 -> paginated
    # funding: settled prints at hour-floored stamps only
    fr = p["funding_rate"].dropna()
    assert (fr == 0.0001).all() and set(fr.index.hour) == {0, 8, 16} and len(fr) == 135
    # expiry hand-off: the expired 260925 leaves at its expiry bar, nq is empty until 270326's first bar
    at = lambda t: p.loc[pd.Timestamp(t, tz="UTC")]
    assert (at("2026-09-25 07:00")["fut_cq_symbol"], at("2026-09-25 07:00")["fut_nq_symbol"]) == (
        "BTCUSDT_260925", "BTCUSDT_261225")
    assert at("2026-09-25 08:00")["fut_cq_symbol"] == "BTCUSDT_261225" and at("2026-09-25 08:00")["fut_nq_symbol"] is None
    assert at("2026-09-25 09:00")["fut_nq_symbol"] == "BTCUSDT_270326"
    assert at("2026-09-25 07:00")["fut_cq_dte_days"] == pytest.approx(1 / 24)
    # r_usd: observation date d applies from d 00:00 UTC (no look-ahead), blanks skipped
    assert at("2026-09-09 23:00")["r_usd"] == pytest.approx(0.037)
    assert at("2026-09-10 00:00")["r_usd"] == pytest.approx(0.039)
    assert at("2026-10-14 23:00")["r_usd"] == pytest.approx(0.039)
    assert p["r_usd"].iloc[-1] == pytest.approx(0.041) and (p["y_coin"] == 0.0).all()


def test_recent_panel_falls_back_on_fred_failure_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="basis_arb.live.history"):
        p = history.recent_panel(ROOT, hours=24 * 3, client=FakePublic(fred_error=True), now=H_NOW)
    validate_panel(p)
    assert p.attrs["r_usd_source"] == "fallback"
    assert (p["r_usd"] == BacktestConfig().rates.fallback_r_usd).all()
    assert any("fallback" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_recent_panel_refuses_an_unconfigured_coin():
    with pytest.raises(ValueError):
        history.recent_panel("DOGEUSDT", hours=24, client=FakePublic(), now=H_NOW)


# =============================================================================================
# feed (fake public client) + live row
# =============================================================================================
F_NOW = pd.Timestamp("2026-10-17 19:40:00", tz="UTC")


class FeedClient:
    def __init__(self):
        n = to_ms(F_NOW)
        self.payloads = {
            "/sapi/v1/system/status": {"status": 0, "msg": "normal"},
            "/fapi/v1/exchangeInfo": {"symbols": [
                {"symbol": ROOT, "pair": ROOT, "contractType": "PERPETUAL", "deliveryDate": 4133404800000,
                 "status": "TRADING"},
                {"symbol": "BTCUSDT_270326", "pair": ROOT, "contractType": "NEXT_QUARTER",
                 "deliveryDate": to_ms("2027-03-26 08:00"), "status": "TRADING"},
                {"symbol": "BTCUSDT_261225", "pair": ROOT, "contractType": "CURRENT_QUARTER",
                 "deliveryDate": to_ms("2026-12-25 08:00"), "status": "TRADING"},
            ]},
            ("/api/v3/ticker/bookTicker", ROOT): {"symbol": ROOT, "bidPrice": "80000.00", "bidQty": "1",
                                                   "askPrice": "80000.02", "askQty": "1"},
            ("/fapi/v1/ticker/bookTicker", ROOT): {"symbol": ROOT, "bidPrice": "79990.0", "askPrice": "79990.2",
                                                    "time": n - 400},
            ("/fapi/v1/premiumIndex", ROOT): {"symbol": ROOT, "markPrice": "79990.1", "indexPrice": "80000.5",
                                               "lastFundingRate": "0.00010000", "interestRate": "0.0001",
                                               "nextFundingTime": to_ms("2026-10-18 00:00"), "time": n - 100},
            ("/fapi/v1/ticker/bookTicker", "BTCUSDT_261225"): {"symbol": "BTCUSDT_261225", "bidPrice": "81000.0",
                                                                "askPrice": "81000.4", "time": n - 70_000},
            ("/fapi/v1/ticker/bookTicker", "BTCUSDT_270326"): {"symbol": "BTCUSDT_270326", "bidPrice": "82000.0",
                                                                "askPrice": "82001.0", "time": n - 90_000},
        }
        self.counts: Counter = Counter()

    def get_json(self, url, params=None):
        path = urlsplit(url).path
        key = (path, params["symbol"]) if params and "symbol" in params else path
        self.counts[key] += 1
        val = self.payloads[key]
        if isinstance(val, Exception):
            raise val
        return copy.deepcopy(val)


def make_feed(client=None) -> LiveFeed:
    return LiveFeed(client=client or FeedClient(), clock=lambda: F_NOW)


def test_feed_snapshot_parses_all_fields_and_caches_exchange_info():
    client = FeedClient()
    feed = make_feed(client)
    q = feed.snapshot(ROOT)
    assert (q.spot_bid, q.spot_ask, q.perp_bid, q.perp_ask) == (80_000.0, 80_000.02, 79_990.0, 79_990.2)
    assert (q.perp_mark, q.perp_index, q.last_funding_rate) == (79_990.1, 80_000.5, 0.0001)
    assert q.next_funding_time == pd.Timestamp("2026-10-18 00:00", tz="UTC")
    assert (q.cq_symbol, q.nq_symbol) == ("BTCUSDT_261225", "BTCUSDT_270326")  # ordered by expiry
    assert (q.cq_bid, q.cq_ask, q.cq_expiry) == (81_000.0, 81_000.4, pd.Timestamp("2026-12-25 08:00", tz="UTC"))
    # staleness follows the exchange clock of the core legs, not the thin quarterly book (70-90 s old here)
    assert q.server_time == F_NOW - pd.Timedelta(milliseconds=400)
    assert q.age_s(F_NOW) == pytest.approx(0.4) and not q.is_stale(F_NOW, 300)
    assert q.is_stale(F_NOW + pd.Timedelta(minutes=6), 300)
    assert q.mid("BTCUSDT-PERP") == pytest.approx(79_990.1) and q.book("BTCUSDT_270326") == (82_000.0, 82_001.0)
    with pytest.raises(KeyError):
        q.book("BTCUSDT_990101")
    feed.snapshot(ROOT)
    assert client.counts["/fapi/v1/exchangeInfo"] == 1  # cached for an hour


@pytest.mark.parametrize("case", ["http", "missing", "maintenance", "crossed", "zero", "perp_halted"])
def test_feed_fails_secure(case):
    client = FeedClient()
    if case == "http":
        client.payloads[("/api/v3/ticker/bookTicker", ROOT)] = HttpError("u", 503, "maintenance window")
    elif case == "missing":
        del client.payloads[("/fapi/v1/premiumIndex", ROOT)]["markPrice"]
    elif case == "maintenance":
        client.payloads["/sapi/v1/system/status"] = {"status": 1, "msg": "system maintenance"}
    elif case == "crossed":
        client.payloads[("/fapi/v1/ticker/bookTicker", ROOT)]["bidPrice"] = "79999.0"
        client.payloads[("/fapi/v1/ticker/bookTicker", ROOT)]["askPrice"] = "79990.0"
    elif case == "zero":
        client.payloads[("/fapi/v1/ticker/bookTicker", "BTCUSDT_261225")]["bidPrice"] = "0.0"
    elif case == "perp_halted":
        client.payloads["/fapi/v1/exchangeInfo"]["symbols"][0]["status"] = "SETTLING"
    with pytest.raises(FeedError):
        make_feed(client).snapshot(ROOT)


def test_feed_skips_quarterlies_that_are_not_trading():
    client = FeedClient()
    client.payloads["/fapi/v1/exchangeInfo"]["symbols"][1]["status"] = "PENDING_TRADING"
    q = make_feed(client).snapshot(ROOT)
    assert q.cq_symbol == "BTCUSDT_261225" and q.nq_symbol is None and math.isnan(q.nq_bid)
    assert client.counts[("/fapi/v1/ticker/bookTicker", "BTCUSDT_270326")] == 0


def mkquote(ts, *, spot=50_000.0, perp=None, cq=("SYN_CQ", 50_300.0, CQ_EXP), nq=("SYN_NQ", 51_500.0, NQ_EXP),
            next_funding=None, age_s=0.0, half=0.5, index=None) -> LiveQuote:
    t = utc(ts) - pd.Timedelta(seconds=age_s)
    perp = spot if perp is None else perp
    nf = next_funding if next_funding is not None else utc(ts).floor("8h") + pd.Timedelta(hours=8)
    legs = []
    for leg in (cq, nq):
        legs += [None, np.nan, np.nan, None] if leg is None else [leg[0], leg[1] - half, leg[1] + half, leg[2]]
    return LiveQuote(
        root=ROOT, ts=t, spot_bid=spot - half, spot_ask=spot + half, perp_bid=perp - half, perp_ask=perp + half,
        perp_mark=perp, perp_index=spot if index is None else index, last_funding_rate=0.0003,
        next_funding_time=nf, cq_symbol=legs[0], cq_bid=legs[1], cq_ask=legs[2], cq_expiry=legs[3],
        nq_symbol=legs[4], nq_bid=legs[5], nq_ask=legs[6], nq_expiry=legs[7], server_time=t, latency_ms=5.0)


def test_append_live_row_builds_the_in_progress_bar_and_passes_the_contract():
    p = make_panel(n_hours=24 * 3)
    now = p.index[-1] + pd.Timedelta(minutes=37)
    out = append_live_row(p, mkquote(now, spot=50_100.0, perp=50_120.0))
    validate_panel(out)
    row = out.iloc[-1]
    assert out.index[-1] == p.index[-1] + pd.Timedelta(hours=1) and len(out) == len(p) + 1
    assert row["spot_close"] == row["spot_high"] == row["spot_low"] == 50_100.0 and row["perp_close"] == 50_120.0
    assert row["fut_cq_symbol"] == "SYN_CQ" and row["fut_cq_close"] == 50_300.0
    assert row["fut_cq_dte_days"] == pytest.approx((CQ_EXP - out.index[-1]) / pd.Timedelta(days=1), abs=1e-9)
    assert math.isnan(row["funding_rate"]) and row["r_usd"] == p["r_usd"].iloc[-1]
    assert (out.dtypes == p.dtypes).all()
    again = append_live_row(out, mkquote(now + pd.Timedelta(minutes=5), spot=50_200.0))  # same hour: replaced
    assert len(again) == len(out) and again["spot_close"].iloc[-1] == 50_200.0
    with pytest.raises(ValueError):
        append_live_row(out, mkquote(p.index[-3]))  # a live row may not precede the panel


def test_append_live_row_applies_the_expiry_rule_at_the_row_ts():
    p = make_panel(n_hours=24 * 60 + 8, n_contracts=3)  # ends 2025-03-02 07:00, one hour before CQ_EXP
    out = append_live_row(p, mkquote(p.index[-1] + pd.Timedelta(minutes=30)))  # row ts 08:00 == CQ expiry
    row = out.iloc[-1]
    assert row["fut_cq_symbol"] == "SYN_NQ" and row["fut_nq_symbol"] is None and math.isnan(row["fut_nq_close"])
    validate_panel(out)


# =============================================================================================
# daemon with fakes
# =============================================================================================
class Clock:
    def __init__(self, t):
        self.t = utc(t)

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = utc(t)


class FakeFeed:
    """snapshot() returns `self.quote` (or raises it if it is an exception)."""

    def __init__(self):
        self.quote = None
        self.calls = 0

    def snapshot(self, root):
        self.calls += 1
        if isinstance(self.quote, Exception):
            raise self.quote
        return self.quote


class FakeFunding:
    def __init__(self):
        self.prints: list[tuple[pd.Timestamp, float, float]] = []
        self.calls = 0

    def __call__(self, root, start_ms, end_ms=None):
        self.calls += 1
        rows = [(utc(s), r, m, to_ms(s) + 3) for s, r, m in self.prints]
        rows = [x for x in rows if x[3] >= start_ms and (end_ms is None or x[3] <= end_ms)]
        idx = pd.DatetimeIndex([x[0] for x in rows], name="ts") if rows else pd.DatetimeIndex([], tz="UTC", name="ts")
        return pd.DataFrame({"rate": [x[1] for x in rows], "mark_price": [x[2] for x in rows],
                             "funding_time_ms": [x[3] for x in rows]}, index=idx)


def fake_targets(monkeypatch, overrides: dict, fail: list | None = None):
    """Wrap the real signals.build_targets; override the LAST row of chosen strategies; optionally raise."""
    real = signals.build_targets
    calls = []

    def fake(panel, cfg, strategies=None):
        calls.append(len(panel))
        if fail and fail[0] > 0:
            fail[0] -= 1
            raise RuntimeError("synthetic decision-path failure")
        out = real(panel, cfg, strategies)
        for name, (t, c) in overrides.items():
            if name in out:
                df = out[name].copy()
                df.iloc[-1, df.columns.get_loc("target")] = t
                df.iloc[-1, df.columns.get_loc("contract")] = c
                df.iloc[-1, df.columns.get_loc("reason")] = "TEST"
                out[name] = df
        return out

    monkeypatch.setattr(daemon_mod.model_signals, "build_targets", fake)
    return calls


def make_daemon(tmp_path, clock, feed, panel, strategies=("funding_carry",), funding=None, **kw) -> Daemon:
    return Daemon(ROOT, "paper", cfg=BacktestConfig(), strategies=strategies, state_dir=tmp_path, feed=feed,
                  broker=PaperBroker(), history_fn=lambda r: panel, funding_fn=funding or FakeFunding(),
                  clock=clock, sleep_fn=lambda s: None, **kw)


def events(tmp_path, kind=None) -> list[dict]:
    evs = Ledger(state_paths(tmp_path, ROOT).events).read()
    return [e for e in evs if kind is None or e["kind"] == kind]


PANEL_END = START + pd.Timedelta(hours=24 * 45 - 1)  # make_panel(n_hours=24*45) ends 2025-02-14 23:00


def test_entry_is_reconciled_to_fills_through_the_backtest_signal_path(tmp_path, monkeypatch):
    assert daemon_mod.model_signals is signals  # the decision path IS model.signals (research/live parity)
    real, seen = signals.build_targets, []
    monkeypatch.setattr(daemon_mod.model_signals, "build_targets",
                        lambda panel, cfg, strategies=None: seen.append(panel.index[-1]) or real(panel, cfg, strategies))
    panel = make_panel(n_hours=24 * 45, funding_per_period=0.0003)  # trailing funding >> hurdle -> S2 target +1
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    feed.quote = mkquote(clock())
    out: list[str] = []
    st = run(ROOT, "paper", once=True, strategies=("funding_carry",), state_dir=tmp_path, feed=feed,
             broker=PaperBroker(), history_fn=lambda r: panel, funding_fn=FakeFunding(), clock=clock,
             sleep_fn=lambda s: None, out=out.append)
    assert seen == [PANEL_END + pd.Timedelta(hours=1)]  # signals ran on the panel + the live row
    assert st["signals"]["funding_carry"]["target"] == 1 and st["decision_loop"] and not st["halted"]
    fills = events(tmp_path, "FILL")
    assert [(f["leg"], f["side"], f["reason"]) for f in fills] == [("spot", "buy", "ENTER"), ("perp", "sell", "ENTER")]
    assert fills[0]["qty"] == pytest.approx(10_000 / 50_000.0) == pytest.approx(fills[1]["qty"])
    assert fills[0]["price"] == pytest.approx(50_000.5 * 1.0002) and fills[1]["price"] == pytest.approx(49_999.5 * 0.9998)
    dec = events(tmp_path, "DECISION")
    assert len(dec) == 1 and dec[0]["strategies"]["funding_carry"]["action"] == "ENTER"
    book = Book.load(state_paths(tmp_path, ROOT).book)
    assert book.strategies["funding_carry"].target == 1 and len(book.strategies["funding_carry"].positions) == 2
    assert "basis_arb live | BTCUSDT | paper" in out[0] and "funding_carry" in out[0]
    kinds = [e["kind"] for e in events(tmp_path)]
    assert kinds[0] == "HEARTBEAT" and {"SNAPSHOT", "DECISION", "FILL"} <= set(kinds) and kinds[-1] == "HEARTBEAT"


def test_decides_once_per_bar(tmp_path, monkeypatch):
    overrides = {"funding_carry": (1, None)}
    fake_targets(monkeypatch, overrides)
    panel = make_panel(n_hours=24 * 45)
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, panel) as d:
        feed.quote = mkquote(clock())
        assert d.step()["decision_loop"]
        overrides["funding_carry"] = (0, None)  # the target flips intra-bar ...
        clock.set(clock() + pd.Timedelta(minutes=20))
        feed.quote = mkquote(clock())
        st = d.step()
        assert not st["decision_loop"] and st["fills"] == []  # ... but nothing trades until the bar closes
        clock.set(PANEL_END + pd.Timedelta(hours=1, seconds=30))
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["decision_loop"] and [f["reason"] for f in st["fills"]] == ["EXIT", "EXIT"]
    assert len(events(tmp_path, "DECISION")) == 2


def test_safe_on_stale_quote_blocks_entries(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, make_panel(n_hours=24 * 45)) as d:
        feed.quote = mkquote(clock(), age_s=6 * 60)  # 6 min old
        st = d.step()
        assert st["safe_codes"] == ["STALE_QUOTE"] and st["fills"] == [] and not st["halted"]
        safe = events(tmp_path, "SAFE")
        assert len(safe) == 1 and safe[0]["active"] and safe[0]["codes"] == ["STALE_QUOTE"]
        assert events(tmp_path, "DECISION") == [] and events(tmp_path, "HEARTBEAT")[-1]["status"] == "SAFE"
        clock.set(clock() + pd.Timedelta(minutes=1))
        feed.quote = mkquote(clock())  # fresh again: SAFE clears and the pending decision is taken
        st = d.step()
        assert st["safe_codes"] == [] and len(st["fills"]) == 2
        assert events(tmp_path, "SAFE")[-1]["active"] is False


def test_halt_and_flatten_after_30_minutes_without_a_fresh_quote(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    t0 = PANEL_END + pd.Timedelta(seconds=30)
    clock, feed = Clock(t0), FakeFeed()
    panel = make_panel(n_hours=24 * 45)
    d = make_daemon(tmp_path, clock, feed, panel)
    feed.quote = mkquote(t0)
    assert len(d.step()["fills"]) == 2
    feed.quote = FeedError("HTTP 503 from bookTicker")
    clock.set(t0 + pd.Timedelta(minutes=11))
    st = d.step()
    assert st["safe_codes"] == ["FEED_ERROR"] and not st["halted"]
    assert len(d.book.strategies["funding_carry"].positions) == 2  # SAFE keeps positions
    clock.set(t0 + pd.Timedelta(minutes=31))
    st = d.step()
    assert st["halted"] and d.halted
    kill = events(tmp_path, "KILL")
    assert len(kill) == 1 and kill[0]["switch"] == "STALE_QUOTE_30MIN"
    flat = [f for f in events(tmp_path, "FILL") if f["reason"] == "KILL"]
    assert [(f["leg"], f["side"]) for f in flat] == [("spot", "sell"), ("perp", "buy")]
    assert all(f["stale_quote"] for f in flat)  # paper flatten on the last (stale) quote, flagged as such
    assert events(tmp_path, "HALT")[-1]["switch"] == "STALE_QUOTE_30MIN"
    assert d.book.strategies["funding_carry"].positions == [] and d.book.halted["switch"] == "STALE_QUOTE_30MIN"
    d.close()
    with pytest.raises(StartupRefused):  # a halted book needs a deliberate restart
        make_daemon(tmp_path, clock, feed, panel)
    make_daemon(tmp_path, clock, feed, panel, clear_halt=True).close()
    assert events(tmp_path, "HEARTBEAT")[-1]["phase"] == "clear_halt"


def test_no_entry_while_safe_after_a_decision_path_exception(tmp_path, monkeypatch):
    fail = [1]
    fake_targets(monkeypatch, {"funding_carry": (1, None)}, fail=fail)
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, make_panel(n_hours=24 * 45)) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["safe_codes"] == ["DECISION_EXCEPTION"] and st["fills"] == []
        assert events(tmp_path, "ERROR")[0]["where"] == "decision"
        clock.set(PANEL_END + pd.Timedelta(hours=1, seconds=30))  # next bar, signal path healthy, target +1
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["decision_loop"] and st["fills"] == [] and st["safe_codes"] == ["DECISION_EXCEPTION"]
        assert st["actions"]["funding_carry"] == "BLOCKED_SAFE"
        dec = events(tmp_path, "DECISION")[-1]
        assert dec["entries_allowed"] is False and dec["strategies"]["funding_carry"]["target"] == 1


def test_exits_are_still_allowed_in_safe(tmp_path, monkeypatch):
    overrides, fail = {"funding_carry": (1, None)}, [0]
    fake_targets(monkeypatch, overrides, fail=fail)
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, make_panel(n_hours=24 * 45)) as d:
        feed.quote = mkquote(clock())
        assert len(d.step()["fills"]) == 2
        fail[0] = 1  # next loop: decision-path exception -> sticky SAFE
        clock.set(clock() + pd.Timedelta(minutes=1))
        feed.quote = mkquote(clock())
        assert d.step()["safe_codes"] == ["DECISION_EXCEPTION"]
        overrides["funding_carry"] = (0, None)
        clock.set(PANEL_END + pd.Timedelta(hours=1, seconds=30))
        feed.quote = mkquote(clock())
        st = d.step()
        assert [(f["leg"], f["reason"]) for f in st["fills"]] == [("spot", "EXIT"), ("perp", "EXIT")]
        assert d.book.strategies["funding_carry"].positions == [] and st["safe_codes"] == ["DECISION_EXCEPTION"]


def test_drawdown_beyond_5_percent_flattens_and_halts(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    d = make_daemon(tmp_path, clock, feed, make_panel(n_hours=24 * 45))
    feed.quote = mkquote(clock())
    assert len(d.step()["fills"]) == 2
    clock.set(clock() + pd.Timedelta(minutes=1))
    feed.quote = mkquote(clock(), spot=46_000.0, perp=50_000.0)  # long spot -8%, short perp flat: -800 USDT
    st = d.step()
    assert st["halted"] and events(tmp_path, "KILL")[-1]["switch"] == "DRAWDOWN"
    assert st["mtm"]["drawdown"] > 0.05
    assert [(f["leg"], f["side"], f["reason"]) for f in st["fills"]] == [("spot", "sell", "KILL"), ("perp", "buy", "KILL")]
    assert not any(f["stale_quote"] for f in st["fills"])
    assert events(tmp_path, "HALT")[-1]["switch"] == "DRAWDOWN" and d.book.halted is not None
    d.close()


def test_funding_accrues_once_to_legs_held_before_the_stamp(tmp_path, monkeypatch):
    """Stamp 00:00. Strategy A (funding_carry) is short perp from 23:00 and exits at 00:00:30; strategy B
    (random_maturity) enters at 00:00:30. The settled print is published late (seen at 00:01:30): A still
    receives it (held at the stamp), B does not (opened after), and nothing is accrued twice."""
    overrides = {"funding_carry": (1, None), "random_maturity": (0, None)}
    fake_targets(monkeypatch, overrides)
    stamp = PANEL_END + pd.Timedelta(hours=1)  # 2025-02-15 00:00
    clock, feed, funding = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed(), FakeFunding()
    with make_daemon(tmp_path, clock, feed, make_panel(n_hours=24 * 45),
                     strategies=("funding_carry", "random_maturity"), funding=funding) as d:
        feed.quote = mkquote(clock(), next_funding=stamp)
        assert len(d.step()["fills"]) == 2
        assert d.book.next_funding_ms == to_ms(stamp)

        overrides.update({"funding_carry": (0, None), "random_maturity": (1, None)})
        clock.set(stamp + pd.Timedelta(seconds=30))  # stamp crossed; print not yet published
        feed.quote = mkquote(clock(), next_funding=stamp + pd.Timedelta(hours=8))
        st = d.step()
        assert st["funding"] == [] and d.book.funding_due_ms == to_ms(stamp)
        assert sorted((f["strategy"], f["reason"]) for f in st["fills"]) == [
            ("funding_carry", "EXIT"), ("funding_carry", "EXIT"), ("random_maturity", "ENTER"),
            ("random_maturity", "ENTER")]

        funding.prints = [(stamp, 0.0001, 50_010.0)]
        clock.set(stamp + pd.Timedelta(seconds=90))
        feed.quote = mkquote(clock(), next_funding=stamp + pd.Timedelta(hours=8))
        st = d.step()
        assert len(st["funding"]) == 1
        acc = events(tmp_path, "FUNDING_ACCRUAL")
        assert len(acc) == 1 and acc[0]["strategy"] == "funding_carry" and acc[0]["closed_leg"] is True
        assert acc[0]["amount"] == pytest.approx(0.2 * 0.0001 * 50_010.0)  # short receives positive funding
        assert d.book.strategies["funding_carry"].funding == pytest.approx(0.2 * 0.0001 * 50_010.0)
        assert d.book.strategies["random_maturity"].funding == 0.0
        assert d.book.funding_due_ms is None and d.book.closed_perp == []
        assert d.panel.loc[stamp, "funding_rate"] == pytest.approx(0.0001)  # settled print patched into the panel

        clock.set(stamp + pd.Timedelta(minutes=5))
        feed.quote = mkquote(clock(), next_funding=stamp + pd.Timedelta(hours=8))
        assert d.step()["funding"] == [] and len(events(tmp_path, "FUNDING_ACCRUAL")) == 1


def test_held_quarterly_settles_at_expiry_and_the_remaining_leg_is_closed(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"cash_and_carry": (1, "SYN_CQ")})  # hold the CQ into its expiry
    panel = make_panel(n_hours=24 * 60 + 8, n_contracts=3)  # ends 2025-03-02 07:00
    clock, feed = Clock(panel.index[-1] + pd.Timedelta(seconds=30)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, panel, strategies=("cash_and_carry",)) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert [(f["instrument"], f["side"]) for f in st["fills"]] == [(ROOT, "buy"), ("SYN_CQ", "sell")]
        entry = st["fills"][1]["price"]
        clock.set(CQ_EXP + pd.Timedelta(seconds=30))
        feed.quote = mkquote(clock(), cq=("SYN_NQ", 51_500.0, NQ_EXP), nq=("SYN_Q3", 52_700.0, Q3_EXP),
                             index=50_050.0)
        st = d.step()
        (exp,) = events(tmp_path, "EXPIRY")
        assert exp["instrument"] == "SYN_CQ" and exp["settle_price"] == 50_050.0 and exp["price_source"] == "index"
        assert exp["delivery_fee"] == pytest.approx(0.2 * 50_050.0 * 5e-4)
        assert exp["realized"] == pytest.approx(-0.2 * (50_050.0 - entry))
        assert [(f["instrument"], f["side"], f["reason"]) for f in st["fills"]] == [(ROOT, "sell", "EXIT")]
        assert d.book.strategies["cash_and_carry"].positions == [] and d.book.strategies["cash_and_carry"].n_expiries == 1


def test_testnet_mode_refuses_to_start_without_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    with pytest.raises(MissingCredentials, match="BINANCE_TESTNET_API_KEY"):
        run(ROOT, "testnet", once=True, state_dir=tmp_path, feed=FakeFeed(), out=None)
    assert not (tmp_path / ROOT).exists()  # refused before any state was touched
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "only-the-key")
    assert main(["--root", ROOT, "--mode", "testnet", "--once", "--state-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "BINANCE_TESTNET_API_SECRET" in err and "only-the-key" not in err


def test_a_second_daemon_on_the_same_book_is_refused(tmp_path):
    clock, feed = Clock(PANEL_END), FakeFeed()
    panel = make_panel(n_hours=24 * 3)
    with make_daemon(tmp_path, clock, feed, panel), pytest.raises(StateLocked):
        make_daemon(tmp_path, clock, feed, panel)


class NoQuarterlyBroker(PaperBroker):
    """Paper fills, but quarterlies are 'not listed' (the testnet situation for a PENDING_TRADING contract)."""

    def supports(self, leg, instrument):
        return leg != "fut"


class SecondLegFailsBroker(PaperBroker):
    def __init__(self):
        super().__init__()
        self.n = 0

    def place(self, order, quote):
        self.n += 1
        if self.n == 2:
            from basis_arb.live.broker import BrokerError

            raise BrokerError("synthetic rejection of the second leg")
        return super().place(order, quote)


def test_unexpected_feed_exception_is_a_non_sticky_safe(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, make_panel(n_hours=24 * 45)) as d:
        feed.quote = ValueError("malformed payload")
        st = d.step()
        assert st["safe_codes"] == ["FEED_ERROR"] and d.sticky_safe is None and st["fills"] == []
        feed.quote = mkquote(clock())
        assert len(d.step()["fills"]) == 2  # recovers on the next good snapshot


def test_quarterly_not_tradable_on_the_venue_is_skipped_not_half_entered(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"cash_and_carry": (1, "SYN_CQ")})
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with Daemon(ROOT, "paper", strategies=("cash_and_carry",), state_dir=tmp_path, feed=feed,
                broker=NoQuarterlyBroker(), history_fn=lambda r: make_panel(n_hours=24 * 45),
                funding_fn=FakeFunding(), clock=clock) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["actions"]["cash_and_carry"] == "UNSUPPORTED" and st["fills"] == []
        assert d.sticky_safe is None and d.book.strategies["cash_and_carry"].positions == []


def test_failed_second_leg_unwinds_the_first_and_blocks_entries(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with Daemon(ROOT, "paper", strategies=("funding_carry",), state_dir=tmp_path, feed=feed,
                broker=SecondLegFailsBroker(), history_fn=lambda r: make_panel(n_hours=24 * 45),
                funding_fn=FakeFunding(), clock=clock) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["actions"]["funding_carry"] == "UNWOUND"
        assert [(f["leg"], f["side"], f["reason"]) for f in st["fills"]] == [("spot", "buy", "ENTER"),
                                                                            ("spot", "sell", "KILL")]
        assert d.book.strategies["funding_carry"].positions == [] and st["safe_codes"] == ["BROKER_FAILURE"]
        clock.set(PANEL_END + pd.Timedelta(hours=1, seconds=30))
        feed.quote = mkquote(clock())
        st = d.step()  # next bar: target still +1, but entries stay blocked until restart
        assert st["fills"] == [] and st["actions"]["funding_carry"] == "BLOCKED_SAFE"


def test_step_is_inert_after_a_halt(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(PANEL_END + pd.Timedelta(seconds=30)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, make_panel(n_hours=24 * 45)) as d:
        feed.quote = mkquote(clock())
        d.step()
        clock.set(clock() + pd.Timedelta(minutes=1))
        feed.quote = mkquote(clock(), spot=46_000.0, perp=50_000.0)
        assert d.step()["halted"]
        n_events = len(events(tmp_path))
        clock.set(PANEL_END + pd.Timedelta(hours=1, seconds=30))
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["halted"] and st["fills"] == [] and len(events(tmp_path)) == n_events
    assert daemon_mod.EXIT_HALT == 4  # distinct from a crash (1) for supervisors


# =============================================================================================
# network smoke (real public endpoints, paper mode)
# =============================================================================================
@pytest.mark.network
def test_paper_once_against_live_endpoints(tmp_path, capsys):
    st = run(root="BTCUSDT", mode="paper", once=True, state_dir=tmp_path)
    paths = state_paths(tmp_path, "BTCUSDT")
    assert paths.book.exists() and paths.events.exists()
    assert not st["halted"] and st["quote"] is not None and st["quote_fresh"]
    assert set(st["signals"]) == {"funding_carry", "cash_and_carry", "calendar", "random_maturity"}
    kinds = [e["kind"] for e in Ledger(paths.events).read()]
    assert "SNAPSHOT" in kinds and "HEARTBEAT" in kinds
    assert "basis_arb live | BTCUSDT | paper" in capsys.readouterr().out
