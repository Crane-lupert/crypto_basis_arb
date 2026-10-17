"""Backtest engine tests on deterministic synthetic panels (tests.conftest.make_panel), spec v1.0.2."""
from __future__ import annotations

import dataclasses
import json
import math
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from basis_arb.backtest.engine import (
    PNL_COLUMNS, RESIZE_THRESHOLD, TRADE_COLUMNS, fills_frame, prepare_panel, run_backtest,
)
from basis_arb.backtest.run import STRATEGY_NAMES
from basis_arb.backtest.sensitivity import FULL_GRID, REPLICATION_STRATEGIES, run_grid
from basis_arb.config import BacktestConfig, MarginModel, StrategyParams
from basis_arb.model import signals
from basis_arb.types import STRATEGIES
from tests.conftest import make_panel

NOTIONAL = 10_000.0
SPOT = 50_000.0
START = pd.Timestamp("2025-01-01", tz="UTC")
CQ_EXPIRY = START + pd.Timedelta(days=60, hours=8)
PRICE_COLUMNS = (
    "spot_close", "spot_high", "spot_low", "perp_close", "perp_high", "perp_low",
    "fut_cq_close", "fut_cq_high", "fut_cq_low", "fut_nq_close", "fut_nq_high", "fut_nq_low",
)
CROSS = MarginModel(mode="cross_collateral")


def manual_targets(panel: pd.DataFrame, enter: int, exit_: int, direction: int = 1, contract=None) -> pd.DataFrame:
    """Hold `direction` on bars [enter, exit_) and be flat elsewhere (bypasses the signal state machines)."""
    n = len(panel)
    target = np.zeros(n, dtype=np.int8)
    target[enter:exit_] = direction
    reason = np.full(n, "FLAT", dtype=object)
    reason[enter:exit_] = "HOLD"
    reason[enter] = "ENTER"
    if exit_ < n:
        reason[exit_] = "EXIT"
    con = np.full(n, None, dtype=object)
    if contract is not None:
        con[enter:exit_] = contract
    return pd.DataFrame(
        {"target": target, "contract": con, "excess": np.nan, "hurdle": np.nan, "reason": reason}, index=panel.index
    )


def scaled_panel(mult: float, at_bar: int, n_hours: int = 24 * 5, wick_only: bool = False) -> pd.DataFrame:
    """Constant 0.03%/8h funding; every price is multiplied by `mult` from `at_bar` on (dates untouched).
    With wick_only, only the highs (mult > 1) or lows (mult < 1) of that single bar move."""
    panel = make_panel(funding_per_period=0.0003, n_hours=n_hours)
    if wick_only:
        col = "high" if mult > 1 else "low"
        for leg in ("spot", "perp", "fut_cq", "fut_nq"):
            c = f"{leg}_{col}"
            panel.iloc[at_bar, panel.columns.get_loc(c)] = panel[f"{leg}_close"].iloc[at_bar] * mult
        return panel
    m = np.ones(len(panel))
    m[at_bar:] = mult
    for col in PRICE_COLUMNS:
        panel[col] = panel[col] * m
    return panel


# --- 1. cash-and-carry: basis convergence and cost accounting --------------------------------------
def test_cash_and_carry_pnl_basis_matches_basis_decay_and_costs_match_cost_model():
    b, cfg = 0.20, BacktestConfig()
    panel = make_panel(basis_ann_cq=b, basis_ann_nq=0.04, r_usd=0.04)  # next quarter is fair -> exit, no roll
    res = run_backtest(panel, "cash_and_carry", cfg)
    assert len(res.trades) == 1
    tr = res.trades.iloc[0]
    assert tr.exit_reason == "EXIT" and tr.contract == "SYN_CQ" and tr.direction == 1
    dte = panel["fut_cq_dte_days"]
    dte_entry, dte_exit = dte.loc[tr.entry_ts], dte.loc[tr.exit_ts]
    assert tr.entry_ts == START and dte_exit < 2.0 <= dte.shift(1).loc[tr.exit_ts]  # first bar in the roll window
    analytic = NOTIONAL * (math.exp(b * dte_entry / 365.0) - math.exp(b * dte_exit / 365.0))
    assert tr.pnl_basis == pytest.approx(analytic, rel=0.02)

    c = cfg.costs
    s, f_spot, f_fut = c.slippage_bps / 1e4, c.spot_fee_bps / 1e4, c.fut_fee_bps / 1e4
    q = NOTIONAL / SPOT
    f0, f1 = panel["fut_cq_close"].loc[tr.entry_ts], panel["fut_cq_close"].loc[tr.exit_ts]
    expected = (
        q * SPOT * (1 + s) * f_spot + q * SPOT * s  # buy spot
        + q * f0 * (1 - s) * f_fut + q * f0 * s  # sell quarterly
        + q * SPOT * (1 - s) * f_spot + q * SPOT * s  # sell spot
        + q * f1 * (1 + s) * f_fut + q * f1 * s  # buy back quarterly
    )
    assert tr.pnl_costs == pytest.approx(-expected, rel=1e-9)
    assert tr.cost_fees + tr.cost_slippage == pytest.approx(expected, rel=1e-9)
    assert tr.pnl_funding == 0.0 and tr.n_resizes == 0
    assert tr.pnl_total == pytest.approx(tr.pnl_basis + tr.pnl_costs)
    assert res.equity.iloc[-1] - res.capital == pytest.approx(tr.pnl_total)
    fills = fills_frame(res.fills)
    assert fills["fee"].sum() == pytest.approx(tr.cost_fees)
    assert list(fills["reason"]) == ["ENTER", "ENTER", "EXIT", "EXIT"]


def test_roll_swaps_only_the_quarterly_leg_and_marks_the_decay_up_to_the_roll():
    b = 0.20
    panel = make_panel(basis_ann_cq=b, basis_ann_nq=b)
    res = run_backtest(panel, "cash_and_carry", BacktestConfig())
    assert len(res.trades) == 1
    tr = res.trades.iloc[0]
    assert tr.contract == "SYN_CQ>SYN_NQ" and tr.n_rolls == 1 and tr.exit_reason == "OPEN"
    fills = fills_frame(res.fills)
    roll = fills[fills["reason"] == "ROLL"]
    assert list(roll["instrument"]) == ["SYN_CQ", "SYN_NQ"] and list(roll["side"]) == ["buy", "sell"]
    roll_ts = roll["ts"].iloc[0]
    assert not ((fills["leg"] == "spot") & (fills["ts"] == roll_ts)).any()  # spot leg untouched
    assert roll["qty"].nunique() == 1  # same coin quantity on the new contract
    assert (res.events["event"] == "ROLL").sum() == 1
    dte = panel["fut_cq_dte_days"]
    decay = res.pnl_components["basis"].loc[tr.entry_ts:roll_ts].sum()
    analytic = NOTIONAL * (math.exp(b * dte.loc[tr.entry_ts] / 365.0) - math.exp(b * dte.loc[roll_ts] / 365.0))
    assert decay == pytest.approx(analytic, rel=0.02)
    # after SYN_CQ expires, SYN_NQ is promoted to the cq columns: the held leg keeps being marked
    assert res.final_positions and {p.instrument for p in res.final_positions} == {"SYN", "SYN_NQ"}


# --- 2. funding accrual ---------------------------------------------------------------------------
def test_funding_accrual_on_signal_targets_skips_the_entry_stamp():
    f, cfg = 0.0003, BacktestConfig()
    panel = make_panel(funding_per_period=f)
    tg = signals.funding_carry_targets(signals.enrich(panel, cfg), cfg)
    res = run_backtest(panel, "funding_carry", cfg, targets=tg)
    enter_bar = int(np.flatnonzero(tg["reason"].to_numpy() == "ENTER")[0])
    stamps = np.flatnonzero(panel["funding_rate"].notna().to_numpy())
    assert enter_bar in stamps  # the trailing window first fills on a stamp bar -> the entry is on a stamp
    assert (tg["target"].iloc[enter_bar:] == 1).all()
    n_eff = int((stamps > enter_bar).sum())
    qty = NOTIONAL / panel["spot_close"].iloc[enter_bar]
    expected = n_eff * f * qty * panel["perp_close"].iloc[0]
    assert res.trades.iloc[0]["pnl_funding"] == pytest.approx(expected, rel=1e-12)
    assert res.pnl_components["funding"].sum() == pytest.approx(expected, rel=1e-12)
    assert res.pnl_components["funding"].iloc[enter_bar] == 0.0


@pytest.mark.parametrize("enter, exit_, n_stamps", [(3, 16, 2), (8, 20, 1), (8, 16, 1), (9, 15, 0)])
def test_funding_paid_to_legs_held_before_the_bar(enter, exit_, n_stamps):
    panel = make_panel(funding_per_period=0.0003, n_hours=24 * 3)
    res = run_backtest(panel, "funding_carry", BacktestConfig(), targets=manual_targets(panel, enter, exit_))
    qty = NOTIONAL / SPOT
    assert res.trades.iloc[0]["pnl_funding"] == pytest.approx(n_stamps * 0.0003 * qty * SPOT, rel=1e-12)


# --- 3. expiry settlement -------------------------------------------------------------------------
def test_hold_to_expiry_settles_at_spot_close_with_delivery_fee():
    cfg = BacktestConfig(params=StrategyParams(cc_hold_to_expiry=True))
    panel = make_panel(basis_ann_cq=0.20, basis_ann_nq=0.20)
    res = run_backtest(panel, "cash_and_carry", cfg)
    tr = res.trades.iloc[0]
    assert tr.exit_reason == "EXPIRY" and tr.exit_ts == CQ_EXPIRY and tr.contract == "SYN_CQ" and tr.n_rolls == 0
    fills = fills_frame(res.fills)
    at = fills[fills["ts"] == CQ_EXPIRY]
    settle, spot_exit = at[at["leg"] == "fut"], at[at["leg"] == "spot"]
    spot_px = panel["spot_close"].loc[CQ_EXPIRY]
    q = NOTIONAL / SPOT
    assert len(settle) == 1 and settle["reason"].iloc[0] == "EXPIRY" and settle["instrument"].iloc[0] == "SYN_CQ"
    assert settle["price"].iloc[0] == spot_px
    assert settle["fee"].iloc[0] == pytest.approx(q * spot_px * cfg.costs.delivery_fee_bps / 1e4)
    assert len(spot_exit) == 1 and spot_exit["side"].iloc[0] == "sell" and spot_exit["reason"].iloc[0] == "EXPIRY"
    assert tr.cost_delivery == pytest.approx(q * spot_px * cfg.costs.delivery_fee_bps / 1e4)
    f0 = panel["fut_cq_close"].loc[tr.entry_ts]
    assert tr.pnl_basis == pytest.approx(q * (f0 - spot_px), rel=1e-9)  # full convergence to spot
    ev = res.events[res.events["event"] == "EXPIRY"]
    assert len(ev) == 1 and ev["ts"].iloc[0] == CQ_EXPIRY
    # a fresh ENTER into the promoted contract follows on the next bar (new decision, new round trip)
    assert len(res.trades) == 2 and res.trades.iloc[1]["entry_ts"] == CQ_EXPIRY + pd.Timedelta(hours=1)


def test_settlement_happens_even_if_the_target_still_holds_the_expired_contract():
    panel = make_panel(basis_ann_cq=0.20, basis_ann_nq=0.20)
    exp_bar = int(np.flatnonzero(panel.index == CQ_EXPIRY)[0])
    tg = manual_targets(panel, 0, exp_bar + 24, contract="SYN_CQ")  # stale target past expiry
    res = run_backtest(panel, "cash_and_carry", BacktestConfig(), targets=tg)
    assert res.trades.iloc[0]["exit_reason"] == "EXPIRY" and res.trades.iloc[0]["exit_ts"] == CQ_EXPIRY
    assert len(res.trades) == 1  # the engine does not re-open the expired contract
    assert (res.position.iloc[exp_bar:] == 0).all()


# --- 4. margin monitor (close prices) ----------------------------------------------------------------
def test_isolated_liquidation_on_a_close_move_closes_both_legs_at_the_close():
    jump = 24 * 2 + 3  # a non-stamp bar, after the manual entry
    panel = scaled_panel(1.6, jump)
    cfg = BacktestConfig(margin=MarginModel(perp_leverage=2.0, mode="isolated"))
    res = run_backtest(panel, "funding_carry", cfg, targets=manual_targets(panel, 5, len(panel)))
    liq = res.events[res.events["event"] == "LIQUIDATION"]
    assert len(liq) == 1 and liq["leg"].iloc[0] == "perp" and liq["ts"].iloc[0] == panel.index[jump]
    fills = fills_frame(res.fills)
    at = fills[fills["ts"] == panel.index[jump]]
    assert sorted(at["leg"]) == ["perp", "spot"] and (at["reason"] == "LIQUIDATION").all()
    close = panel["perp_close"].iloc[jump]
    s = cfg.costs.slippage_bps / 1e4
    assert at[at["leg"] == "perp"]["price"].iloc[0] == pytest.approx(close * (1 + s))  # at the bar's close
    assert len(res.trades) == 1 and res.trades.iloc[0]["exit_reason"] == "LIQUIDATION"  # no re-entry
    tr = res.trades.iloc[0]
    penalty = tr.qty * close * cfg.costs.liquidation_penalty_bps / 1e4
    assert res.pnl_components["liquidation"].sum() == pytest.approx(-penalty)
    assert liq["price"].iloc[0] == pytest.approx(close)
    assert (res.position.iloc[jump:] == 0).all()


def test_intrabar_wick_never_liquidates_and_is_counted_as_a_diagnostic():
    wick = 24 * 2 + 3
    panel = scaled_panel(1.6, wick, wick_only=True)  # perp high +60% on one bar, close unchanged
    res = run_backtest(panel, "funding_carry", BacktestConfig(), targets=manual_targets(panel, 5, len(panel)))
    assert not (res.events["event"] == "LIQUIDATION").any()
    ms = res.margin_stats
    assert ms["intrabar_breach_bars"] == 1 and ms["peak_intrabar_utilization"] > 1.0
    assert ms["peak_utilization"] < 0.01  # closes never moved
    assert res.trades.iloc[0]["exit_reason"] == "OPEN"


def test_cross_collateral_never_liquidates_reports_utilization_and_resizes_at_next_stamp():
    jump = 24 * 2 + 3  # 03:00 -> next stamp 08:00
    panel = scaled_panel(1.6, jump)
    cfg = BacktestConfig(margin=CROSS)
    res = run_backtest(panel, "funding_carry", cfg, targets=manual_targets(panel, 5, len(panel)))
    assert not (res.events["event"] == "LIQUIDATION").any() and res.margin_stats["n_liquidations"] == 0
    entry = [f.price for f in res.fills if f.leg == "perp" and f.reason == "ENTER"][0]
    peak_move = panel["perp_close"].iloc[jump] / entry - 1.0
    assert res.margin_stats["peak_utilization"] == pytest.approx(peak_move / cfg.margin.liquidation_move("perp"))
    assert res.margin_stats["peak_utilization"] > 1.0
    resize = res.events[res.events["event"] == "RESIZE"]
    assert len(resize) == 1 and resize["ts"].iloc[0] == panel.index[jump + 5]


def test_calendar_uses_futures_pair_mode():
    panel = scaled_panel(1.6, 24 * 2 + 3)
    tg = manual_targets(panel, 5, len(panel), direction=1, contract="SYN_CQ")
    iso_pair = BacktestConfig(margin=MarginModel(mode="cross_collateral", futures_pair_mode="isolated"))
    default = run_backtest(panel, "calendar", BacktestConfig(), targets=tg)  # spot-leg mode isolated, pair cross
    assert default.margin_stats["mode"] == "cross_collateral" and default.margin_stats["n_liquidations"] == 0
    iso = run_backtest(panel, "calendar", iso_pair, targets=tg)  # the short quarterly leg breaches on the close
    assert iso.margin_stats["mode"] == "isolated" and iso.margin_stats["n_liquidations"] == 1
    assert np.isfinite(default.margin_stats["peak_pair_utilization"])


# --- resize rule --------------------------------------------------------------------------------------
def test_two_x_price_move_triggers_one_resize_at_the_next_stamp():
    jump = 24 * 2 + 3  # 03:00 on day 3; the next funding stamp is 08:00
    panel = scaled_panel(2.0, jump)
    cfg = BacktestConfig(margin=CROSS)
    res = run_backtest(panel, "funding_carry", cfg, targets=manual_targets(panel, 5, len(panel)))
    resize = res.events[res.events["event"] == "RESIZE"]
    assert len(resize) == 1 and resize["ts"].iloc[0] == panel.index[jump + 5]
    fills = fills_frame(res.fills)
    rf = fills[fills["reason"] == "RESIZE"]
    q0 = NOTIONAL / SPOT
    assert sorted(rf["leg"]) == ["perp", "spot"] and rf["qty"].to_numpy() == pytest.approx([q0 / 2, q0 / 2])
    assert dict(zip(rf["leg"], rf["side"])) == {"spot": "sell", "perp": "buy"}  # both legs back to 10k notional
    s, c = cfg.costs.slippage_bps / 1e4, cfg.costs
    px = 2 * SPOT
    cost = (q0 / 2) * px * (1 - s) * c.spot_fee_bps / 1e4 + (q0 / 2) * px * (1 + s) * c.fut_fee_bps / 1e4 \
        + 2 * (q0 / 2) * px * s
    assert res.pnl_components["resize"].sum() == pytest.approx(-cost)
    assert res.trades.iloc[0]["n_resizes"] == 1 and res.margin_stats["n_resizes"] == 1
    final = {p.leg: p.qty for p in res.final_positions}
    assert final["spot"] * px == pytest.approx(NOTIONAL) and final["perp"] == pytest.approx(-final["spot"])
    prev = res.equity.shift(1).fillna(res.capital)
    np.testing.assert_allclose(res.pnl_components.sum(axis=1), res.equity - prev, atol=1e-7)


def test_small_moves_do_not_resize_and_a_halving_resizes_up():
    small = scaled_panel(1.0 + RESIZE_THRESHOLD * 0.9, 24 * 2 + 3)
    res = run_backtest(small, "funding_carry", BacktestConfig(), targets=manual_targets(small, 5, len(small)))
    assert not (res.events["event"] == "RESIZE").any()
    half = scaled_panel(0.5, 24 * 2 + 3)  # short perp gains: no margin issue in isolated mode
    res = run_backtest(half, "funding_carry", BacktestConfig(), targets=manual_targets(half, 5, len(half)))
    rf = fills_frame(res.fills).query("reason == 'RESIZE'")
    assert dict(zip(rf["leg"], rf["side"])) == {"spot": "buy", "perp": "sell"}
    assert res.margin_stats["n_liquidations"] == 0 and res.margin_stats["n_resizes"] == 1


# --- capital and returns --------------------------------------------------------------------------------
def test_capital_is_required_capital_and_target_when_flat():
    panel = scaled_panel(1.2, 24 * 2 + 3)  # +20%: below the resize threshold, capital drifts with the spot value
    cfg = BacktestConfig()
    res = run_backtest(panel, "funding_carry", cfg, targets=manual_targets(panel, 5, 24 * 4))
    target = NOTIONAL * (1 + 1 / cfg.margin.perp_leverage)
    assert res.capital == pytest.approx(target)
    cap = res.capital_t
    assert cap.iloc[:5].eq(target).all() and cap.iloc[24 * 4:].eq(target).all()  # flat: target capital
    q = NOTIONAL / SPOT
    i = 24 * 3
    expected = q * panel["spot_close"].iloc[i] + q * panel["perp_close"].iloc[i] / cfg.margin.perp_leverage
    assert cap.iloc[i] == pytest.approx(expected)
    prev_cap = cap.shift(1).fillna(res.capital)
    np.testing.assert_allclose(res.returns_hourly * prev_cap, res.equity.diff().fillna(res.equity.iloc[0] - res.capital),
                               atol=1e-7)
    assert res.returns_hourly.notna().all() and (res.returns_hourly.iloc[24 * 4 + 1:] == 0).all()


def test_capital_units_follow_the_spec():
    panel = make_panel(n_hours=48)
    lev2 = BacktestConfig()
    lev3 = BacktestConfig(margin=MarginModel(perp_leverage=3.0, fut_leverage=3.0))
    for s in ("cash_and_carry", "funding_carry", "random_maturity"):
        assert run_backtest(panel, s, lev2).capital == pytest.approx(NOTIONAL * 1.5)
        assert run_backtest(panel, s, lev3).capital == pytest.approx(NOTIONAL * (1 + 1 / 3))
    assert run_backtest(panel, "calendar", lev2).capital == pytest.approx(NOTIONAL)
    assert run_backtest(panel, "calendar", lev3).capital == pytest.approx(2 * NOTIONAL / 3)


# --- fill lag -------------------------------------------------------------------------------------------
def test_fill_lag_one_executes_one_bar_later():
    panel = scaled_panel(1.0, 0)
    ramp = np.linspace(1.0, 1.1, len(panel))
    for col in PRICE_COLUMNS:
        panel[col] = panel[col] * ramp
    tg = manual_targets(panel, 5, 30)
    lag0 = run_backtest(panel, "funding_carry", BacktestConfig(), targets=tg)
    cfg1 = BacktestConfig(params=dataclasses.replace(StrategyParams(), fill_lag_bars=1))
    lag1 = run_backtest(panel, "funding_carry", cfg1, targets=tg)
    f0, f1 = fills_frame(lag0.fills), fills_frame(lag1.fills)
    assert len(f0) == len(f1) == 4
    assert (f1["ts"] - f0["ts"]).eq(pd.Timedelta(hours=1)).all()
    s = BacktestConfig().costs.slippage_bps / 1e4
    for _, r in f1.iterrows():
        close = panel.loc[r["ts"], "spot_close" if r["leg"] == "spot" else "perp_close"]
        assert r["price"] == pytest.approx(close * (1 + s if r["side"] == "buy" else 1 - s))
    assert lag1.trades.iloc[0]["entry_ts"] == panel.index[6]


# --- 6. no look-ahead / 7. determinism --------------------------------------------------------------
def _busy_panel(**kw) -> pd.DataFrame:
    """All four strategies trade: 20%/yr basis, 0.03%/8h funding, 0.5% perp premium."""
    return make_panel(basis_ann_cq=0.20, basis_ann_nq=0.20, funding_per_period=0.0003, perp_premium=0.005,
                      n_contracts=3, **kw)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("lag", [0, 1])
def test_no_look_ahead(strategy, lag):
    cfg = BacktestConfig(params=dataclasses.replace(StrategyParams(), fill_lag_bars=lag))
    base = _busy_panel()
    t_cut = base.index[24 * 45]
    mod = base.copy()
    after = mod.index >= t_cut
    for col in PRICE_COLUMNS:
        mod.loc[after, col] = mod.loc[after, col] * (1.5 if col.startswith("fut_") else 1.1)
    mod.loc[after, "funding_rate"] = mod.loc[after, "funding_rate"] * -2.0
    a, b = run_backtest(base, strategy, cfg), run_backtest(mod, strategy, cfg)
    before_a = [f for f in a.fills if f.ts < t_cut]
    assert before_a, "the strategy should trade before the cut"
    assert before_a == [f for f in b.fills if f.ts < t_cut]
    pre = a.equity.index < t_cut
    assert np.array_equal(a.equity.to_numpy()[pre], b.equity.to_numpy()[pre])
    assert not np.array_equal(a.equity.to_numpy(), b.equity.to_numpy())  # the change after the cut matters


def test_determinism_equity_and_trades_csv_bytes(tmp_path):
    cfg = BacktestConfig()
    panel = _busy_panel()
    for s in STRATEGIES:
        a, b = run_backtest(panel, s, cfg), run_backtest(panel.copy(), s, cfg)
        assert np.array_equal(a.equity.to_numpy(), b.equity.to_numpy())
        pa, pb = tmp_path / f"a_{s}.csv", tmp_path / f"b_{s}.csv"
        a.trades.to_csv(pa, index=False, lineterminator="\n")
        b.trades.to_csv(pb, index=False, lineterminator="\n")
        assert pa.read_bytes() == pb.read_bytes()


# --- accounting invariants ----------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_pnl_components_add_up_to_equity_changes(strategy):
    res = run_backtest(_busy_panel(), strategy, BacktestConfig())
    assert tuple(res.pnl_components.columns) == PNL_COLUMNS and tuple(res.trades.columns) == TRADE_COLUMNS
    prev = res.equity.shift(1).fillna(res.capital)
    np.testing.assert_allclose(res.pnl_components.sum(axis=1), res.equity - prev, atol=1e-7)
    prev_cap = res.capital_t.shift(1).fillna(res.capital)
    np.testing.assert_allclose(res.returns_hourly * prev_cap, res.equity - prev, atol=1e-7)
    tr = res.trades
    assert tr["pnl_total"].sum() == pytest.approx(res.equity.iloc[-1] - res.capital, abs=1e-6)
    np.testing.assert_allclose(tr["pnl_total"], tr["pnl_basis"] + tr["pnl_funding"] + tr["pnl_costs"], atol=1e-9)


def test_reverse_calendar_flips_both_legs_with_equal_coin_quantity():
    panel = make_panel(basis_ann_cq=0.02, basis_ann_nq=0.02, funding_per_period=0.0003, perp_premium=0.001)
    res = run_backtest(panel, "calendar", BacktestConfig())
    tr = res.trades.iloc[0]
    assert tr.direction == -1
    entry = [f for f in res.fills if f.reason == "ENTER"]
    sides = {f.leg: f.side for f in entry}
    assert sides == {"perp": "sell", "fut": "buy"}
    qty_expected = NOTIONAL / panel["perp_close"].loc[tr.entry_ts]  # first leg of S3 is the perpetual
    assert all(f.qty == pytest.approx(qty_expected) for f in entry)
    assert tr.pnl_funding > 0  # the short perp collects positive funding


def test_unpriced_contract_bar_marks_at_last_price_and_logs_stale_close():
    panel = make_panel(basis_ann_cq=0.20, basis_ann_nq=0.20)
    exit_bar = 100
    for col in ("fut_cq_close", "fut_cq_high", "fut_cq_low"):
        panel.iloc[exit_bar - 2: exit_bar + 1, panel.columns.get_loc(col)] = np.nan
    res = run_backtest(panel, "cash_and_carry", BacktestConfig(), targets=manual_targets(panel, 0, exit_bar, contract="SYN_CQ"))
    stale = res.events[res.events["event"] == "STALE_PRICE"]
    assert len(stale) == 1 and stale["ts"].iloc[0] == panel.index[exit_bar]
    last_px = panel["fut_cq_close"].iloc[exit_bar - 3]
    fut_exit = [f for f in res.fills if f.leg == "fut" and f.reason == "EXIT"][0]
    assert fut_exit.price == pytest.approx(last_px * (1 + BacktestConfig().costs.slippage_bps / 1e4))


def test_input_contract_violations_raise():
    panel = make_panel(n_hours=48)
    cfg = BacktestConfig()
    with pytest.raises(ValueError, match="unknown strategy"):
        run_backtest(panel, "nope", cfg)
    with pytest.raises(ValueError, match="index"):
        run_backtest(panel, "funding_carry", cfg, targets=manual_targets(panel, 1, 5).iloc[1:])
    bad = manual_targets(panel, 1, 5)
    bad.loc[bad.index[2], "target"] = 2
    with pytest.raises(ValueError, match="-1, 0 or \\+1"):
        run_backtest(panel, "funding_carry", cfg, targets=bad)
    with pytest.raises(ValueError, match="contract"):
        run_backtest(panel, "cash_and_carry", cfg, targets=manual_targets(panel, 1, 5))
    with pytest.raises(ValueError, match="margin mode"):
        run_backtest(panel, "funding_carry", BacktestConfig(margin=MarginModel(mode="portfolio")))
    with pytest.raises(ValueError, match="different panel"):
        run_backtest(panel, "funding_carry", cfg, prepared=prepare_panel(make_panel(n_hours=72)))
    assert STRATEGY_NAMES == STRATEGIES


# --- sensitivity grid -------------------------------------------------------------------------------
def test_small_grid_marks_base_cell_and_dedupe_matches_full_recompute():
    panel = _busy_panel(n_hours=24 * 75)
    fast = run_grid(panel, "SYN", BacktestConfig(), FULL_GRID, mode="small")
    slow = run_grid(panel, "SYN", BacktestConfig(), FULL_GRID, mode="small", dedupe=False)
    n_rep = 3 * len(REPLICATION_STRATEGIES)
    assert fast.attrs["n_configs"] == 12 and len(fast) == 12 * len(STRATEGIES) + n_rep
    assert fast.attrs["n_replication_cells"] == 3
    assert set(fast[fast["kind"] == "replication"]["strategy"]) == set(REPLICATION_STRATEGIES)
    assert fast["is_base"].sum() == len(STRATEGIES)
    assert fast.attrs["n_unique_runs"] < slow.attrs["n_unique_runs"] == len(fast)
    pd.testing.assert_frame_equal(fast, slow)
    base = fast[fast["is_base"]].set_index("strategy")
    for s in STRATEGIES:
        direct = run_backtest(panel, s, BacktestConfig())
        assert base.loc[s, "pnl_total"] == pytest.approx(direct.equity.iloc[-1] - direct.capital, abs=1e-9)
    lag = fast[(fast["kind"] == "grid") & (fast["fill_lag"] == 1)]
    assert len(lag) == len(STRATEGIES)


# --- 8. CLI -----------------------------------------------------------------------------------------
def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "basis_arb.backtest.run", *args], capture_output=True, text=True, timeout=600
    )


def test_cli_smoke_writes_report_and_figures(tmp_path):
    panel_path = tmp_path / "panel_SYN_1h.parquet"
    _busy_panel(n_hours=24 * 100).to_parquet(panel_path)
    out = tmp_path / "report"
    proc = _cli("--root", "SYN", "--panel", str(panel_path), "--out", str(out), "--grid", "small")
    assert proc.returncode == 0, proc.stderr
    expected = ["summary.md", "metrics.json", "equity_curves.png", "drawdown.png", "theory_vs_market.png",
                "sensitivity.csv", "sensitivity.md"] + [f"trades_{s}.csv" for s in STRATEGIES]
    for name in expected:
        assert (out / name).is_file() and (out / name).stat().st_size > 0, name
    for png in ("equity_curves.png", "drawdown.png", "theory_vs_market.png"):
        assert (out / png).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "N_configs = 12" in md and "## Assumptions" in md and "Stress episodes" in md
    assert "Excess Sharpe" in md and "Funding diagnostics" in md and "HMRW-low" in md
    assert b"\r\n" not in (out / "summary.md").read_bytes()
    payload = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert set(payload["strategies"]) == set(STRATEGIES) and payload["n_configs"] == 12
    s1 = payload["strategies"]["cash_and_carry"]["summary"]
    assert {"ann_excess_return", "excess_sharpe", "ann_return_raw", "sharpe_raw", "n_resizes"} <= set(s1)


def test_cli_missing_panel_exits_nonzero(tmp_path):
    proc = _cli("--root", "SYN", "--panel", str(tmp_path / "missing.parquet"), "--out", str(tmp_path / "r"))
    assert proc.returncode != 0 and "not found" in proc.stderr
    assert not (tmp_path / "r").exists()
