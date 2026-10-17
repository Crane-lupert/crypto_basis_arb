import numpy as np
import pandas as pd
import pytest

from basis_arb.config import KAPPA, CostModel
from basis_arb.model import fair_value as fv


def test_cost_of_carry_zero_rates_is_spot():
    assert fv.cost_of_carry_price(100.0, 0.0, 0.0, 0.25) == pytest.approx(100.0)
    assert fv.fair_basis_ann(0.0, 0.0) == 0.0


def test_cost_of_carry_round_trips_through_annualized_basis():
    spot, r, tau = 50_000.0, 0.05, 90 / 365
    f = fv.cost_of_carry_price(spot, r, 0.0, tau)
    assert f / spot == pytest.approx(np.exp(0.05 * 90 / 365))
    assert fv.annualized_basis(f, spot, tau) == pytest.approx(0.05)
    assert fv.excess_basis_ann(f, spot, tau, r, 0.0) == pytest.approx(0.0, abs=1e-12)


def test_coin_yield_lowers_fair_basis():
    # ETH-style: 3% staking yield -> fair basis = r - y
    assert fv.fair_basis_ann(0.05, 0.03) == pytest.approx(0.02)


def test_annualized_basis_nan_at_or_after_expiry():
    assert np.isnan(fv.annualized_basis(101.0, 100.0, 0.0))
    out = fv.annualized_basis(np.array([101.0, 101.0]), np.array([100.0, 100.0]), np.array([0.5, -1.0]))
    assert np.isfinite(out[0]) and np.isnan(out[1])


def test_hmrw_prop1_zero_spread_perp_equals_spot():
    assert fv.perp_fair_price(100.0, 0.0, 0.0) == pytest.approx(100.0)
    assert fv.fair_funding_per_period(0.0, 0.0) == 0.0
    assert fv.fair_perp_premium_ann(0.0) == pytest.approx(0.0)


def test_hmrw_prop1_matches_formula_and_first_order_approximation():
    r, rp, spot = 0.04, 0.0, 50_000.0
    expected = KAPPA / (KAPPA - (r - rp)) * spot
    assert fv.perp_fair_price(spot, r, rp) == pytest.approx(expected)
    # per-period equilibrium funding ~ (r - r')/kappa = 4%/1095 ~ 0.00365% per 8h
    assert fv.fair_funding_per_period(r, rp) == pytest.approx((r - rp) / KAPPA, rel=1e-3)
    assert fv.fair_funding_ann(r, rp) == pytest.approx(r - rp, rel=1e-3)
    assert fv.fair_perp_premium_ann(r, rp) == pytest.approx(r - rp, rel=1e-3)


def test_hmrw_requires_kappa_above_spread():
    with pytest.raises(ValueError):
        fv.perp_fair_price(100.0, KAPPA + 1.0, 0.0)


def test_random_maturity_representation_reproduces_prop1():
    s0, g = 50_000.0, 0.04
    flat = fv.perp_price_random_maturity(lambda s: np.full_like(s, s0))
    assert flat == pytest.approx(s0, rel=1e-6)
    drift = fv.perp_price_random_maturity(lambda s: s0 * np.exp(g * s))
    assert drift == pytest.approx(fv.perp_fair_price(s0, g, 0.0), rel=1e-6)


def test_no_arb_band_is_asymmetric_log_band():
    lo, hi = fv.no_arb_band_ann(0.0038)
    assert hi == pytest.approx(KAPPA * np.log1p(0.0038))
    assert lo == pytest.approx(KAPPA * np.log1p(-0.0038))
    assert lo < 0 < hi and abs(lo) > hi
    with pytest.raises(ValueError):
        fv.no_arb_band_ann(1.5)


def test_binance_funding_formula_cases():
    # zero premium -> interest component only
    assert fv.binance_funding_rate(0.0) == pytest.approx(0.0001)
    # premium far above interest -> damper binds at -0.05%
    assert fv.binance_funding_rate(0.001) == pytest.approx(0.001 - 0.0005)
    # cap binds for majors (0.30%)
    assert fv.binance_funding_rate(0.05) == pytest.approx(0.003)
    # 4h settlement halves the rate per settlement
    assert fv.binance_funding_rate(0.0, interval_hours=4) == pytest.approx(0.00005)


def test_amortized_cost_and_round_trip():
    costs = CostModel()
    rt = costs.round_trip_frac(("spot", "fut"))
    assert rt == pytest.approx(2 * ((10 + 2) + (5 + 2)) / 1e4)  # 38 bp
    assert fv.amortized_cost_ann(rt, 30.0) == pytest.approx(rt * 365 / 30)
    assert fv.amortized_cost_ann(rt, 0.0) > 1e6  # degenerate horizon never yields a tiny hurdle


def test_trailing_funding_uses_only_past_prints():
    idx = pd.date_range("2025-01-01", periods=6, freq="8h", tz="UTC")
    s = pd.Series([0.0001, 0.0002, 0.0003, 0.0004, 0.0005, 0.0006], index=idx)
    out = fv.trailing_funding_ann(s, lookback_prints=3)
    assert out.iloc[-1] == pytest.approx(np.mean([0.0004, 0.0005, 0.0006]) * KAPPA)
    assert out.iloc[0] == pytest.approx(0.0001 * KAPPA)  # min_periods = 1 for a 3-print window


def test_calendar_excess_sign():
    assert fv.calendar_excess_ann(0.12, 0.08) == pytest.approx(0.04)
