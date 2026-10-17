import numpy as np
import pytest

from basis_arb.config import BacktestConfig, StrategyParams
from basis_arb.model import signals as sg
from basis_arb.types import validate_panel
from tests.conftest import make_panel


def test_synthetic_panel_passes_contract(panel):
    validate_panel(panel)


def test_enrich_adds_model_columns(panel):
    p = sg.enrich(panel, BacktestConfig())
    for col in (
        "basis_cq_ann", "excess_cq_ann", "funding_trailing_ann", "excess_funding_ann", "rm_dev_ann", "calendar_cq_ann"
    ):
        assert col in p.columns
    # constant 10%/yr basis with r = 4% -> excess ~ 6%
    assert p["excess_cq_ann"].dropna().iloc[0] == pytest.approx(0.06, abs=1e-9)
    # constant 0.03%/8h funding -> 32.85%/yr trailing, fair ~ 4% -> excess ~ 28.85%
    assert p["funding_trailing_ann"].dropna().iloc[-1] == pytest.approx(0.0003 * 1095, rel=1e-9)
    assert p["excess_funding_ann"].dropna().iloc[-1] == pytest.approx(0.0003 * 1095 - 0.04, rel=1e-3)


def test_cash_and_carry_enters_when_excess_clears_hurdle_and_rolls_before_expiry():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(basis_ann_cq=0.20, basis_ann_nq=0.20), cfg)
    t = sg.cash_and_carry_targets(p, cfg)
    assert t["target"].iloc[0] == 1 and t["reason"].iloc[0] == "ENTER"
    assert t["contract"].iloc[0] == "SYN_CQ"
    # roll window: 2 days before cq expiry (60d + 8h from start) the position rolls into nq
    roll_rows = t[t["reason"] == "ROLL"]
    assert len(roll_rows) == 1
    assert (t["contract"].iloc[-1] == "SYN_NQ") and t["target"].iloc[-1] == 1


def test_cash_and_carry_stays_flat_when_basis_is_fair():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(basis_ann_cq=0.04, basis_ann_nq=0.04, r_usd=0.04), cfg)
    t = sg.cash_and_carry_targets(p, cfg)
    assert (t["target"] == 0).all()


def test_cash_and_carry_hurdle_rises_near_expiry():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(basis_ann_cq=0.20, basis_ann_nq=0.04), cfg)
    t = sg.cash_and_carry_targets(p, cfg)
    h = t["hurdle"].dropna()
    assert h.iloc[0] < h.iloc[100]  # amortized cost grows as days-to-expiry shrinks
    # nq basis is fair -> cannot roll -> EXIT inside the roll window, then flat
    assert "EXIT" in set(t["reason"]) and t["target"].iloc[-1] == 0


def test_funding_carry_hysteresis_and_min_hold():
    cfg = BacktestConfig(params=StrategyParams(min_hold_bars=8))
    base = make_panel(funding_per_period=0.0003)  # 32.85%/yr >> hurdle
    p = sg.enrich(base, cfg)
    t = sg.funding_carry_targets(p, cfg)
    # trailing funding needs min_periods = 30 prints (10 days) before it is defined -> flat during warm-up
    assert t["target"].iloc[0] == 0 and t["target"].iloc[-1] == 1
    first_enter = int(np.argmax(t["reason"].to_numpy() == "ENTER"))
    assert 24 * 9 <= first_enter <= 24 * 11
    # excess at/below exit -> EXIT, but only after min hold
    excess = np.where(np.arange(len(p)) < 3, 0.30, -0.01)
    tgt, reason = sg._simple_loop(excess, np.full(len(p), 0.05), 0.0, 8, False)
    assert tgt[0] == 1 and tgt[3] == 1 and tgt[7] == 1 and tgt[8] == 0 and reason[8] == "EXIT"


def test_funding_carry_no_reverse_by_default():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(funding_per_period=-0.0005), cfg)
    t = sg.funding_carry_targets(p, cfg)
    assert (t["target"] == 0).all()


def test_calendar_reverse_allowed():
    cfg = BacktestConfig()
    # quarterly basis 2%/yr but funding 32.85%/yr -> calendar excess ~ -31% -> short perp / long quarterly
    p = sg.enrich(make_panel(basis_ann_cq=0.02, basis_ann_nq=0.02, funding_per_period=0.0003), cfg)
    t = sg.calendar_targets(p, cfg)
    assert t["target"].iloc[0] == 0  # trailing-funding warm-up
    assert (t["target"] == -1).any() and t["target"].iloc[-1] == -1
    assert (t["target"] != 1).all()


def test_random_maturity_band():
    cfg = BacktestConfig()
    # premium 0.5% >> band (38bp round trip) -> enter; no premium -> never enters
    p = sg.enrich(make_panel(perp_premium=0.005), cfg)
    t = sg.random_maturity_targets(p, cfg)
    assert t["target"].iloc[0] == 1
    p2 = sg.enrich(make_panel(perp_premium=0.0), cfg)
    t2 = sg.random_maturity_targets(p2, cfg)
    assert (t2["target"] == 0).all()


def test_build_targets_returns_all_strategies(panel):
    out = sg.build_targets(panel, BacktestConfig())
    assert set(out) == {"cash_and_carry", "funding_carry", "calendar", "random_maturity"}
    for df in out.values():
        assert tuple(df.columns) == sg.TARGET_COLUMNS
        assert len(df) == len(panel)
