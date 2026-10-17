"""Rates helpers, cq/nq contract selection and build_panel on fixture frames (fetchers monkeypatched)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from basis_arb.config import RateInputs
from basis_arb.data import binance as bn
from basis_arb.data import panel as pn
from basis_arb.data import rates
from basis_arb.types import PANEL_REQUIRED_COLUMNS, validate_panel
from tests.conftest import make_panel

START, END = "2025-03-20", "2025-04-05"
EXP_MAR = datetime(2025, 3, 28, 8, tzinfo=UTC)
EXP_JUN = datetime(2025, 6, 27, 8, tzinfo=UTC)
EXP_SEP = datetime(2025, 9, 26, 8, tzinfo=UTC)

DTB3_CSV = (
    "observation_date,DTB3\n2025-03-14,4.30\n2025-03-17,4.31\n2025-03-18,4.32\n2025-03-19,.\n2025-03-20,4.33\n"
    "2025-03-21,4.34\n2025-03-24,\n2025-03-25,4.40\n2025-03-26,4.41\n2025-03-27,4.42\n2025-03-28,4.43\n"
    "2025-03-31,4.44\n2025-04-01,4.45\n2025-04-02,4.46\n2025-04-03,4.47\n2025-04-04,4.48\n"
)


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def klines(first_open: str, last_open: str, price: float, slope: float = 1.0, drop: tuple[str, ...] = ()) -> pd.DataFrame:
    """Synthetic hourly klines (open_time keyed) like binance.fetch_klines returns them."""
    ot = pd.date_range(first_open, last_open, freq="1h", tz="UTC")
    if drop:
        ot = ot[~ot.isin(pd.DatetimeIndex([ts(d) for d in drop]))]
    close = price + slope * np.arange(len(ot), dtype=float)
    return pd.DataFrame({
        "open_time": ot, "open": close, "high": close + 1.0, "low": close - 1.0, "close": close,
        "volume": 1.0, "quote_volume": close, "trades": np.ones(len(ot), dtype=np.int64),
    })


def funding_rows(first: str = "2025-03-15", last: str = "2025-04-10") -> list[dict]:
    """Raw fundingRate records every 8h; every other stamp arrives 3 ms late."""
    stamps = pd.date_range(first, last, freq="8h", tz="UTC")
    return [
        {"symbol": "BTCUSDT", "fundingTime": int(t.value // 1_000_000) + (3 if k % 2 else 0),
         "fundingRate": f"{1e-4 * (1 + k % 3):.8f}", "markPrice": "1", "rateType": "Regular"}
        for k, t in enumerate(stamps)
    ]


@pytest.fixture
def sources(monkeypatch):
    """Monkeypatch every network fetcher with fixture data; returns the mutable fixture state."""
    state = {
        "frames": {
            ("spot", "BTCUSDT"): klines("2025-03-15", "2025-04-10", 80_000.0),
            ("um", "BTCUSDT"): klines("2025-03-15", "2025-04-10", 80_010.0),
            ("um", "BTCUSDT_250328"): klines("2024-12-27 08:00", "2025-03-28 07:00", 80_500.0),
            ("um", "BTCUSDT_250627"): klines("2025-03-01", "2025-04-10", 81_000.0, drop=("2025-03-25 10:00",)),
            ("um", "BTCUSDT_250926"): klines("2025-03-28 08:00", "2025-04-10", 82_000.0),
            ("um", "BTCUSDT_251226"): klines("2025-03-01", "2025-03-01", 1.0).iloc[0:0],  # listed, no bars
        },
        "funding": funding_rows(),
        "dtb3": rates.parse_dtb3_csv(DTB3_CSV),
        "listed": [("BTCUSDT_250627", EXP_JUN), ("BTCUSDT_250926", EXP_SEP)],
        "calls": [],
    }

    def fetch_klines(market, symbol, interval, start_ms, end_ms=None, *, client=None):
        state["calls"].append((market, symbol, start_ms, end_ms))
        df = state["frames"].get((market, symbol))
        if df is None:
            raise bn.BinanceInvalidSymbol("Invalid symbol.", status=400, code=-1121)
        t = df["open_time"]
        m = t >= pd.Timestamp(start_ms, unit="ms", tz="UTC")
        if end_ms is not None:
            m &= t <= pd.Timestamp(end_ms, unit="ms", tz="UTC")
        return df[m].reset_index(drop=True)

    def fetch_funding(symbol, start_ms, end_ms=None, *, client=None):
        rows = [r for r in state["funding"]
                if r["fundingTime"] >= start_ms and (end_ms is None or r["fundingTime"] <= end_ms)]
        return bn.parse_funding(rows)

    def fetch_dtb3(**kwargs):
        if isinstance(state["dtb3"], Exception):
            raise state["dtb3"]
        return state["dtb3"]

    monkeypatch.setattr(bn, "fetch_klines", fetch_klines)
    monkeypatch.setattr(bn, "fetch_funding", fetch_funding)
    monkeypatch.setattr(bn, "listed_quarterlies", lambda root, *, client=None: list(state["listed"]))
    monkeypatch.setattr(rates, "fetch_dtb3", fetch_dtb3)
    return state


def build(**kw) -> pd.DataFrame:
    kw.setdefault("use_cache", False)
    return pn.build_panel("BTCUSDT", START, END, client=object(), **kw)


# --- rates -------------------------------------------------------------------------------------
def test_parse_dtb3_handles_dot_blank_and_legacy_header():
    s = rates.parse_dtb3_csv("observation_date,DTB3\n2025-01-02,4.00\n2025-01-03,.\n2025-01-06,\n2025-01-07,4.25\n")
    assert s.index.tolist() == [ts("2025-01-02"), ts("2025-01-07")]
    assert s.tolist() == pytest.approx([0.04, 0.0425])
    assert rates.parse_dtb3_csv("DATE,DTB3\n2020-01-02,1.53\n").iloc[0] == pytest.approx(0.0153)
    with pytest.raises(ValueError):
        rates.parse_dtb3_csv("observation_date,DTB3\n2025-01-02,abc\n")
    with pytest.raises(ValueError, match="header"):
        rates.parse_dtb3_csv("date,value\n2025-01-02,4.0\n")


def test_r_usd_on_grid_has_no_look_ahead():
    dtb3 = rates.parse_dtb3_csv("observation_date,DTB3\n2025-01-02,4.00\n2025-01-03,.\n2025-01-06,4.50\n")
    grid = pd.date_range("2025-01-01 22:00", "2025-01-07 02:00", freq="1h", tz="UTC", name="ts")
    r = rates.r_usd_on_grid(dtb3, grid)
    assert r.index.equals(grid) and r.name == "r_usd"
    # the observation for date d is never visible on d itself (next-business-day publication)
    assert np.isnan(r.loc[ts("2025-01-02 00:00")]) and np.isnan(r.loc[ts("2025-01-02 23:00")])
    assert r.loc[ts("2025-01-03 00:00")] == pytest.approx(0.04)  # date d applies from (d + 1 day) 00:00 UTC
    assert r.loc[ts("2025-01-06 23:00")] == pytest.approx(0.04)  # "." on 01-03, weekend carried forward
    assert r.loc[ts("2025-01-07 00:00")] == pytest.approx(0.045)  # the 01-06 print first applies on 01-07


def test_coin_yield_from_config():
    assert rates.coin_yield("BTCUSDT") == RateInputs().coin_yield["BTC"]
    assert rates.coin_yield("ETHUSDT") == RateInputs().coin_yield["ETH"]
    assert rates.coin_yield("ETHUSDT", RateInputs(coin_yield={"ETH": 0.03})) == pytest.approx(0.03)
    with pytest.raises(ValueError, match="SOL"):
        rates.coin_yield("SOLUSDT")
    with pytest.raises(ValueError):
        rates.coin_yield("BTCEUR")


# --- contract selection ------------------------------------------------------------------------
def test_select_contracts_across_an_expiry_boundary():
    grid = pd.date_range("2025-03-28 05:00", "2025-03-28 10:00", freq="1h", tz="UTC", name="ts")
    contracts = pd.DataFrame({
        "symbol": ["C", "A", "B"],
        "expiry": [ts("2025-09-26 08:00"), ts("2025-03-28 08:00"), ts("2025-06-27 08:00")],
        "listed": [ts("2025-03-28 09:00"), ts("2024-12-27 09:00"), ts("2025-03-14 09:00")],
    })
    sel = pn.select_contracts(grid, contracts)
    assert sel.loc[ts("2025-03-28 07:00"), ["cq_symbol", "nq_symbol"]].tolist() == ["A", "B"]
    # at ts == expiry the old contract is already gone; C has not printed a bar yet
    assert sel.loc[ts("2025-03-28 08:00"), ["cq_symbol", "nq_symbol"]].tolist() == ["B", None]
    assert sel.loc[ts("2025-03-28 09:00"), ["cq_symbol", "nq_symbol"]].tolist() == ["B", "C"]
    assert sel.loc[ts("2025-03-28 07:00"), "cq_expiry"] == ts("2025-03-28 08:00")
    assert pd.isna(sel.loc[ts("2025-03-28 08:00"), "nq_expiry"])
    assert str(sel["cq_expiry"].dtype) == "datetime64[ns, UTC]"


def test_select_contracts_without_listing_dates_or_contracts():
    grid = pd.date_range("2025-03-28 07:00", periods=2, freq="1h", tz="UTC", name="ts")
    contracts = pd.DataFrame({"symbol": ["A", "B", "C"],
                              "expiry": [ts("2025-03-28 08:00"), ts("2025-06-27 08:00"), ts("2025-09-26 08:00")]})
    sel = pn.select_contracts(grid, contracts)
    assert sel["cq_symbol"].tolist() == ["A", "B"] and sel["nq_symbol"].tolist() == ["B", "C"]
    empty = pn.select_contracts(grid, pd.DataFrame(columns=["symbol", "expiry"]))
    assert empty["cq_symbol"].tolist() == [None, None] and empty["nq_expiry"].isna().all()
    assert str(empty["cq_expiry"].dtype) == "datetime64[ns, UTC]"


# --- build_panel -------------------------------------------------------------------------------
def test_build_panel_satisfies_the_contract(sources):
    df = build()
    validate_panel(df)
    assert tuple(df.columns) == PANEL_REQUIRED_COLUMNS
    assert df.index.name == "ts" and str(df.index.dtype) == "datetime64[ns, UTC]"
    assert df.index[0] == ts("2025-03-20 00:00") and df.index[-1] == ts("2025-04-05 00:00")
    assert (df.index.to_series().diff().dropna() == pd.Timedelta(hours=1)).all()
    assert df.dtypes.to_dict() == make_panel().dtypes.to_dict()  # same dtypes as the shared fixture
    # row ts holds the bar that ENDS at ts
    spot = sources["frames"][("spot", "BTCUSDT")].set_index("open_time")
    assert df.loc[ts("2025-03-20 00:00"), "spot_close"] == spot.loc[ts("2025-03-19 23:00"), "close"]
    assert df.loc[ts("2025-03-20 00:00"), "spot_high"] == spot.loc[ts("2025-03-19 23:00"), "high"]


def test_funding_only_at_stamps(sources):
    df = build()
    stamps = df.index[df["funding_rate"].notna()]
    assert stamps.equals(pd.date_range("2025-03-20", "2025-04-05", freq="8h", tz="UTC"))
    expected = bn.parse_funding(sources["funding"]).set_index("funding_time")["funding_rate"]
    np.testing.assert_allclose(df.loc[stamps, "funding_rate"], expected.loc[stamps])


def test_quarterlies_roll_at_expiry_and_dte_is_exact(sources):
    df = build()
    before, at, after = (df.loc[ts(f"2025-03-28 {h}")] for h in ("07:00", "08:00", "09:00"))
    assert (before["fut_cq_symbol"], before["fut_nq_symbol"]) == ("BTCUSDT_250328", "BTCUSDT_250627")
    assert before["fut_cq_dte_days"] == pytest.approx(1 / 24) and before["fut_cq_expiry"] == ts("2025-03-28 08:00")
    # at ts == expiry the March contract is gone; September has not printed its first bar yet
    assert (at["fut_cq_symbol"], at["fut_nq_symbol"]) == ("BTCUSDT_250627", None)
    assert np.isnan(at["fut_nq_close"]) and pd.isna(at["fut_nq_expiry"]) and np.isnan(at["fut_nq_dte_days"])
    assert after["fut_nq_symbol"] == "BTCUSDT_250926" and after["fut_nq_close"] == 82_000.0
    # closes come from the selected contract's bar ending at ts; NaN where it had no bar
    mar = sources["frames"][("um", "BTCUSDT_250328")].set_index("open_time")["close"]
    assert df.loc[ts("2025-03-25 00:00"), "fut_cq_close"] == mar.loc[ts("2025-03-24 23:00")]
    gap = df.loc[ts("2025-03-25 11:00")]
    assert gap["fut_nq_symbol"] == "BTCUSDT_250627" and np.isnan(gap["fut_nq_close"]) and np.isnan(gap["fut_nq_high"])
    for k in ("cq", "nq"):
        listed = df[f"fut_{k}_symbol"].notna()
        idx = df.index[listed]
        dte = (df.loc[listed, f"fut_{k}_expiry"] - pd.Series(idx, index=idx)) / pd.Timedelta(days=1)
        np.testing.assert_allclose(df.loc[listed, f"fut_{k}_dte_days"], dte)
        assert (df.loc[listed, f"fut_{k}_dte_days"] > 0).all()
        assert df.loc[~listed, f"fut_{k}_dte_days"].isna().all() and df.loc[~listed, f"fut_{k}_expiry"].isna().all()


def test_rates_and_coin_yield_columns(sources):
    df = build()
    assert df.loc[ts("2025-03-20 00:00"), "r_usd"] == pytest.approx(0.0432)  # 03-19 is "." -> 03-18 print
    assert df.loc[ts("2025-03-24 23:00"), "r_usd"] == pytest.approx(0.0434)  # Friday 03-21 print
    assert df.loc[ts("2025-03-25 00:00"), "r_usd"] == pytest.approx(0.0434)  # 03-24 blank; 03-25 not yet visible
    assert df.loc[ts("2025-03-26 00:00"), "r_usd"] == pytest.approx(0.0440)
    assert (df["y_coin"] == 0.0).all()


def test_symbols_without_data_are_skipped_and_logged(sources, caplog):
    caplog.set_level(logging.INFO, logger="basis_arb.data.panel")
    df = build()
    used = set(df["fut_cq_symbol"].dropna()) | set(df["fut_nq_symbol"].dropna())
    assert used == {"BTCUSDT_250328", "BTCUSDT_250627", "BTCUSDT_250926"}
    assert "BTCUSDT_251226 (no bars in window)" in caplog.text
    assert "BTCUSDT_260327 (invalid symbol)" in caplog.text
    assert "BTCUSDT_241227 (expired before start)" in caplog.text
    assert not any(c[1] == "BTCUSDT_241227" for c in sources["calls"])  # not even requested


def test_short_gap_is_forward_filled_with_nan_high_low(sources, caplog):
    caplog.set_level(logging.INFO, logger="basis_arb.data.panel")
    sources["frames"][("spot", "BTCUSDT")] = klines(
        "2025-03-15", "2025-04-10", 80_000.0, drop=("2025-03-22 10:00", "2025-03-22 11:00", "2025-03-22 12:00")
    )
    df = build()
    filled = [ts("2025-03-22 11:00"), ts("2025-03-22 12:00"), ts("2025-03-22 13:00")]
    assert (df.loc[filled, "spot_close"] == df.loc[ts("2025-03-22 10:00"), "spot_close"]).all()
    assert df.loc[filled, ["spot_high", "spot_low"]].isna().all().all()
    assert "spot_close: forward-filled 1 gap(s), 3 bar(s)" in caplog.text
    # every filled row is logged at INFO with its exact timestamp
    assert any(
        r.levelno == logging.INFO
        and r.getMessage() == "spot_close: forward-filled rows (3, UTC): 2025-03-22 11:00, 2025-03-22 12:00, 2025-03-22 13:00"
        for r in caplog.records
    )


def test_gaps_up_to_5h_fill_by_default_and_longer_ones_raise(sources):
    five = tuple(f"2025-03-22 {h:02d}:00" for h in range(10, 15))  # bars ending 11:00 .. 15:00 missing
    sources["frames"][("spot", "BTCUSDT")] = klines("2025-03-15", "2025-04-10", 80_000.0, drop=five)
    df = build()
    validate_panel(df)
    filled = pd.date_range("2025-03-22 11:00", "2025-03-22 15:00", freq="1h", tz="UTC")
    assert (df.loc[filled, "spot_close"] == df.loc[ts("2025-03-22 10:00"), "spot_close"]).all()
    assert df.loc[filled, ["spot_high", "spot_low"]].isna().all().all()
    six = tuple(f"2025-03-22 {h:02d}:00" for h in range(10, 16))
    sources["frames"][("spot", "BTCUSDT")] = klines("2025-03-15", "2025-04-10", 80_000.0, drop=six)
    with pytest.raises(ValueError, match=r"longer than the 5h .*2025-03-22 11:00 UTC \+6h"):
        build()
    df = build(max_ffill_hours=6)
    validate_panel(df)
    assert df.loc[ts("2025-03-22 16:00"), "spot_close"] == df.loc[ts("2025-03-22 10:00"), "spot_close"]


def test_leading_rows_without_spot_or_perp_are_dropped(sources):
    sources["frames"][("um", "BTCUSDT")] = klines("2025-03-20 04:00", "2025-04-10", 80_010.0)
    assert build().index[0] == ts("2025-03-20 05:00")  # first perp bar (open 04:00) ends 05:00
    sources["frames"][("um", "BTCUSDT")] = klines(
        "2025-03-15", "2025-04-10", 80_010.0, drop=("2025-03-19 23:00", "2025-03-20 00:00", "2025-03-20 01:00")
    )
    df = build()
    assert df.index[0] == ts("2025-03-20 03:00")
    validate_panel(df)


def test_r_usd_leading_gap_takes_the_first_observation(sources, caplog):
    sources["dtb3"] = rates.parse_dtb3_csv("observation_date,DTB3\n2025-03-25,4.40\n2025-03-26,4.41\n")
    df = build()
    assert df.loc[ts("2025-03-20 00:00"), "r_usd"] == pytest.approx(0.044)  # filled with the first observation
    assert df.loc[ts("2025-03-25 23:00"), "r_usd"] == pytest.approx(0.044)
    assert df.loc[ts("2025-03-26 00:00"), "r_usd"] == pytest.approx(0.044)  # 03-25 print applies from 03-26
    assert df.loc[ts("2025-03-27 00:00"), "r_usd"] == pytest.approx(0.0441)
    assert np.isfinite(df["r_usd"].to_numpy()).all()
    assert "leading row(s) before the first DTB3 observation" in caplog.text


def test_rate_fallback_is_constant_and_logged_loudly(sources, caplog):
    sources["dtb3"] = RuntimeError("FRED unreachable")
    caplog.set_level(logging.ERROR)
    df = build()
    assert (df["r_usd"] == 0.04).all()
    assert any(r.levelno == logging.ERROR and "fallback" in r.getMessage() for r in caplog.records)


def test_calendar_cross_check_and_holes_fail_loudly(sources):
    sources["listed"] = [("BTCUSDT_250627", datetime(2025, 6, 27, 9, tzinfo=UTC))]
    with pytest.raises(ValueError, match="deliveryDate"):
        build()
    sources["listed"] = [("BTCUSDT_250620", datetime(2025, 6, 20, 8, tzinfo=UTC))]
    with pytest.raises(ValueError, match="not in the generated calendar"):
        build()
    sources["listed"] = []
    del sources["frames"][("um", "BTCUSDT_250627")]
    with pytest.raises(ValueError, match="BTCUSDT_250627"):
        build()


def test_save_load_round_trip(sources, tmp_path):
    df = build()
    path = pn.save_panel(df, "btcusdt", data_dir=tmp_path)
    assert path == tmp_path / "panel_BTCUSDT_1h.parquet"
    pd.testing.assert_frame_equal(pn.load_panel("BTCUSDT", data_dir=tmp_path), df, check_freq=False)
    with pytest.raises(ValueError):
        pn.save_panel(df.drop(columns=["r_usd"]), "BTCUSDT", data_dir=tmp_path)


def test_build_through_the_cache_is_deterministic(sources, tmp_path):
    a = build(use_cache=True, data_dir=tmp_path)
    raw = {p.name for p in (tmp_path / "raw").iterdir()}
    assert {"spot_BTCUSDT_1h.parquet", "um_BTCUSDT_1h.parquet", "um_BTCUSDT_250328_1h.parquet",
            "funding_BTCUSDT.parquet"} <= raw
    n = len(sources["calls"])
    b = build(use_cache=True, data_dir=tmp_path)
    pd.testing.assert_frame_equal(a, b)
    # complete series (spot/perp up to `end`, the expired March contract) are not requested again
    again = {c[1] for c in sources["calls"][n:]}
    assert "BTCUSDT_250328" not in again and "BTCUSDT" not in again


def test_coverage_stats(sources):
    st = pn.coverage_stats(build())
    assert st["rows"] == 16 * 24 + 1 and st["funding_prints"] == 16 * 3 + 1
    assert st["first_funding"] == ts("2025-03-20 00:00") and st["last_funding"] == ts("2025-04-05 00:00")
    assert st["quarterly_contracts"] == 3 and st["quarterly_symbols"] == "BTCUSDT_250328 .. BTCUSDT_250926"
    assert st["fut_cq_close_nan_share"] == 0.0 and st["spot_filled_bars"] == 0
    assert st["r_usd_min"] == pytest.approx(0.0432) and st["r_usd_max"] == pytest.approx(0.0448)


def test_cli_builds_validates_saves_and_prints(sources, tmp_path, capsys):
    rc = pn.main(["--root", "BTCUSDT", "--start", START, "--end", END, "--data-dir", str(tmp_path), "--no-cache"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rows" in out and "quarterly_contracts" in out and "cq_basis_ann" in out
    validate_panel(pn.load_panel("BTCUSDT", data_dir=tmp_path))


def test_cli_exits_nonzero_on_a_long_gap(sources, tmp_path, caplog):
    sources["frames"][("spot", "BTCUSDT")] = klines(
        "2025-03-15", "2025-04-10", 80_000.0, drop=tuple(f"2025-03-22 {h:02d}:00" for h in range(10, 16))
    )
    rc = pn.main(["--root", "BTCUSDT", "--start", START, "--end", END, "--data-dir", str(tmp_path), "--no-cache"])
    assert rc == 2 and "longer than the 5h" in caplog.text
    assert not (tmp_path / "panel_BTCUSDT_1h.parquet").exists()
