"""Binance fetchers, client retry policy, quarterly calendar and parquet cache (offline unless marked)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import requests

from basis_arb.data import binance as bn
from basis_arb.data import cache, rates

H = 3_600_000
T2020 = 1_577_836_800_000  # 2020-01-01 00:00 UTC

SPOT_KLINES_JSON = """[
 [1577836800000,"7195.24000000","7196.25000000","7175.46000000","7177.02000000","511.81490100",
  1577840399999,"3675856.57948543",7640,"226.15312600","1624289.18965037","0"],
 [1577840400000,"7176.47000000","7230.00000000","7175.71000000","7216.27000000","883.05260300",
  1577843999999,"6365952.54111276",9033,"570.72141900","4114804.04969093","0"]
]"""

FUNDING_JSON = """[
 {"symbol":"BTCUSDT","fundingTime":1568102400000,"fundingRate":"0.00010000","markPrice":"","rateType":"Regular"},
 {"symbol":"BTCUSDT","fundingTime":1792252800003,"fundingRate":"-0.00002511","markPrice":"105000.1",
  "rateType":"Regular"}
]"""


def _ts(ms: int) -> pd.Timestamp:
    return pd.Timestamp(ms, unit="ms", tz="UTC")


def _bar(open_ms: int, close: float) -> list:
    return [open_ms, f"{close:.2f}", f"{close + 1:.2f}", f"{close - 1:.2f}", f"{close:.2f}", "1.5",
            open_ms + H - 1, "150.0", 7, "0", "0", "0"]


class FakeKlineServer:
    """Answers like Binance: open_time in [startTime, endTime], at most `limit` rows per call.

    overlap > 0 repeats that many bars before startTime at the head of every non-empty page.
    """

    def __init__(self, first_open_ms: int, n_bars: int, overlap: int = 0):
        self.bars = [_bar(first_open_ms + i * H, 100.0 + i) for i in range(n_bars)]
        self.overlap = overlap
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, base, path, params=None):
        self.calls.append((base, path, dict(params)))
        hi = params.get("endTime", float("inf"))
        fresh = [b for b in self.bars if params["startTime"] <= b[0] <= hi]
        if not fresh:
            return []
        lo = params["startTime"] - self.overlap * H
        return [b for b in self.bars if lo <= b[0] <= hi][: params["limit"]]


class FakeFundingServer:
    def __init__(self, n: int, first_ms: int = T2020, step_ms: int = 8 * H):
        self.rows = [
            {"symbol": "BTCUSDT", "fundingTime": first_ms + i * step_ms + (3 if i % 2 else 0),
             "fundingRate": f"{1e-4 + i * 1e-8:.8f}", "markPrice": "" if i < 10 else "7000.5", "rateType": "Regular"}
            for i in range(n)
        ]
        self.calls: list[dict] = []

    def get(self, base, path, params=None):
        self.calls.append(dict(params))
        hi = params.get("endTime", float("inf"))
        return [r for r in self.rows if params["startTime"] <= r["fundingTime"] <= hi][: params["limit"]]


# --- parsing -----------------------------------------------------------------------------------
def test_parse_klines_from_inline_json():
    df = bn.parse_klines(json.loads(SPOT_KLINES_JSON))
    assert tuple(df.columns) == bn.KLINE_COLUMNS
    assert str(df["open_time"].dtype) == "datetime64[ns, UTC]"
    assert df["open_time"].tolist() == [_ts(T2020), _ts(T2020 + H)]
    assert df["close"].tolist() == pytest.approx([7177.02, 7216.27])
    assert df["high"].iloc[1] == pytest.approx(7230.0) and df["low"].iloc[0] == pytest.approx(7175.46)
    assert df["quote_volume"].iloc[0] == pytest.approx(3675856.57948543)
    assert df["trades"].tolist() == [7640, 9033] and df["trades"].dtype == np.int64
    assert (df[["open", "high", "low", "close", "volume", "quote_volume"]].dtypes == np.float64).all()


def test_parse_klines_empty_keeps_schema():
    df = bn.parse_klines([])
    assert df.empty and tuple(df.columns) == bn.KLINE_COLUMNS
    assert str(df["open_time"].dtype) == "datetime64[ns, UTC]"


def test_parse_funding_floors_late_stamps_and_blank_mark_price():
    df = bn.parse_funding(json.loads(FUNDING_JSON))
    assert tuple(df.columns) == bn.FUNDING_COLUMNS
    # 1792252800003 is 2026-10-17 16:00:00.003 UTC -> floored onto the hour
    assert df["funding_time"].tolist() == [pd.Timestamp("2019-09-10 08:00", tz="UTC"),
                                           pd.Timestamp("2026-10-17 16:00", tz="UTC")]
    assert df["funding_rate"].tolist() == pytest.approx([0.0001, -0.00002511])
    assert np.isnan(df["mark_price"].iloc[0]) and df["mark_price"].iloc[1] == pytest.approx(105000.1)


# --- pagination --------------------------------------------------------------------------------
def test_fetch_klines_paginates_spot_in_pages_of_1000():
    server = FakeKlineServer(T2020, 2500)
    df = bn.fetch_klines("spot", "BTCUSDT", "1h", T2020, client=server)
    assert len(df) == 2500 and df["open_time"].is_unique and df["open_time"].is_monotonic_increasing
    assert df["open_time"].iloc[-1] == _ts(T2020 + 2499 * H)
    # cursor = last open + 1 bar; the final request returns an empty page
    assert [c[2]["startTime"] for c in server.calls] == [T2020, T2020 + 1000 * H, T2020 + 2000 * H, T2020 + 2500 * H]
    assert all(c[0] == bn.SPOT_BASE and c[1] == "/api/v3/klines" and c[2]["limit"] == 1000 for c in server.calls)


def test_fetch_klines_um_uses_1500_rows_and_stops_at_end():
    server = FakeKlineServer(T2020, 4000)
    end = T2020 + 1600 * H
    df = bn.fetch_klines("um", "BTCUSDT_210326", "1h", T2020, end, client=server)
    assert len(df) == 1601 and df["open_time"].iloc[-1] == _ts(end)
    assert [c[2]["startTime"] for c in server.calls] == [T2020, T2020 + 1500 * H]  # stops once last open >= end
    assert all(
        c[0] == bn.UM_BASE and c[1] == "/fapi/v1/klines" and c[2]["limit"] == 1500 and c[2]["endTime"] == end
        and c[2]["symbol"] == "BTCUSDT_210326"
        for c in server.calls
    )


def test_fetch_klines_dedupes_overlapping_pages():
    server = FakeKlineServer(T2020, 2500, overlap=2)
    df = bn.fetch_klines("spot", "BTCUSDT", "1h", T2020, client=server)
    assert len(df) == 2500 and df["open_time"].is_unique and df["open_time"].is_monotonic_increasing


def test_fetch_klines_drops_the_bar_still_forming(monkeypatch):
    server = FakeKlineServer(T2020, 10)
    monkeypatch.setattr(bn, "_now_ms", lambda: T2020 + 9 * H + 30 * 60_000)  # inside the 10th bar
    df = bn.fetch_klines("spot", "BTCUSDT", "1h", T2020, client=server)
    assert len(df) == 9 and df["open_time"].iloc[-1] == _ts(T2020 + 8 * H)
    assert len(server.calls) == 1  # the cursor passed "now": no extra request


def test_fetch_klines_raises_when_the_server_ignores_start_time():
    class Stuck:
        def get(self, base, path, params=None):
            return [_bar(T2020, 100.0)]

    with pytest.raises(bn.BinanceAPIError, match="no progress"):
        bn.fetch_klines("spot", "BTCUSDT", "1h", T2020 + 5 * H, client=Stuck())


def test_fetch_klines_rejects_unknown_market():
    with pytest.raises(ValueError, match="market"):
        bn.fetch_klines("coinm", "BTCUSD_PERP", "1h", T2020, client=FakeKlineServer(T2020, 1))


def test_fetch_premium_index_klines_same_shape():
    server = FakeKlineServer(T2020, 3)
    df = bn.fetch_premium_index_klines("BTCUSDT", "1h", T2020, client=server)
    assert tuple(df.columns) == bn.KLINE_COLUMNS and len(df) == 3
    assert server.calls[0][1] == "/fapi/v1/premiumIndexKlines" and server.calls[0][2]["limit"] == 1500


def test_fetch_funding_paginates_and_floors():
    server = FakeFundingServer(2500)
    df = bn.fetch_funding("BTCUSDT", T2020, client=server)
    assert len(df) == 2500 and df["funding_time"].is_unique and df["funding_time"].is_monotonic_increasing
    assert (df["funding_time"] == df["funding_time"].dt.floor("h")).all()
    assert len(server.calls) == 4  # 1000 + 1000 + 500 + empty page
    assert all(c["limit"] == 1000 for c in server.calls)
    assert server.calls[1]["startTime"] == server.rows[999]["fundingTime"] + 1
    assert df["mark_price"].iloc[:10].isna().all() and df["mark_price"].iloc[10] == pytest.approx(7000.5)


def test_fetch_funding_end_bound_is_inclusive_on_raw_stamps():
    server = FakeFundingServer(2500)
    end = server.rows[1201]["fundingTime"]  # an odd row: stamp is 3 ms late
    df = bn.fetch_funding("BTCUSDT", T2020, end, client=server)
    assert len(df) == 1202 and len(server.calls) == 2
    assert df["funding_time"].iloc[-1] == _ts(T2020 + 1201 * 8 * H)


# --- client retry policy -----------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, payload=None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class ScriptedSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


def test_client_retries_connection_errors_and_5xx_and_honors_retry_after():
    sleeps: list[float] = []
    session = ScriptedSession([
        requests.ConnectionError("connection reset"),
        FakeResponse(503),
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, [1, 2]),
    ])
    client = bn.BinanceClient(session=session, sleep=sleeps.append, backoff_s=1.0)
    assert client.get(bn.SPOT_BASE, "/api/v3/klines", {"symbol": "BTCUSDT"}) == [1, 2]
    assert sleeps == [1.0, 2.0, 7.0]
    assert len(session.calls) == 4 and session.calls[0] == ("https://api.binance.com/api/v3/klines", {"symbol": "BTCUSDT"})


def test_client_raises_invalid_symbol_and_other_4xx_without_retry():
    session = ScriptedSession([FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."})])
    with pytest.raises(bn.BinanceInvalidSymbol) as info:
        bn.BinanceClient(session=session, sleep=lambda s: None).get(bn.UM_BASE, "/fapi/v1/klines", {})
    assert info.value.code == -1121 and info.value.status == 400 and len(session.calls) == 1

    session = ScriptedSession([FakeResponse(400, {"code": -1100, "msg": "Illegal characters"})])
    with pytest.raises(bn.BinanceAPIError) as info:
        bn.BinanceClient(session=session, sleep=lambda s: None).get(bn.UM_BASE, "/fapi/v1/klines", {})
    assert not isinstance(info.value, bn.BinanceInvalidSymbol) and info.value.code == -1100


def test_client_gives_up_after_max_retries_and_on_long_bans():
    sleeps: list[float] = []
    session = ScriptedSession([FakeResponse(502)] * 4)
    client = bn.BinanceClient(session=session, max_retries=3, sleep=sleeps.append)
    with pytest.raises(bn.BinanceAPIError, match="giving up after 4 attempts"):
        client.get(bn.UM_BASE, "/fapi/v1/klines", {})
    assert sleeps == [1.0, 2.0, 4.0]

    session = ScriptedSession([FakeResponse(418, headers={"Retry-After": "7200"})])
    with pytest.raises(bn.BinanceRateLimitError):
        bn.BinanceClient(session=session, sleep=sleeps.append).get(bn.UM_BASE, "/fapi/v1/klines", {})
    assert sleeps == [1.0, 2.0, 4.0]  # a 2h ban is not slept through


# --- quarterly calendar ------------------------------------------------------------------------
EXPECTED_QUARTERLIES = [
    "210326", "210625", "210924", "211231", "220325", "220624", "220930", "221230", "230331",
    "230630", "230929", "231229", "240329", "240628", "240927", "241227", "250328", "250627",
    "250926", "251226", "260327", "260626", "260925", "261225", "270326",
]


def test_quarterly_expiries_reproduce_the_binance_calendar():
    expiries = bn.quarterly_expiries(date(2021, 3, 1), date(2027, 3, 31))
    assert [f"{e:%y%m%d}" for e in expiries] == EXPECTED_QUARTERLIES
    assert all(e.weekday() == 4 and (e.hour, e.minute) == (8, 0) and e.utcoffset() == timedelta(0) for e in expiries)
    assert [bn.quarterly_symbol("BTCUSDT", e) for e in expiries][:2] == ["BTCUSDT_210326", "BTCUSDT_210625"]
    # bounds are inclusive on the expiry date
    assert bn.quarterly_expiries(date(2021, 3, 26), date(2021, 3, 26)) == [datetime(2021, 3, 26, 8, tzinfo=UTC)]


def test_quarterly_expiries_default_end_is_two_years_ahead():
    expiries = bn.quarterly_expiries()
    assert expiries[0] == datetime(2021, 3, 26, 8, tzinfo=UTC)
    horizon = bn.add_years(datetime.now(UTC).date(), 2)
    assert horizon - timedelta(days=92) < expiries[-1].date() <= horizon


def test_quarterly_symbol_formatting():
    assert bn.quarterly_symbol("ETHUSDT", datetime(2026, 12, 25, 8, tzinfo=UTC)) == "ETHUSDT_261225"
    assert bn.quarterly_symbol("BTCUSDT", date(2021, 3, 26)) == "BTCUSDT_210326"
    assert bn.quarterly_symbol("BTCUSDT", pd.Timestamp("2030-06-28 08:00", tz="UTC")) == "BTCUSDT_300628"


def test_listed_quarterlies_from_exchange_info():
    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    dec, mar = datetime(2026, 12, 25, 8, tzinfo=UTC), datetime(2027, 3, 26, 8, tzinfo=UTC)
    info = {"symbols": [
        {"symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL", "deliveryDate": 4133404800000},
        {"symbol": "BTCUSDT_270326", "pair": "BTCUSDT", "contractType": "NEXT_QUARTER", "deliveryDate": ms(mar)},
        {"symbol": "BTCUSDT_261225", "pair": "BTCUSDT", "contractType": "CURRENT_QUARTER", "deliveryDate": ms(dec)},
        {"symbol": "ETHUSDT_261225", "pair": "ETHUSDT", "contractType": "CURRENT_QUARTER", "deliveryDate": ms(dec)},
    ]}

    class Info:
        def get(self, base, path, params=None):
            assert (base, path) == (bn.UM_BASE, "/fapi/v1/exchangeInfo")
            return info

    assert bn.listed_quarterlies("BTCUSDT", client=Info()) == [("BTCUSDT_261225", dec), ("BTCUSDT_270326", mar)]


# --- parquet cache -----------------------------------------------------------------------------
class FakeSeries:
    """Server-side hourly series; fetch(start_ms, end_ms) returns the inclusive window."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.calls: list[tuple[int, int | None]] = []

    def __call__(self, start_ms, end_ms):
        self.calls.append((start_ms, end_ms))
        t = self.df["open_time"]
        m = t >= _ts(start_ms)
        if end_ms is not None:
            m &= t <= _ts(end_ms)
        return self.df[m].reset_index(drop=True)


def _hourly(n: int) -> pd.DataFrame:
    return pd.DataFrame({"open_time": pd.date_range(_ts(T2020), periods=n, freq="1h"), "close": np.arange(n, dtype=float)})


def test_load_or_fetch_incremental_tail_and_head_backfill(tmp_path):
    path = tmp_path / "raw" / "spot_TEST_1h.parquet"
    full = _hourly(100)
    server = FakeSeries(full.iloc[:60].reset_index(drop=True))
    kw = {"time_col": "open_time", "overlap_ms": 2 * H}

    a = cache.load_or_fetch(path, server, T2020 + 10 * H, None, **kw)
    assert len(a) == 50 and path.exists() and server.calls == [(T2020 + 10 * H, None)]

    # 40 new bars appear and the last cached bar is revised: re-fetch from last - 2 bars, newest wins
    revised = full.copy()
    revised.loc[59, "close"] = -1.0
    server.df = revised
    b = cache.load_or_fetch(path, server, T2020 + 10 * H, None, **kw)
    assert server.calls[-1] == (T2020 + 57 * H, None)
    assert len(b) == 90 and b["open_time"].is_unique and b["open_time"].is_monotonic_increasing
    assert b.loc[b["open_time"] == _ts(T2020 + 59 * H), "close"].item() == -1.0

    # an earlier start backfills the head once ...
    c = cache.load_or_fetch(path, server, T2020, None, **kw)
    assert server.calls[-2] == (T2020, T2020 + 10 * H - 1)
    assert len(c) == 100 and c["open_time"].iloc[0] == _ts(T2020)

    # ... and a cache that already reaches `end` is served without any request
    n_calls = len(server.calls)
    d = cache.load_or_fetch(path, server, T2020, T2020 + 99 * H, **kw)
    assert len(server.calls) == n_calls
    pd.testing.assert_frame_equal(c, d)
    assert cache.read_cache(path)[1] == T2020  # covered start is remembered in the file metadata


def test_cached_klines_and_funding_write_the_documented_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(bn, "fetch_klines", lambda m, s, i, a, b=None, *, client=None: bn.parse_klines(
        [_bar(T2020 + k * H, 100.0 + k) for k in range(5)]))
    monkeypatch.setattr(bn, "fetch_funding", lambda s, a, b=None, *, client=None: bn.parse_funding(
        json.loads(FUNDING_JSON)))
    k = cache.cached_klines("um", "BTCUSDT_210326", "1h", T2020, None, data_dir=tmp_path)
    f = cache.cached_funding("BTCUSDT", 0, None, data_dir=tmp_path)
    assert (tmp_path / "raw" / "um_BTCUSDT_210326_1h.parquet").exists()
    assert (tmp_path / "raw" / "funding_BTCUSDT.parquet").exists()
    assert len(k) == 5 and str(k["open_time"].dtype) == "datetime64[ns, UTC]"
    again = cache.cached_klines("um", "BTCUSDT_210326", "1h", T2020, None, data_dir=tmp_path)
    pd.testing.assert_frame_equal(k, again)
    assert len(f) == 2 and str(f["funding_time"].dtype) == "datetime64[ns, UTC]"


# --- live endpoints (network) ------------------------------------------------------------------
@pytest.mark.network
def test_live_binance_endpoints_match_the_contract():
    client = bn.BinanceClient()
    df = bn.fetch_klines("spot", "BTCUSDT", "1h", T2020, T2020 + 2 * H, client=client)
    assert df["open_time"].tolist() == [_ts(T2020 + i * H) for i in range(3)]
    fr = bn.fetch_funding("BTCUSDT", 1568102400000, 1568102400000 + 16 * H, client=client)
    assert fr["funding_time"].iloc[0] == pd.Timestamp("2019-09-10 08:00", tz="UTC") and len(fr) == 3
    listed = bn.listed_quarterlies("BTCUSDT", client=client)
    calendar = {bn.quarterly_symbol("BTCUSDT", e): e for e in bn.quarterly_expiries()}
    assert listed and all(calendar[s] == e for s, e in listed)
    with pytest.raises(bn.BinanceInvalidSymbol):
        bn.fetch_klines("um", "BTCUSDT_200925", "1h", T2020, T2020 + H, client=client)


@pytest.mark.network
def test_live_fred_dtb3():
    s = rates.fetch_dtb3()
    assert s.index[0] == pd.Timestamp("1954-01-04", tz="UTC") and 0.0 <= s.iloc[-1] < 0.25
