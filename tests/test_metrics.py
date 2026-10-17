"""Metric helpers on hand-made series and table shapes on small engine runs."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from basis_arb.backtest import metrics as mt
from basis_arb.backtest.engine import run_backtest
from basis_arb.config import BacktestConfig
from tests.conftest import make_panel


def _hourly(values, start="2025-01-01 01:00") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="1h", tz="UTC", name="ts")
    return pd.Series(np.asarray(values, float), index=idx)


def test_sharpe_on_hand_made_series():
    assert mt.sharpe(pd.Series([1.0, 2.0, 3.0])) == pytest.approx(2.0 * math.sqrt(365))
    assert mt.sharpe(pd.Series([1.0, -1.0, 1.0, -1.0])) == pytest.approx(0.0)
    assert mt.sharpe(pd.Series([2.0, 4.0]), periods=252) == pytest.approx(3.0 / math.sqrt(2.0) * math.sqrt(252))
    assert math.isnan(mt.sharpe(pd.Series([5.0, 5.0, 5.0])))  # flat: undefined
    assert math.isnan(mt.sharpe(pd.Series([1.0])))
    assert mt.sharpe(pd.Series([1.0, np.nan, 3.0])) == pytest.approx(mt.sharpe(pd.Series([1.0, 3.0])))


def test_max_drawdown_on_hand_made_series():
    eq = pd.Series([100.0, 110.0, 99.0, 105.0, 120.0, 90.0, 95.0])
    assert mt.max_drawdown(eq) == pytest.approx(0.25)  # 120 -> 90
    assert mt.max_drawdown(pd.Series([100.0, 101.0, 102.0])) == 0.0
    assert mt.max_drawdown(pd.Series([100.0, 95.0]), start=200.0) == pytest.approx(0.525)  # start counts as a peak
    assert math.isnan(mt.max_drawdown(pd.Series([], dtype=float)))
    dd = mt.drawdown_series(eq)
    assert dd.min() == pytest.approx(-0.25) and dd.iloc[4] == 0.0 and (dd <= 0).all()


def test_daily_pnl_attributes_the_midnight_bar_to_the_previous_day():
    # bars close at 01:00 .. 00:00 next day (24 bars of day 1) and 01:00 on day 2
    eq = _hourly(np.arange(25) + 100.0)
    daily = mt.daily_pnl(eq, initial=100.0)
    assert list(daily.index.strftime("%Y-%m-%d")) == ["2025-01-01", "2025-01-02"]
    assert daily.iloc[0] == pytest.approx(23.0)  # 100 -> 123 over the 24 bars ending 2025-01-02 00:00
    assert daily.iloc[1] == pytest.approx(1.0)
    assert daily.sum() == pytest.approx(eq.iloc[-1] - 100.0)


def test_daily_pnl_is_calendar_complete_and_worst_window():
    idx = pd.DatetimeIndex(["2025-01-01 12:00", "2025-01-05 12:00"], tz="UTC")
    daily = mt.daily_pnl(pd.Series([10.0, 7.0], index=idx), initial=10.0)
    assert len(daily) == 5 and daily.iloc[1:4].eq(0.0).all() and daily.iloc[-1] == pytest.approx(-3.0)
    d = pd.Series([1.0, -2.0, -3.0, 4.0, -1.0])
    assert mt.worst_window(d, 2) == pytest.approx(-5.0)
    assert math.isnan(mt.worst_window(d, 30))


def _two_year_run():
    panel = make_panel(n_hours=24 * 400, start="2024-06-01", funding_per_period=0.0003)
    return run_backtest(panel, "funding_carry", BacktestConfig())


def test_yearly_table_shape_and_totals():
    res = _two_year_run()
    yt = mt.yearly_table(res)
    assert list(yt.index) == [2024, 2025] and yt.index.name == "year"
    assert yt.shape == (2, 12)
    for col in ("n_days", "pnl", "ret_on_capital", "sharpe", "max_dd", "pnl_basis", "pnl_funding", "pnl_costs",
                "n_entries", "n_round_trips", "time_in_position", "n_liquidations"):
        assert col in yt.columns
    assert yt["pnl"].sum() == pytest.approx(res.equity.iloc[-1] - res.capital)
    assert (yt["pnl_basis"] + yt["pnl_funding"] + yt["pnl_costs"]).sum() == pytest.approx(yt["pnl"].sum())
    assert yt["n_entries"].sum() == len(res.trades)


def test_episode_table_covers_only_episodes_inside_the_sample():
    panel = make_panel(n_hours=24 * 90, start="2024-07-15", funding_per_period=0.0003)
    res = run_backtest(panel, "funding_carry", BacktestConfig())
    ep = mt.episode_table(res)
    assert len(ep) == len(mt.EPISODES) == 5
    assert ep.loc["Aug-2024", "coverage"] == pytest.approx(1.0) and ep.loc["Aug-2024", "n_days"] == 61
    assert ep.loc["Aug-2024", "pnl"] > 0  # short perp collecting 0.03% per stamp
    for name in ("Mar-2020", "May-2021", "Nov-2022 FTX", "Q1/Q2-2026 funding compression"):
        assert ep.loc[name, "n_days"] == 0 and math.isnan(ep.loc[name, "pnl"])


def test_summarize_is_consistent_with_the_result():
    res = _two_year_run()
    s = mt.summarize(res)
    assert s["capital"] == res.capital and s["n_bars"] == len(res.equity)
    parts = s["pnl_basis"] + s["pnl_funding"] + s["pnl_fees"] + s["pnl_delivery"] + s["pnl_liquidation"]
    assert parts == pytest.approx(s["pnl_total"])
    assert s["ann_return_on_capital"] == pytest.approx(s["pnl_total"] / res.capital / (len(res.equity) / 8760.0))
    assert s["n_round_trips"] == 0 and s["n_open_trades"] == 1 and math.isnan(s["hit_rate"])
    assert 0.0 < s["time_in_position_frac"] < 1.0 and s["n_liquidations"] == 0
    assert s["turnover"] > 0 and s["max_dd"] >= 0
