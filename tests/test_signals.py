import numpy as np
import pandas as pd
import pytest

from basis_arb.config import BacktestConfig, StrategyParams
from basis_arb.model import fair_value as fv
from basis_arb.model import signals as sg
from basis_arb.types import validate_panel
from tests.conftest import make_panel

WARMUP_H = 24 * 30  # trailing funding needs a full 30-day window before S2/S3 can decide


def test_synthetic_panel_passes_contract(panel):
    validate_panel(panel)


def test_validate_panel_rejects_non_utc_and_percent_rates():
    p = make_panel(n_hours=48)
    bad_tz = p.copy()
    bad_tz.index = bad_tz.index.tz_convert("Asia/Seoul")
    bad_tz.index.name = "ts"
    with pytest.raises(ValueError, match="UTC"):
        validate_panel(bad_tz)
    with pytest.raises(ValueError, match="r_usd"):
        validate_panel(make_panel(n_hours=48, r_usd=4.0))
    bad_dte = p.copy()
    bad_dte.loc[bad_dte.index[0], "fut_cq_dte_days"] += 1.0
    with pytest.raises(ValueError, match="dte"):
        validate_panel(bad_dte)


def test_contract_chain_promotes_next_quarter_to_current(panel):
    first_exp = pd.Timestamp("2025-01-01", tz="UTC") + pd.Timedelta(days=60, hours=8)
    before = panel.loc[first_exp - pd.Timedelta(hours=1)]
    at = panel.loc[first_exp]
    assert before["fut_cq_symbol"] == "SYN_CQ" and before["fut_nq_symbol"] == "SYN_NQ"
    assert at["fut_cq_symbol"] == "SYN_NQ" and at["fut_nq_symbol"] is None
    assert np.isnan(at["fut_nq_close"])


def test_enrich_adds_model_columns(panel):
    p = sg.enrich(panel, BacktestConfig())
    for col in (
        "basis_cq_ann", "excess_cq_ann", "funding_trailing_ann", "excess_funding_ann", "rm_dev_ann", "calendar_cq_ann"
    ):
        assert col in p.columns
    # constant 10%/yr basis with r = 4% -> excess = 6%
    assert p["excess_cq_ann"].dropna().iloc[0] == pytest.approx(0.06, abs=1e-9)
    # constant 0.03%/8h funding -> 32.85%/yr trailing; fair = kappa*(r/(kappa-r)) ~ 4.0001%
    assert p["funding_trailing_ann"].dropna().iloc[-1] == pytest.approx(0.0003 * 1095, rel=1e-9)
    assert p["excess_funding_ann"].dropna().iloc[-1] == pytest.approx(0.0003 * 1095 - fv.fair_funding_ann(0.04), abs=1e-9)
    # full 30-day window required: NaN before, defined after
    assert p["funding_trailing_ann"].iloc[: WARMUP_H - 1].isna().all()
    assert p["funding_trailing_ann"].iloc[WARMUP_H + 8 :].notna().all()


def test_cash_and_carry_enters_when_excess_clears_hurdle_and_rolls_before_expiry():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(basis_ann_cq=0.20, basis_ann_nq=0.20), cfg)
    t = sg.cash_and_carry_targets(p, cfg)
    assert t["target"].iloc[0] == 1 and t["reason"].iloc[0] == "ENTER"
    assert t["contract"].iloc[0] == "SYN_CQ"
    roll_rows = t[t["reason"] == "ROLL"]
    assert len(roll_rows) == 1
    roll_ts = roll_rows.index[0]
    assert p.loc[roll_ts, "fut_cq_dte_days"] < 2.0  # inside the 2-day roll window of SYN_CQ
    # the ROLL row reports the next contract's excess/hurdle (the values the decision used)
    assert roll_rows["excess"].iloc[0] == pytest.approx(p.loc[roll_ts, "excess_nq_ann"])
    assert (t["contract"].iloc[-1] == "SYN_NQ") and t["target"].iloc[-1] == 1


def test_cash_and_carry_entry_boundary():
    cfg = BacktestConfig()
    rt = cfg.costs.round_trip_frac(("spot", "fut"))
    dte0 = 60 + 8 / 24
    hurdle0 = fv.amortized_cost_ann(rt, dte0) + 0.03
    just_above = sg.enrich(make_panel(basis_ann_cq=0.04 + hurdle0 + 1e-4, basis_ann_nq=0.04), cfg)
    just_below = sg.enrich(make_panel(basis_ann_cq=0.04 + hurdle0 - 1e-4, basis_ann_nq=0.04), cfg)
    assert sg.cash_and_carry_targets(just_above, cfg)["target"].iloc[0] == 1
    assert (sg.cash_and_carry_targets(just_below, cfg)["target"] == 0).all()  # hurdle only rises as dte shrinks


def test_cash_and_carry_stays_flat_when_basis_is_fair():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(basis_ann_cq=0.04, basis_ann_nq=0.04, r_usd=0.04), cfg)
    assert (sg.cash_and_carry_targets(p, cfg)["target"] == 0).all()


def test_cash_and_carry_never_reverses_even_if_flag_is_set():
    cfg = BacktestConfig(params=StrategyParams(allow_reverse=True))
    p = sg.enrich(make_panel(basis_ann_cq=-0.20, basis_ann_nq=-0.20), cfg)
    assert (sg.cash_and_carry_targets(p, cfg)["target"] == 0).all()


def test_cash_and_carry_hurdle_rises_near_expiry_and_exits_when_next_is_fair():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(basis_ann_cq=0.20, basis_ann_nq=0.04), cfg)
    t = sg.cash_and_carry_targets(p, cfg)
    h = t["hurdle"].dropna()
    assert h.iloc[0] < h.iloc[100]
    assert "EXIT" in set(t["reason"]) and t["target"].iloc[-1] == 0


def test_cash_and_carry_chain_handoff_across_three_contracts():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(n_hours=24 * 200, n_contracts=3, basis_ann_cq=0.20, basis_ann_nq=0.20), cfg)
    t = sg.cash_and_carry_targets(p, cfg)
    rolls = t[t["reason"] == "ROLL"]
    assert list(rolls["contract"]) == ["SYN_NQ", "SYN_Q3"]
    assert (t["target"] == 1).all()  # never flat: each roll clears the next contract's hurdle


def test_cash_and_carry_hold_to_expiry_settles_at_expiry_bar():
    cfg = BacktestConfig(params=StrategyParams(cc_hold_to_expiry=True))
    p = sg.enrich(make_panel(basis_ann_cq=0.20, basis_ann_nq=0.20), cfg)
    t = sg.cash_and_carry_targets(p, cfg)
    first_exp = pd.Timestamp("2025-01-01", tz="UTC") + pd.Timedelta(days=60, hours=8)
    assert "ROLL" not in set(t["reason"])
    assert t.loc[first_exp, "reason"] == "EXPIRY" and t.loc[first_exp, "target"] == 0
    assert t.loc[first_exp - pd.Timedelta(hours=1), "target"] == 1
    # re-enters the promoted contract on the next bar
    assert t.loc[first_exp + pd.Timedelta(hours=1), "target"] == 1
    assert t.loc[first_exp + pd.Timedelta(hours=1), "contract"] == "SYN_NQ"


def test_outage_rows_carry_no_decision():
    cfg = BacktestConfig()
    p = make_panel(perp_premium=0.0)
    rows = p.index[100:104]
    # exchange outage: spot close forward-filled (frozen) while the perp jumped 2% -> fake premium
    p.loc[rows, ["spot_high", "spot_low"]] = np.nan
    p.loc[rows, ["perp_close", "perp_high", "perp_low"]] = 50_000.0 * 1.02
    e = sg.enrich(p, cfg)
    assert e["stale_bar"].loc[rows].all() and e["stale_bar"].sum() == 4
    assert e.loc[rows, "rm_dev_ann"].isna().all() and e.loc[rows, "excess_cq_ann"].isna().all()
    assert (sg.random_maturity_targets(e, cfg)["target"] == 0).all()


def test_nan_symbol_never_enters():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(basis_ann_cq=0.20), cfg)
    p = p.copy()
    p["fut_cq_symbol"] = p["fut_cq_symbol"].astype(object)
    p.iloc[0, p.columns.get_loc("fut_cq_symbol")] = np.nan
    p.iloc[0, p.columns.get_loc("fut_nq_symbol")] = np.nan
    t = sg.cash_and_carry_targets(p, cfg)
    assert t["target"].iloc[0] == 0 and t["reason"].iloc[0] == "FLAT"


def test_simple_loop_hysteresis_min_hold_and_nan_hold():
    excess = np.where(np.arange(20) < 3, 0.30, -0.01)
    tgt, reason = sg._simple_loop(excess, np.full(20, 0.05), 0.0, 8, False)
    assert tgt[0] == 1 and tgt[3] == 1 and tgt[7] == 1 and tgt[8] == 0 and reason[8] == "EXIT"
    # NaN = no decision: the position is held, not exited
    excess2 = np.array([0.3, 0.3, np.nan, 0.3, np.nan, np.nan, np.nan, np.nan, np.nan, -0.1, -0.1])
    tgt2, reason2 = sg._simple_loop(excess2, np.full(11, 0.05), 0.0, 8, False)
    assert tgt2[2] == 1 and reason2[2] == "HOLD" and tgt2[8] == 1 and tgt2[9] == 0
    # NaN before any entry: stays flat
    tgt3, _ = sg._simple_loop(np.array([np.nan, np.nan, 0.3]), np.full(3, 0.05), 0.0, 8, False)
    assert list(tgt3) == [0, 0, 1]


def test_simple_loop_reverse_uses_lower_band():
    lo, hi = fv.no_arb_band_ann(0.0038)
    inside = np.full(5, (lo - hi) / 2.0)  # between the (more negative) lower edge and -hi
    tgt, _ = sg._simple_loop(inside, np.full(5, hi), 0.0, 8, True, hurdle_lo=np.full(5, lo))
    assert (tgt == 0).all()
    tgt2, _ = sg._simple_loop(np.full(5, lo - 0.01), np.full(5, hi), 0.0, 8, True, hurdle_lo=np.full(5, lo))
    assert tgt2[0] == -1


def test_funding_carry_enters_after_warmup_and_never_reverses():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(funding_per_period=0.0003), cfg)  # 32.85%/yr >> hurdle
    t = sg.funding_carry_targets(p, cfg)
    assert t["target"].iloc[0] == 0 and t["target"].iloc[-1] == 1
    first_enter = int(np.argmax(t["reason"].to_numpy() == "ENTER"))
    assert WARMUP_H - 8 <= first_enter <= WARMUP_H + 8
    neg = sg.enrich(make_panel(funding_per_period=-0.0005), cfg)
    assert (sg.funding_carry_targets(neg, cfg)["target"] == 0).all()


def test_funding_carry_hurdle_is_cost_derived():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(), cfg)
    t = sg.funding_carry_targets(p, cfg)
    rt = cfg.costs.round_trip_frac(("spot", "perp"))
    assert t["hurdle"].iloc[0] == pytest.approx(rt * 365 / 30 + 0.03)


def test_funding_window_handles_four_hour_regime():
    cfg = BacktestConfig()
    p = make_panel(funding_per_period=0.0003)
    # switch the whole sample to 4h stamps at half the per-print rate: same annualized funding
    fr = pd.Series(np.nan, index=p.index)
    fr.loc[p.index[p.index.hour % 4 == 0]] = 0.00015
    p["funding_rate"] = fr
    e = sg.enrich(p, cfg)
    assert e["funding_trailing_ann"].dropna().iloc[-1] == pytest.approx(0.0003 * 1095, rel=1e-9)


def test_funding_outage_turns_trailing_into_nan():
    cfg = BacktestConfig()
    p = make_panel()
    cut = p.index[24 * 80]
    p.loc[p.index >= cut, "funding_rate"] = np.nan
    e = sg.enrich(p, cfg)
    assert e["funding_trailing_ann"].loc[cut + pd.Timedelta(hours=12) :].isna().all()


def test_calendar_reverse_allowed_and_never_long_here():
    cfg = BacktestConfig()
    # quarterly basis 2%/yr but funding 32.85%/yr -> calendar excess ~ -31% -> short perp / long quarterly
    p = sg.enrich(make_panel(basis_ann_cq=0.02, basis_ann_nq=0.02, funding_per_period=0.0003), cfg)
    t = sg.calendar_targets(p, cfg)
    assert t["target"].iloc[0] == 0  # warm-up
    assert (t["target"] == -1).any() and t["target"].iloc[-1] == -1
    assert (t["target"] != 1).all()


def test_random_maturity_band():
    cfg = BacktestConfig()
    p = sg.enrich(make_panel(perp_premium=0.005), cfg)  # 0.5% premium >> 38 bp band
    t = sg.random_maturity_targets(p, cfg)
    assert t["target"].iloc[0] == 1
    assert t["hurdle"].iloc[0] == pytest.approx(fv.no_arb_band_ann(cfg.costs.round_trip_frac(("spot", "perp")))[1])
    flat = sg.enrich(make_panel(perp_premium=0.0), cfg)
    assert (sg.random_maturity_targets(flat, cfg)["target"] == 0).all()
    discount = sg.enrich(make_panel(perp_premium=-0.01), cfg)  # deep discount: no reverse without spot borrow
    assert (sg.random_maturity_targets(discount, cfg)["target"] == 0).all()


def test_build_targets_returns_all_strategies(panel):
    out = sg.build_targets(panel, BacktestConfig())
    assert set(out) == {"cash_and_carry", "funding_carry", "calendar", "random_maturity"}
    for df in out.values():
        assert tuple(df.columns) == sg.TARGET_COLUMNS
        assert len(df) == len(panel)
