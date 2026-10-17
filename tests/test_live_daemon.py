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
from basis_arb.live.broker import BrokerError, PaperBroker, UnsupportedOnTestnet
from basis_arb.live.daemon import Daemon, MissingCredentials, StartupRefused, main, run
from basis_arb.live.feed import FeedError, LiveFeed, LiveQuote, append_live_row
from basis_arb.live.state import (
    EVENT_KINDS,
    Book,
    Ledger,
    StateLock,
    StateLocked,
    iso,
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

    def __init__(self, now=H_NOW, fred_text=FRED_CSV, fred_error=False, spot_gaps=()):
        self.now_ms = to_ms(now)
        self.fred_text, self.fred_error = fred_text, fred_error
        self.spot_gaps = [(to_ms(s), to_ms(s) + h * HOUR_MS) for s, h in spot_gaps]  # missing bar ENDS [s, s+h)
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
            if path == "/api/v3/klines" and any(a <= o + HOUR_MS < b for a, b in self.spot_gaps):
                continue  # exchange outage: no spot bar
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
    # r_usd: the observation for date d applies from d + 1 00:00 UTC (publication lag), blanks skipped
    assert at("2026-09-10 23:00")["r_usd"] == pytest.approx(0.037)
    assert at("2026-09-11 00:00")["r_usd"] == pytest.approx(0.039)
    assert at("2026-10-15 23:00")["r_usd"] == pytest.approx(0.039)
    assert at("2026-10-16 00:00")["r_usd"] == pytest.approx(0.041)
    assert p["r_usd"].iloc[-1] == pytest.approx(0.041) and (p["y_coin"] == 0.0).all()
    assert (p["spot_high"].notna() & p["perp_high"].notna()).all()  # no outage in this window


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


def test_recent_panel_outages_follow_the_grid_rule(caplog):
    short = pd.Timestamp("2026-10-10 05:00", tz="UTC")  # 3 missing spot bars: forward-filled, high/low NaN
    p = history.recent_panel(ROOT, hours=24 * 45, client=FakePublic(spot_gaps=[(short, 3)]), now=H_NOW)
    validate_panel(p)  # complete 1h grid
    gap = p.loc[short: short + pd.Timedelta(hours=2)]
    assert len(p) == 24 * 45 and gap["spot_high"].isna().all() and gap["spot_low"].isna().all()
    assert (gap["spot_close"] == p.loc[short - pd.Timedelta(hours=1), "spot_close"]).all()
    assert signals.enrich(p, BacktestConfig()).loc[gap.index, "stale_bar"].all()  # the model blanks them
    long = pd.Timestamp("2026-10-01 00:00", tz="UTC")  # 7 missing bars > 5h: the window starts after it
    with caplog.at_level(logging.WARNING, logger="basis_arb.live.history"):
        p2 = history.recent_panel(ROOT, hours=24 * 45, client=FakePublic(spot_gaps=[(long, 7)]), now=H_NOW)
    validate_panel(p2)
    assert p2.index[0] == long + pd.Timedelta(hours=7) and p2.index[-1] == H_NOW.floor("h")
    assert any("outage" in r.getMessage() for r in caplog.records)


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


def closed_history(panel: pd.DataFrame, clock: Clock):
    """history_fn stand-in: the bars of `panel` that have closed by the clock's time (no provisional row)."""
    return lambda root: panel[panel.index <= clock().floor("h")]


def fake_targets(monkeypatch, overrides: dict, fail: list | None = None):
    """Wrap the real signals.build_targets; override the LAST row of chosen strategies; optionally raise."""
    real = signals.build_targets
    calls: list[pd.Timestamp] = []

    def fake(panel, cfg, strategies=None):
        calls.append(panel.index[-1])
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


PANEL = make_panel(n_hours=24 * 46)  # closed bars from 2025-01-01 00:00 to 2025-02-15 23:00
T0 = START + pd.Timedelta(hours=24 * 45 - 1)  # 2025-02-14 23:00, the last closed bar at the first loop


def make_daemon(tmp_path, clock, feed, panel=PANEL, strategies=("funding_carry",), funding=None, mode="paper",
                broker=None, **kw) -> Daemon:
    return Daemon(ROOT, mode, cfg=BacktestConfig(), strategies=strategies, state_dir=tmp_path, feed=feed,
                  broker=broker or PaperBroker(), history_fn=closed_history(panel, clock),
                  funding_fn=funding or FakeFunding(), clock=clock, sleep_fn=lambda s: None, **kw)


def events(tmp_path, kind=None, mode="paper") -> list[dict]:
    evs = Ledger(state_paths(tmp_path, ROOT, mode).events).read()
    return [e for e in evs if kind is None or e["kind"] == kind]


def at_bar(bar, seconds=30):
    return utc(bar) + pd.Timedelta(seconds=seconds)


def test_decisions_use_closed_bars_only_through_the_backtest_signal_path(tmp_path, monkeypatch):
    assert daemon_mod.model_signals is signals  # the decision path IS model.signals (research/live parity)
    real, seen = signals.build_targets, []
    monkeypatch.setattr(daemon_mod.model_signals, "build_targets",
                        lambda panel, cfg, strategies=None: seen.append(panel.index[-1]) or real(panel, cfg, strategies))
    panel = make_panel(n_hours=24 * 46, funding_per_period=0.0003)  # trailing funding >> hurdle -> S2 target +1
    clock, feed = Clock(at_bar(T0, 37 * 60)), FakeFeed()  # 37 minutes into the hour ending at T0 + 1h
    feed.quote = mkquote(clock())
    out: list[str] = []
    st = run(ROOT, "paper", once=True, strategies=("funding_carry",), state_dir=tmp_path, feed=feed,
             broker=PaperBroker(), history_fn=closed_history(panel, clock), funding_fn=FakeFunding(), clock=clock,
             sleep_fn=lambda s: None, out=out.append)
    assert seen == [T0]  # decided on the last CLOSED bar, not on the provisional row (T0 + 1h)
    assert st["live_row_ts"] == T0 + pd.Timedelta(hours=1)  # the provisional row exists, for marks only
    assert st["signals"]["funding_carry"]["target"] == 1 and st["decision_bar"] == T0 and not st["halted"]
    fills = events(tmp_path, "FILL")
    assert [(f["leg"], f["side"], f["reason"]) for f in fills] == [("spot", "buy", "ENTER"), ("perp", "sell", "ENTER")]
    assert fills[0]["qty"] == pytest.approx(10_000 / 50_000.0) == pytest.approx(fills[1]["qty"])
    assert fills[0]["price"] == pytest.approx(50_000.5 * 1.0002) and fills[1]["price"] == pytest.approx(49_999.5 * 0.9998)
    (dec,) = events(tmp_path, "DECISION")
    assert dec["bar"] == iso(T0) and dec["strategies"]["funding_carry"]["action"] == "ENTER"
    book = Book.load(state_paths(tmp_path, ROOT, "paper").book)
    assert book.strategies["funding_carry"].target == 1 and len(book.strategies["funding_carry"].positions) == 2
    assert "basis_arb live | BTCUSDT | paper" in out[0] and "decision input" in out[0]


def test_replay_of_closed_bars_matches_build_targets(tmp_path, monkeypatch):
    """48 hourly rows (make_panel semantics), fed to the daemon one closed bar per hour: the adopted
    (target, contract) per strategy equals model.signals.build_targets on the same closed bars."""
    p = make_panel(n_hours=48, basis_ann_cq=0.30, basis_ann_nq=0.30, perp_premium=0.006)
    spot = p["spot_close"]
    rich_basis = (p.index < p.index[20]) | ((p.index >= p.index[30]) & (p.index < p.index[40]))
    for col, mult in (("close", 1.0), ("high", 1.001), ("low", 0.999)):  # S1: basis below fair outside the windows
        fair = spot * np.exp(0.02 * p["fut_cq_dte_days"] / 365.0) * mult
        p.loc[~rich_basis, f"fut_cq_{col}"] = fair[~rich_basis]
    rich_prem = (p.index < p.index[12]) | (p.index >= p.index[25])
    for col, mult in (("close", 1.0), ("high", 1.001), ("low", 0.999)):  # S4: premium only in two windows
        p.loc[~rich_prem, f"perp_{col}"] = spot[~rich_prem] * mult
    validate_panel(p)
    strategies = ("cash_and_carry", "random_maturity", "funding_carry", "calendar")
    expected = signals.build_targets(p, BacktestConfig(), strategies)

    clock, feed, funding = Clock(at_bar(p.index[0])), FakeFeed(), FakeFunding()
    funding.prints = [(ts, float(r), 50_000.0) for ts, r in p["funding_rate"].dropna().items()]
    got = {n: [] for n in strategies}
    with make_daemon(tmp_path, clock, feed, panel=p, strategies=strategies, funding=funding) as d:
        for i, bar in enumerate(p.index):
            clock.set(at_bar(bar))
            row = p.iloc[i]
            feed.quote = mkquote(clock(), spot=float(row["spot_close"]), perp=float(row["perp_close"]),
                                 cq=("SYN_CQ", float(row["fut_cq_close"]), CQ_EXP),
                                 nq=("SYN_NQ", float(row["fut_nq_close"]), NQ_EXP))
            st = d.step()
            assert st["decision_bar"] == bar and len(d.closed) == i + 1  # one new closed bar per loop
            for n in strategies:
                sb = d.book.strategies[n]
                got[n].append((sb.signal_target, sb.signal_contract))
    for n in strategies:
        want = [(int(t), c if isinstance(c, str) else None)
                for t, c in zip(expected[n]["target"], expected[n]["contract"])]
        assert got[n] == want, n
    # the replay is not trivial: S1 and S4 enter and exit inside the 48 bars
    for n in ("cash_and_carry", "random_maturity"):
        reasons = set(expected[n]["reason"])
        assert {"ENTER", "EXIT"} <= reasons, (n, reasons)
    assert {e["kind"] for e in events(tmp_path)} >= {"DECISION", "FILL", "FUNDING_ACCRUAL"}


def test_decides_once_per_closed_bar(tmp_path, monkeypatch):
    overrides = {"funding_carry": (1, None)}
    calls = fake_targets(monkeypatch, overrides)
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed) as d:
        feed.quote = mkquote(clock())
        assert d.step()["decision_loop"]
        overrides["funding_carry"] = (0, None)  # whatever the model would say now ...
        clock.set(at_bar(T0, 20 * 60))
        feed.quote = mkquote(clock())
        st = d.step()
        assert not st["decision_loop"] and st["fills"] == [] and calls == [T0]  # ... nothing until a bar closes
        clock.set(at_bar(T0 + pd.Timedelta(hours=1)))
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["decision_loop"] and [f["reason"] for f in st["fills"]] == ["EXIT", "EXIT"]
        assert calls == [T0, T0 + pd.Timedelta(hours=1)]
    assert len(events(tmp_path, "DECISION")) == 2


def test_stale_quote_is_safe_entries_blocked_exits_allowed(tmp_path, monkeypatch):
    overrides = {"funding_carry": (1, None)}
    fake_targets(monkeypatch, overrides)
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed) as d:
        feed.quote = mkquote(clock(), age_s=6 * 60)  # 6 min old
        st = d.step()
        assert st["safe_codes"] == ["STALE_QUOTE"] and st["fills"] == [] and not st["halted"]
        assert st["actions"]["funding_carry"] == "BLOCKED_SAFE"
        (safe,) = events(tmp_path, "SAFE")
        assert safe["active"] and safe["codes"] == ["STALE_QUOTE"]
        assert events(tmp_path, "DECISION")[-1]["entries_allowed"] is False
        clock.set(at_bar(T0, 120))
        feed.quote = mkquote(clock())  # fresh again, same bar: the blocked entry waits for the next bar
        st = d.step()
        assert st["safe_codes"] == [] and st["fills"] == [] and st["actions"]["funding_carry"] == "WAIT_BAR"
        clock.set(at_bar(T0 + pd.Timedelta(hours=1)))
        feed.quote = mkquote(clock())
        assert len(d.step()["fills"]) == 2  # next closed bar: entered
        clock.set(at_bar(T0 + pd.Timedelta(hours=1), 50 * 60))
        feed.quote = mkquote(clock())  # a fresh poll late in that hour
        assert d.step()["fills"] == []
        overrides["funding_carry"] = (0, None)
        clock.set(at_bar(T0 + pd.Timedelta(hours=2)))
        feed.quote = mkquote(clock(), age_s=7 * 60)  # stale again at the next decision: the exit still goes through
        st = d.step()
        assert st["safe_codes"] == ["STALE_QUOTE"] and [f["reason"] for f in st["fills"]] == ["EXIT", "EXIT"]
        assert all(f["stale_quote"] for f in st["fills"])  # paper exit on a 7-min-old quote, flagged as such


def test_halt_after_30_minutes_without_a_fresh_quote_keeps_positions(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    t0 = at_bar(T0)
    clock, feed = Clock(t0), FakeFeed()
    d = make_daemon(tmp_path, clock, feed)
    feed.quote = mkquote(t0)
    assert len(d.step()["fills"]) == 2
    feed.quote = FeedError("HTTP 503 from bookTicker")
    clock.set(t0 + pd.Timedelta(minutes=11))
    st = d.step()
    assert st["safe_codes"] == ["FEED_ERROR"] and not st["halted"]
    clock.set(t0 + pd.Timedelta(minutes=31))
    n_fills = len(events(tmp_path, "FILL"))
    st = d.step()
    assert st["halted"] and d.halted
    (halt,) = events(tmp_path, "HALT")
    assert halt["switch"] == "STALE_QUOTE_30MIN" and halt["flattened"] is False and len(halt["positions_kept"]) == 2
    assert len(events(tmp_path, "FILL")) == n_fills and events(tmp_path, "KILL") == []  # nothing traded blind
    assert len(d.book.strategies["funding_carry"].positions) == 2 and d.book.halted["switch"] == "STALE_QUOTE_30MIN"
    clock.set(t0 + pd.Timedelta(minutes=40))
    feed.quote = mkquote(clock())
    assert d.step()["halted"] and len(events(tmp_path, "FILL")) == n_fills  # inert after HALT
    d.close()
    with pytest.raises(StartupRefused):  # a halted book needs a deliberate restart
        make_daemon(tmp_path, clock, feed)
    make_daemon(tmp_path, clock, feed, clear_halt=True).close()
    assert events(tmp_path, "HEARTBEAT")[-1]["phase"] == "clear_halt"
    assert daemon_mod.EXIT_HALT == 4


def test_cli_exit_code_is_4_after_a_halt(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "run", lambda *a, **k: {"halted": True})
    assert main(["--root", ROOT, "--mode", "paper", "--once", "--state-dir", str(tmp_path)]) == 4


def test_drawdown_beyond_5_percent_is_safe_without_flattening(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None), "random_maturity": (1, None)})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, strategies=("funding_carry", "random_maturity")) as d:
        feed.quote = mkquote(clock())
        assert len(d.step()["fills"]) == 4
        clock.set(at_bar(T0, 90))
        feed.quote = mkquote(clock(), spot=45_000.0, perp=50_000.0)  # long spot -10%, short perp flat
        st = d.step()
        assert "DRAWDOWN" in st["safe_codes"] and st["mtm"]["drawdown"] > 0.05 and not st["halted"]
        assert st["fills"] == [] and events(tmp_path, "KILL") == [] and events(tmp_path, "HALT") == []
        assert all(len(d.book.strategies[n].positions) == 2 for n in ("funding_carry", "random_maturity"))
        clock.set(at_bar(T0 + pd.Timedelta(hours=1)))
        feed.quote = mkquote(clock(), spot=50_000.0, perp=50_000.0)  # recovered: entries stay blocked
        st = d.step()
        assert "DRAWDOWN" in st["safe_codes"]


def test_margin_breach_on_an_isolated_leg_flattens_that_strategy_only(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None), "calendar": (1, "SYN_CQ")})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, strategies=("funding_carry", "calendar")) as d:
        feed.quote = mkquote(clock(), cq=("SYN_CQ", 50_000.0, CQ_EXP))
        assert len(d.step()["fills"]) == 4
        clock.set(at_bar(T0, 90))
        # +55% on everything: the hedge is delta-neutral, but the isolated short perp of S2 is past
        # 1/leverage - maintenance = 49.6%; S3 (cross collateral) is long perp / short quarterly: no breach
        feed.quote = mkquote(clock(), spot=77_500.0, perp=77_500.0, cq=("SYN_CQ", 77_500.0, CQ_EXP))
        st = d.step()
        (kill,) = events(tmp_path, "KILL")
        assert kill["switch"] == "MARGIN_BREACH" and kill["strategy"] == "funding_carry"
        assert kill["adverse_move"] == pytest.approx(77_500.0 / (49_999.5 * 0.9998) - 1.0)
        assert [(f["strategy"], f["leg"], f["reason"]) for f in st["fills"]] == [
            ("funding_carry", "perp", "LIQUIDATION"), ("funding_carry", "spot", "KILL")]
        fc, cal = d.book.strategies["funding_carry"], d.book.strategies["calendar"]
        assert fc.positions == [] and fc.penalties == pytest.approx(st["fills"][0]["qty"] * st["fills"][0]["price"] * 50e-4)
        assert len(cal.positions) == 2 and max(p.peak_adverse_move for p in cal.positions) > 0.5  # tracked only
        assert "MARGIN_BREACH" in st["safe_codes"] and not st["halted"]


def test_no_entry_while_safe_after_a_decision_path_exception(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)}, fail=[1])
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["safe_codes"] == ["DECISION_EXCEPTION"] and st["fills"] == [] and not st["decision_loop"]
        assert events(tmp_path, "ERROR")[0]["where"] == "decision"
        clock.set(at_bar(T0, 90))  # same bar: the failed decision is retried, entries stay blocked
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["decision_loop"] and st["fills"] == [] and st["actions"]["funding_carry"] == "BLOCKED_SAFE"
        assert st["safe_codes"] == ["DECISION_EXCEPTION"]
        assert events(tmp_path, "DECISION")[-1]["entries_allowed"] is False


def test_exits_are_still_allowed_in_safe(tmp_path, monkeypatch):
    overrides, fail = {"funding_carry": (1, None)}, [0]
    fake_targets(monkeypatch, overrides, fail=fail)
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed) as d:
        feed.quote = mkquote(clock())
        assert len(d.step()["fills"]) == 2
        fail[0] = 1  # the next decision raises -> sticky SAFE
        clock.set(at_bar(T0 + pd.Timedelta(hours=1)))
        feed.quote = mkquote(clock())
        assert d.step()["safe_codes"] == ["DECISION_EXCEPTION"]
        overrides["funding_carry"] = (0, None)
        clock.set(at_bar(T0 + pd.Timedelta(hours=1), 90))  # the retried decision says exit
        feed.quote = mkquote(clock())
        st = d.step()
        assert [(f["leg"], f["reason"]) for f in st["fills"]] == [("spot", "EXIT"), ("perp", "EXIT")]
        assert d.book.strategies["funding_carry"].positions == [] and st["safe_codes"] == ["DECISION_EXCEPTION"]


def test_funding_accrues_once_to_legs_held_before_the_stamp(tmp_path, monkeypatch):
    """Stamp 00:00. Strategy A (funding_carry) is short perp from 23:00 and exits on the 00:00 bar; strategy B
    (random_maturity) enters on that bar. The settled print is published late (seen at 00:01:30): A still
    receives it (held at the stamp), B does not (opened after), and nothing is accrued twice."""
    overrides = {"funding_carry": (1, None), "random_maturity": (0, None)}
    fake_targets(monkeypatch, overrides)
    stamp = T0 + pd.Timedelta(hours=1)  # 2025-02-15 00:00
    clock, feed, funding = Clock(at_bar(T0)), FakeFeed(), FakeFunding()
    with make_daemon(tmp_path, clock, feed, strategies=("funding_carry", "random_maturity"), funding=funding) as d:
        feed.quote = mkquote(clock(), next_funding=stamp)
        assert len(d.step()["fills"]) == 2
        assert d.book.next_funding_ms == to_ms(stamp)

        overrides.update({"funding_carry": (0, None), "random_maturity": (1, None)})
        clock.set(at_bar(stamp))  # stamp crossed; the closed panel already carries the print (history)
        feed.quote = mkquote(clock(), next_funding=stamp + pd.Timedelta(hours=8))
        st = d.step()
        assert st["funding"] == [] and d.book.funding_due_ms == to_ms(stamp) and st["decision_bar"] == stamp
        assert sorted((f["strategy"], f["reason"]) for f in st["fills"]) == [
            ("funding_carry", "EXIT"), ("funding_carry", "EXIT"), ("random_maturity", "ENTER"),
            ("random_maturity", "ENTER")]

        funding.prints = [(stamp, 0.0001, 50_010.0)]
        clock.set(at_bar(stamp, 90))
        feed.quote = mkquote(clock(), next_funding=stamp + pd.Timedelta(hours=8))
        st = d.step()
        (acc,) = events(tmp_path, "FUNDING_ACCRUAL")
        assert acc["strategy"] == "funding_carry" and acc["closed_leg"] is True and len(st["funding"]) == 1
        assert acc["amount"] == pytest.approx(0.2 * 0.0001 * 50_010.0)  # short receives positive funding
        assert d.book.strategies["funding_carry"].funding == pytest.approx(0.2 * 0.0001 * 50_010.0)
        assert d.book.strategies["random_maturity"].funding == 0.0
        assert d.book.funding_due_ms is None and d.book.closed_perp == []

        clock.set(at_bar(stamp, 300))
        feed.quote = mkquote(clock(), next_funding=stamp + pd.Timedelta(hours=8))
        assert d.step()["funding"] == [] and len(events(tmp_path, "FUNDING_ACCRUAL")) == 1


def test_decision_waits_for_a_missing_settled_print(tmp_path, monkeypatch):
    calls = fake_targets(monkeypatch, {})
    stamp = T0 + pd.Timedelta(hours=1)
    panel = PANEL.copy()
    panel.loc[stamp, "funding_rate"] = np.nan  # history fetched before the print was published
    clock, feed, funding = Clock(at_bar(T0)), FakeFeed(), FakeFunding()
    with make_daemon(tmp_path, clock, feed, panel=panel, funding=funding) as d:
        feed.quote = mkquote(clock(), next_funding=stamp)
        d.step()
        clock.set(at_bar(stamp))
        feed.quote = mkquote(clock(), next_funding=stamp + pd.Timedelta(hours=8))
        st = d.step()
        assert not st["decision_loop"] and "waiting" in st["waiting"] and calls == [T0]
        funding.prints = [(stamp, 0.0003, 50_000.0)]
        clock.set(at_bar(stamp, 60))
        st = d.step()
        assert st["decision_bar"] == stamp and d.closed.loc[stamp, "funding_rate"] == pytest.approx(0.0003)


def test_held_quarterly_settles_at_expiry_and_the_remaining_leg_is_closed(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"cash_and_carry": (1, "SYN_CQ")})  # hold the CQ into its expiry
    panel = make_panel(n_hours=24 * 60 + 12, n_contracts=3)
    last = CQ_EXP - pd.Timedelta(hours=1)  # 2025-03-02 07:00
    clock, feed = Clock(at_bar(last)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, panel=panel, strategies=("cash_and_carry",)) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert [(f["instrument"], f["side"]) for f in st["fills"]] == [(ROOT, "buy"), ("SYN_CQ", "sell")]
        entry = st["fills"][1]["price"]
        clock.set(at_bar(CQ_EXP))
        feed.quote = mkquote(clock(), cq=("SYN_NQ", 51_500.0, NQ_EXP), nq=("SYN_Q3", 52_700.0, Q3_EXP),
                             index=50_050.0)
        st = d.step()
        (exp,) = events(tmp_path, "EXPIRY")
        assert exp["instrument"] == "SYN_CQ" and exp["settle_price"] == 50_050.0 and exp["price_source"] == "index"
        assert exp["delivery_fee"] == pytest.approx(0.2 * 50_050.0 * 5e-4)
        assert exp["realized"] == pytest.approx(-0.2 * (50_050.0 - entry))
        assert [(f["instrument"], f["side"], f["reason"]) for f in st["fills"]] == [(ROOT, "sell", "EXIT")]
        assert d.book.strategies["cash_and_carry"].positions == [] and d.book.strategies["cash_and_carry"].n_expiries == 1


class NoQuarterlyBroker(PaperBroker):
    """Paper fills, but quarterlies are 'not listed' (the testnet situation for delivery contracts)."""

    name = "testnet-stub"

    def supports(self, leg, instrument):
        return leg != "fut"


class SecondLegFailsBroker(PaperBroker):
    def __init__(self):
        super().__init__()
        self.n = 0

    def place(self, order, quote):
        self.n += 1
        if self.n == 2:
            raise BrokerError("synthetic rejection of the second leg")
        return super().place(order, quote)


class ListingVanishesBroker(PaperBroker):
    """supports() says yes, place() then finds the quarterly unlisted (listing changed between the calls)."""

    def place(self, order, quote):
        if order.leg == "fut":
            raise UnsupportedOnTestnet(f"{order.instrument} is not listed on the futures testnet")
        return super().place(order, quote)


def test_testnet_mode_skips_unsupported_quarterly_strategies(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"cash_and_carry": (1, "SYN_CQ"), "funding_carry": (1, None)})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, strategies=("cash_and_carry", "funding_carry"), mode="testnet",
                     broker=NoQuarterlyBroker()) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["actions"] == {"cash_and_carry": "UNSUPPORTED", "funding_carry": "ENTER"}
        assert d.sticky == {} and d.book.strategies["cash_and_carry"].positions == []
        (dec,) = events(tmp_path, "DECISION", mode="testnet")
        assert dec["strategies"]["cash_and_carry"]["action"] == "UNSUPPORTED"
        assert dec["notes"]["cash_and_carry"]["unsupported"] == ["SYN_CQ"]


def test_unsupported_raised_by_the_broker_is_skipped_not_propagated(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"cash_and_carry": (1, "SYN_CQ")})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, strategies=("cash_and_carry",), mode="testnet",
                     broker=ListingVanishesBroker()) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["actions"]["cash_and_carry"] == "UNSUPPORTED" and d.sticky == {}
        assert [(f["leg"], f["reason"]) for f in st["fills"]] == [("spot", "ENTER"), ("spot", "EXIT")]  # unwound
        assert d.book.strategies["cash_and_carry"].positions == [] and st["errors"] == []


def test_paper_and_testnet_books_and_ledgers_are_separate(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    feed.quote = mkquote(clock())
    with make_daemon(tmp_path, clock, feed) as paper, \
            make_daemon(tmp_path, clock, feed, mode="testnet", broker=NoQuarterlyBroker()) as testnet:
        paper.step()
        testnet.step()
        assert paper.paths.dir == tmp_path / ROOT / "paper" and testnet.paths.dir == tmp_path / ROOT / "testnet"
    paper_fills, testnet_fills = events(tmp_path, "FILL"), events(tmp_path, "FILL", mode="testnet")
    assert {f["broker"] for f in paper_fills} == {"paper"} and {f["broker"] for f in testnet_fills} == {"testnet-stub"}
    assert Book.load(state_paths(tmp_path, ROOT, "testnet").book).mode == "testnet"


def test_unexpected_feed_exception_is_a_non_sticky_safe(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed) as d:
        feed.quote = ValueError("malformed payload")
        st = d.step()
        assert st["safe_codes"] == ["FEED_ERROR"] and d.sticky == {} and st["fills"] == []
        clock.set(at_bar(T0 + pd.Timedelta(hours=1)))
        feed.quote = mkquote(clock())
        assert len(d.step()["fills"]) == 2  # recovers at the next closed bar


def test_failed_second_leg_unwinds_the_first_and_blocks_entries(tmp_path, monkeypatch):
    fake_targets(monkeypatch, {"funding_carry": (1, None)})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed, broker=SecondLegFailsBroker()) as d:
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["actions"]["funding_carry"] == "UNWOUND"
        assert [(f["leg"], f["side"], f["reason"]) for f in st["fills"]] == [("spot", "buy", "ENTER"),
                                                                            ("spot", "sell", "KILL")]
        assert d.book.strategies["funding_carry"].positions == [] and st["safe_codes"] == ["BROKER_FAILURE"]
        clock.set(at_bar(T0 + pd.Timedelta(hours=1)))
        feed.quote = mkquote(clock())
        st = d.step()  # next bar: target still +1, but entries stay blocked until restart
        assert st["fills"] == [] and st["actions"]["funding_carry"] == "BLOCKED_SAFE"


def test_history_lag_pauses_decisions(tmp_path, monkeypatch):
    calls = fake_targets(monkeypatch, {})
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with Daemon(ROOT, "paper", strategies=("funding_carry",), state_dir=tmp_path, feed=feed, broker=PaperBroker(),
                history_fn=lambda r: PANEL[PANEL.index <= T0], funding_fn=FakeFunding(), clock=clock) as d:
        feed.quote = mkquote(clock())
        d.step()
        clock.set(at_bar(T0 + pd.Timedelta(hours=1), 120))  # the next bar never arrives from history
        feed.quote = mkquote(clock())
        st = d.step()
        assert st["safe_codes"] == ["HISTORY_LAG"] and not st["decision_loop"] and calls == [T0]
        assert "live row" not in str(st["safe"])  # no provisional row across a hole


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
    clock, feed = Clock(at_bar(T0)), FakeFeed()
    with make_daemon(tmp_path, clock, feed), pytest.raises(StateLocked):
        make_daemon(tmp_path, clock, feed)


# =============================================================================================
# network smoke (real public endpoints, paper mode)
# =============================================================================================
@pytest.mark.network
def test_paper_once_against_live_endpoints(tmp_path, capsys):
    st = run(root="BTCUSDT", mode="paper", once=True, state_dir=tmp_path)
    paths = state_paths(tmp_path, "BTCUSDT", "paper")
    assert paths.book.exists() and paths.events.exists()
    assert not st["halted"] and st["quote"] is not None and st["quote_fresh"]
    assert st["decision_loop"] and st["decision_bar"] < st["live_row_ts"]  # decided on a closed bar
    assert set(st["signals"]) == {"funding_carry", "cash_and_carry", "calendar", "random_maturity"}
    kinds = [e["kind"] for e in Ledger(paths.events).read()]
    assert "SNAPSHOT" in kinds and "HEARTBEAT" in kinds and "DECISION" in kinds
    assert "basis_arb live | BTCUSDT | paper" in capsys.readouterr().out
