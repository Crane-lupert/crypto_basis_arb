"""Pre-registered signal state machines for the four strategies.

Every strategy produces a *target* frame aligned to the panel index:
    target   int8   -1 / 0 / +1  (+1 = the carry direction listed in types.STRATEGY_LEGS)
    contract object  quarterly symbol held (cash_and_carry, calendar) or None
    excess   float   the annualized excess the decision was based on
    hurdle   float   the annualized entry hurdle at that bar
    reason   object  ENTER / HOLD / EXIT / ROLL / EXPIRY / FLAT

Decision rule (all strategies): enter when excess > hurdle (hurdle = amortized round-trip cost + buffer),
hold while excess > exit level, exit at or below the exit level once min_hold_bars have elapsed.
Decisions at bar t use data up to and including bar t's close; the engine fills at bar t's close.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from basis_arb.config import DAYS_PER_YEAR, KAPPA, BacktestConfig
from basis_arb.model import fair_value as fv
from basis_arb.types import STRATEGY_LEGS, validate_panel

TARGET_COLUMNS = ("target", "contract", "excess", "hurdle", "reason")


# --- enrichment --------------------------------------------------------------------------------
def enrich(panel: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """Append the model columns to a validated panel (pure; returns a copy)."""
    validate_panel(panel)
    p = panel.copy()
    r = p["r_usd"].astype(float)
    y = p["y_coin"].astype(float)

    # dated futures: market vs cost-of-carry
    for k in ("cq", "nq"):
        tau = p[f"fut_{k}_dte_days"].astype(float) / DAYS_PER_YEAR
        p[f"basis_{k}_ann"] = fv.annualized_basis(
            p[f"fut_{k}_close"].astype(float).to_numpy(), p["spot_close"].to_numpy(), tau.to_numpy()
        )
    p["fair_basis_ann"] = fv.fair_basis_ann(r.to_numpy(), y.to_numpy())
    for k in ("cq", "nq"):
        p[f"excess_{k}_ann"] = p[f"basis_{k}_ann"] - p["fair_basis_ann"]

    # perpetual: funding level vs equilibrium funding
    stamps = p["funding_rate"].dropna()
    trailing = fv.trailing_funding_ann(stamps, cfg.params.fc_lookback_prints, KAPPA)
    p["funding_trailing_ann"] = trailing.reindex(p.index).ffill()
    p["fair_funding_ann"] = fv.fair_funding_ann(r.to_numpy(), y.to_numpy(), KAPPA)
    p["excess_funding_ann"] = p["funding_trailing_ann"] - p["fair_funding_ann"]

    # perpetual: instantaneous premium vs HMRW fair premium
    p["perp_prem_ann"] = fv.perp_premium_ann(p["perp_close"].to_numpy(), p["spot_close"].to_numpy(), KAPPA)
    p["fair_perp_prem_ann"] = fv.fair_perp_premium_ann(r.to_numpy(), y.to_numpy(), KAPPA)
    p["rm_dev_ann"] = p["perp_prem_ann"] - p["fair_perp_prem_ann"]

    # calendar: quarterly basis vs expected funding
    for k in ("cq", "nq"):
        p[f"calendar_{k}_ann"] = fv.calendar_excess_ann(
            p[f"basis_{k}_ann"].to_numpy(), p["funding_trailing_ann"].to_numpy()
        )
    return p


# --- generic loops -----------------------------------------------------------------------------
def _simple_loop(
    excess: np.ndarray,
    hurdle: np.ndarray,
    exit_level: float,
    min_hold_bars: int,
    allow_reverse: bool,
) -> tuple[np.ndarray, np.ndarray]:
    n = len(excess)
    target = np.zeros(n, dtype=np.int8)
    reason = np.full(n, "FLAT", dtype=object)
    pos, held = 0, 0
    for i in range(n):
        e, h = excess[i], hurdle[i]
        valid = bool(np.isfinite(e) and np.isfinite(h))
        if pos == 0:
            if valid and e > h:
                pos, held, reason[i] = 1, 0, "ENTER"
            elif valid and allow_reverse and e < -h:
                pos, held, reason[i] = -1, 0, "ENTER"
        else:
            held += 1
            exit_now = (not valid) or (pos == 1 and e <= exit_level) or (pos == -1 and e >= -exit_level)
            if exit_now and (held >= min_hold_bars or not valid):
                pos, reason[i] = 0, "EXIT"
            else:
                reason[i] = "HOLD"
        target[i] = pos
    return target, reason


def _contract_loop(
    p: pd.DataFrame,
    excess_prefix: str,
    round_trip_frac: float,
    buffer_ann: float,
    exit_level: float,
    roll_days: float,
    hold_to_expiry: bool,
    min_hold_bars: int,
    allow_reverse: bool,
) -> pd.DataFrame:
    """Shared state machine for strategies whose futures leg is a specific quarterly contract."""
    n = len(p)
    cq_sym = p["fut_cq_symbol"].to_numpy(dtype=object)
    nq_sym = p["fut_nq_symbol"].to_numpy(dtype=object)
    ex = {"cq": p[f"{excess_prefix}_cq_ann"].to_numpy(float), "nq": p[f"{excess_prefix}_nq_ann"].to_numpy(float)}
    dte = {"cq": p["fut_cq_dte_days"].to_numpy(float), "nq": p["fut_nq_dte_days"].to_numpy(float)}
    sym = {"cq": cq_sym, "nq": nq_sym}

    def hurdle_at(k: str, i: int) -> float:
        d = dte[k][i]
        if not np.isfinite(d) or d <= 0:
            return np.nan
        return fv.amortized_cost_ann(round_trip_frac, d) + buffer_ann

    target = np.zeros(n, dtype=np.int8)
    contract = np.full(n, None, dtype=object)
    excess_out = np.full(n, np.nan)
    hurdle_out = np.full(n, np.nan)
    reason = np.full(n, "FLAT", dtype=object)
    pos, held_sym, held_bars = 0, None, 0

    for i in range(n):
        if pos == 0:
            # candidate contract: current quarter unless it is inside the roll window
            k = "cq" if (np.isfinite(dte["cq"][i]) and dte["cq"][i] >= roll_days) else "nq"
            h, e = hurdle_at(k, i), ex[k][i]
            excess_out[i], hurdle_out[i] = e, h
            if np.isfinite(e) and np.isfinite(h) and sym[k][i] is not None:
                if e > h:
                    pos, held_sym, held_bars, reason[i] = 1, sym[k][i], 0, "ENTER"
                elif allow_reverse and e < -h:
                    pos, held_sym, held_bars, reason[i] = -1, sym[k][i], 0, "ENTER"
        else:
            held_bars += 1
            k = "cq" if held_sym == cq_sym[i] else ("nq" if held_sym == nq_sym[i] else None)
            if k is None:
                # the held contract is no longer in the panel (expired or delisted): engine settles it
                pos, held_sym, reason[i] = 0, None, "EXPIRY"
            else:
                e, d = ex[k][i], dte[k][i]
                excess_out[i], hurdle_out[i] = e, hurdle_at(k, i)
                in_roll_window = (not np.isfinite(d)) or d < roll_days
                if in_roll_window and not hold_to_expiry:
                    e_nq, h_nq = ex["nq"][i], hurdle_at("nq", i)
                    can_roll = k == "cq" and nq_sym[i] is not None and np.isfinite(e_nq) and np.isfinite(h_nq)
                    if can_roll and ((pos == 1 and e_nq > h_nq) or (pos == -1 and e_nq < -h_nq)):
                        held_sym, held_bars, reason[i] = nq_sym[i], 0, "ROLL"
                    else:
                        pos, held_sym, reason[i] = 0, None, "EXIT"
                elif held_bars >= min_hold_bars and (
                    (pos == 1 and e <= exit_level) or (pos == -1 and e >= -exit_level)
                ):
                    pos, held_sym, reason[i] = 0, None, "EXIT"
                else:
                    reason[i] = "HOLD"
        target[i] = pos
        contract[i] = held_sym
    return pd.DataFrame(
        {"target": target, "contract": contract, "excess": excess_out, "hurdle": hurdle_out, "reason": reason},
        index=p.index,
    )


# --- strategies --------------------------------------------------------------------------------
def cash_and_carry_targets(p: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """S1: long spot / short quarterly when the annualized excess basis clears the amortized cost hurdle."""
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["cash_and_carry"])
    return _contract_loop(
        p, "excess", cfg.costs.round_trip_frac(legs), cfg.params.cc_entry_buffer_ann,
        cfg.params.cc_exit_excess_ann, cfg.params.cc_roll_days, cfg.params.cc_hold_to_expiry,
        cfg.params.min_hold_bars, cfg.params.allow_reverse,
    )


def calendar_targets(p: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """S3: long perp / short quarterly when quarterly basis exceeds expected funding by the hurdle (reverse allowed)."""
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["calendar"])
    return _contract_loop(
        p, "calendar", cfg.costs.round_trip_frac(legs), cfg.params.cal_entry_buffer_ann,
        cfg.params.cal_exit_excess_ann, cfg.params.cal_roll_days, False,
        cfg.params.min_hold_bars, cfg.params.cal_allow_reverse,
    )


def funding_carry_targets(p: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """S2: long spot / short perp when trailing funding exceeds equilibrium funding by the hurdle."""
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["funding_carry"])
    rt = cfg.costs.round_trip_frac(legs)
    hurdle_value = fv.amortized_cost_ann(rt, cfg.params.fc_expected_hold_days) + cfg.params.fc_entry_buffer_ann
    excess = p["excess_funding_ann"].to_numpy(float)
    hurdle = np.full(len(p), hurdle_value)
    target, reason = _simple_loop(
        excess, hurdle, cfg.params.fc_exit_excess_ann, cfg.params.min_hold_bars, cfg.params.allow_reverse
    )
    return pd.DataFrame(
        {"target": target, "contract": None, "excess": excess, "hurdle": hurdle, "reason": reason}, index=p.index
    )


def random_maturity_targets(p: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """S4 (HMRW): long spot / short perp when the annualized premium deviation leaves the no-arbitrage band."""
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["random_maturity"])
    rt = cfg.costs.round_trip_frac(legs)
    _, upper = fv.no_arb_band_ann(rt, KAPPA)
    excess = p["rm_dev_ann"].to_numpy(float)
    hurdle = np.full(len(p), upper)
    target, reason = _simple_loop(
        excess, hurdle, cfg.params.rm_exit_dev_ann, cfg.params.min_hold_bars, cfg.params.allow_reverse
    )
    return pd.DataFrame(
        {"target": target, "contract": None, "excess": excess, "hurdle": hurdle, "reason": reason}, index=p.index
    )


STRATEGY_FUNCS = {
    "cash_and_carry": cash_and_carry_targets,
    "funding_carry": funding_carry_targets,
    "calendar": calendar_targets,
    "random_maturity": random_maturity_targets,
}


def build_targets(
    panel: pd.DataFrame, cfg: BacktestConfig, strategies: tuple[str, ...] | None = None
) -> dict[str, pd.DataFrame]:
    """Enrich the panel once and return {strategy: target frame}."""
    p = enrich(panel, cfg)
    out = {}
    for name in strategies or tuple(STRATEGY_FUNCS):
        out[name] = STRATEGY_FUNCS[name](p, cfg)
    return out
