"""Pre-registered signal state machines for the four strategies.

Every strategy produces a *target* frame aligned to the panel index:
    target   int8   -1 / 0 / +1  (+1 = the carry direction listed in types.STRATEGY_LEGS)
    contract object  quarterly symbol held (cash_and_carry, calendar) or None
    excess   float   the annualized excess the decision was based on (on ROLL rows: the next contract's)
    hurdle   float   the annualized entry hurdle at that bar (on ROLL rows: the next contract's)
    reason   object  ENTER / HOLD / EXIT / ROLL / EXPIRY / FLAT

Decision rule (all strategies): enter when excess > hurdle (hurdle = amortized round-trip cost + buffer),
hold while excess > exit level, exit at or below the exit level once min_hold_bars have elapsed since
entry (a roll does not reset the clock). A NaN excess is "no decision": no entry, and an open position is
held. A held quarterly is keyed to the expiry recorded at entry: it expires only when ts >= that expiry,
and a data hole (missing symbol, price or dte on a later bar) is a HOLD, never an expiry or a roll.
Decisions at bar t use data up to and including bar t's close; the engine fills at bar t's close
(or at bar t + fill_lag_bars when that sensitivity axis is used).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from basis_arb.config import DAYS_PER_YEAR, FUNDING_INTERVAL_HOURS, KAPPA, BacktestConfig
from basis_arb.model import fair_value as fv
from basis_arb.types import STRATEGY_LEGS, validate_panel

TARGET_COLUMNS = ("target", "contract", "excess", "hurdle", "reason")
FUNDING_FFILL_LIMIT_BARS = 9  # one 8h interval + 1h slack: a stalled funding feed turns f_hat into NaN (no decision)
DECISION_COLUMNS = (
    "excess_cq_ann", "excess_nq_ann", "excess_funding_ann", "rm_dev_ann", "calendar_cq_ann", "calendar_nq_ann"
)
_NS_PER_DAY = 86_400_000_000_000
_NAT_NS = np.datetime64("NaT").astype("datetime64[ns]").astype(np.int64)


def _is_sym(x) -> bool:
    return isinstance(x, str) and x != ""


def _expiry_ns(series: pd.Series) -> np.ndarray:
    """tz-aware expiry column -> int64 ns since epoch (NaT -> _NAT_NS)."""
    return series.dt.tz_convert("UTC").astype("int64").to_numpy()


# --- enrichment --------------------------------------------------------------------------------
def enrich(panel: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """Append the model columns to a validated panel (pure; returns a copy)."""
    validate_panel(panel)
    p = panel.copy()
    r = p["r_usd"].astype(float).to_numpy()
    y = p["y_coin"].astype(float).to_numpy()
    spot = p["spot_close"].to_numpy(float)
    perp = p["perp_close"].to_numpy(float)

    # dated futures: market vs cost-of-carry
    for k in ("cq", "nq"):
        tau = p[f"fut_{k}_dte_days"].astype(float).to_numpy() / DAYS_PER_YEAR
        fut = p[f"fut_{k}_close"].astype(float).to_numpy()
        p[f"basis_{k}_ann"] = fv.annualized_basis(fut, spot, tau)
        p[f"basis_vs_perp_{k}_ann"] = fv.annualized_basis(fut, perp, tau)
    p["fair_basis_ann"] = fv.fair_basis_ann(r, y)
    for k in ("cq", "nq"):
        p[f"excess_{k}_ann"] = p[f"basis_{k}_ann"] - p["fair_basis_ann"]

    # perpetual: trailing funding = sum of prints over a calendar window, annualized (cadence-proof)
    stamps = p["funding_rate"].dropna()
    window_days = cfg.params.fc_lookback_prints * FUNDING_INTERVAL_HOURS / 24.0
    trailing = fv.trailing_funding_ann_window(stamps, window_days)
    p["funding_trailing_ann"] = trailing.reindex(p.index).ffill(limit=FUNDING_FFILL_LIMIT_BARS)
    p["fair_funding_ann"] = fv.fair_funding_ann(r, y, KAPPA)
    p["excess_funding_ann"] = p["funding_trailing_ann"] - p["fair_funding_ann"]

    # perpetual: instantaneous premium vs HMRW fair premium
    p["perp_prem_ann"] = fv.perp_premium_ann(perp, spot, KAPPA)
    p["fair_perp_prem_ann"] = fv.fair_perp_premium_ann(r, y, KAPPA)
    p["rm_dev_ann"] = p["perp_prem_ann"] - p["fair_perp_prem_ann"]

    # calendar: quarterly-vs-perpetual spread (the spread actually traded) vs the trailing funding forecast
    for k in ("cq", "nq"):
        p[f"calendar_{k}_ann"] = fv.calendar_excess_ann(
            p[f"basis_vs_perp_{k}_ann"].to_numpy(), p["funding_trailing_ann"].to_numpy()
        )

    # exchange-outage rows (spot or perp bar missing, close forward-filled, high/low NaN): no decision.
    # A frozen spot against a moving perp manufactures premiums of up to ~2% that are not tradable.
    stale = p["spot_high"].isna() | p["perp_high"].isna()
    p["stale_bar"] = stale
    for col in DECISION_COLUMNS:
        p.loc[stale, col] = np.nan
    return p


# --- generic loops -----------------------------------------------------------------------------
def _simple_loop(
    excess: np.ndarray,
    hurdle: np.ndarray,
    exit_level: float,
    min_hold_bars: int,
    allow_reverse: bool,
    hurdle_lo: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Hysteresis loop for strategies without a contract leg.

    Enter +1 when excess > hurdle; enter -1 (if allowed) when excess < hurdle_lo (default -hurdle).
    Exit +1 when excess <= exit_level, exit -1 when excess >= -exit_level, both only after min_hold_bars.
    NaN excess: no entry; an open position is held (no decision).
    """
    n = len(excess)
    hi = np.asarray(hurdle, float)
    lo = -hi if hurdle_lo is None else np.asarray(hurdle_lo, float)
    target = np.zeros(n, dtype=np.int8)
    reason = np.full(n, "FLAT", dtype=object)
    pos, held = 0, 0
    for i in range(n):
        e = excess[i]
        if pos == 0:
            if np.isfinite(e) and np.isfinite(hi[i]) and e > hi[i]:
                pos, held, reason[i] = 1, 0, "ENTER"
            elif allow_reverse and np.isfinite(e) and np.isfinite(lo[i]) and e < lo[i]:
                pos, held, reason[i] = -1, 0, "ENTER"
        else:
            held += 1
            if not np.isfinite(e):
                reason[i] = "HOLD"
            elif held >= min_hold_bars and ((pos == 1 and e <= exit_level) or (pos == -1 and e >= -exit_level)):
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
    """Shared state machine for strategies whose futures leg is a specific quarterly contract.

    The held contract is identified by symbol and keyed to the expiry recorded at entry. Days to expiry of
    the held contract are computed from that expiry (not from the panel column), so a data hole can never
    produce a phantom expiry or roll.
    """
    n = len(p)
    ts_ns = p.index.asi8
    sym = {"cq": p["fut_cq_symbol"].to_numpy(dtype=object), "nq": p["fut_nq_symbol"].to_numpy(dtype=object)}
    ex = {"cq": p[f"{excess_prefix}_cq_ann"].to_numpy(float), "nq": p[f"{excess_prefix}_nq_ann"].to_numpy(float)}
    dte = {"cq": p["fut_cq_dte_days"].to_numpy(float), "nq": p["fut_nq_dte_days"].to_numpy(float)}
    exp = {"cq": _expiry_ns(p["fut_cq_expiry"]), "nq": _expiry_ns(p["fut_nq_expiry"])}

    def hurdle_for(days: float) -> float:
        if not np.isfinite(days) or days <= 0:
            return np.nan
        return fv.amortized_cost_ann(round_trip_frac, days) + buffer_ann

    target = np.zeros(n, dtype=np.int8)
    contract = np.full(n, None, dtype=object)
    excess_out = np.full(n, np.nan)
    hurdle_out = np.full(n, np.nan)
    reason = np.full(n, "FLAT", dtype=object)
    pos, held_sym, held_exp, held_bars = 0, None, _NAT_NS, 0

    for i in range(n):
        if pos == 0:
            # candidate: current quarter unless it is inside the roll window (or absent), else next quarter
            k = "cq" if (_is_sym(sym["cq"][i]) and np.isfinite(dte["cq"][i]) and dte["cq"][i] >= roll_days) else "nq"
            h, e = hurdle_for(dte[k][i]), ex[k][i]
            excess_out[i], hurdle_out[i] = e, h
            if _is_sym(sym[k][i]) and exp[k][i] != _NAT_NS and np.isfinite(e) and np.isfinite(h):
                if e > h:
                    pos, held_sym, held_exp, held_bars, reason[i] = 1, sym[k][i], exp[k][i], 0, "ENTER"
                elif allow_reverse and e < -h:
                    pos, held_sym, held_exp, held_bars, reason[i] = -1, sym[k][i], exp[k][i], 0, "ENTER"
        else:
            held_bars += 1
            if ts_ns[i] >= held_exp:
                # the held contract has settled at its expiry: the engine books the delivery
                pos, held_sym, held_exp, reason[i] = 0, None, _NAT_NS, "EXPIRY"
            else:
                if _is_sym(sym["cq"][i]) and held_sym == sym["cq"][i]:
                    k = "cq"
                elif _is_sym(sym["nq"][i]) and held_sym == sym["nq"][i]:
                    k = "nq"
                else:
                    k = None  # data hole on the held contract: no decision
                days_left = (held_exp - ts_ns[i]) / _NS_PER_DAY
                e = ex[k][i] if k is not None else np.nan
                excess_out[i], hurdle_out[i] = e, hurdle_for(days_left)
                in_roll_window = days_left < roll_days
                if in_roll_window and hold_to_expiry:
                    reason[i] = "HOLD"  # carried into delivery; no noise exits in the final hours
                elif in_roll_window:
                    e_nq, h_nq = ex["nq"][i], hurdle_for(dte["nq"][i])
                    can_roll = (
                        _is_sym(sym["nq"][i])
                        and sym["nq"][i] != held_sym
                        and exp["nq"][i] != _NAT_NS
                        and exp["nq"][i] > held_exp
                        and np.isfinite(e_nq)
                        and np.isfinite(h_nq)
                    )
                    if can_roll and ((pos == 1 and e_nq > h_nq) or (pos == -1 and e_nq < -h_nq)):
                        held_sym, held_exp, reason[i] = sym["nq"][i], exp["nq"][i], "ROLL"  # min-hold clock keeps counting
                        excess_out[i], hurdle_out[i] = e_nq, h_nq
                    else:
                        pos, held_sym, held_exp, reason[i] = 0, None, _NAT_NS, "EXIT"
                elif not np.isfinite(e):
                    reason[i] = "HOLD"  # no decision
                elif held_bars >= min_hold_bars and (
                    (pos == 1 and e <= exit_level) or (pos == -1 and e >= -exit_level)
                ):
                    pos, held_sym, held_exp, reason[i] = 0, None, _NAT_NS, "EXIT"
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
    """S1: long spot / short quarterly when the annualized excess basis clears the amortized cost hurdle.

    No reverse trades: a short-spot leg needs a borrow-cost model that v1 does not have.
    """
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["cash_and_carry"])
    return _contract_loop(
        p, "excess", cfg.costs.round_trip_frac(legs), cfg.params.cc_entry_buffer_ann,
        cfg.params.cc_exit_excess_ann, cfg.params.cc_roll_days, cfg.params.cc_hold_to_expiry,
        cfg.params.min_hold_bars, allow_reverse=False,
    )


def calendar_targets(p: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """S3: long perp / short quarterly when the quarterly-vs-perp spread exceeds the trailing funding forecast
    by the hurdle (reverse allowed: both legs are futures)."""
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["calendar"])
    return _contract_loop(
        p, "calendar", cfg.costs.round_trip_frac(legs), cfg.params.cal_entry_buffer_ann,
        cfg.params.cal_exit_excess_ann, cfg.params.cal_roll_days, cfg.params.cal_hold_to_expiry,
        cfg.params.min_hold_bars, cfg.params.cal_allow_reverse,
    )


def funding_carry_targets(p: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """S2: long spot / short perp when trailing funding exceeds equilibrium funding by the hurdle (no reverse)."""
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["funding_carry"])
    rt = cfg.costs.round_trip_frac(legs)
    hurdle_value = fv.amortized_cost_ann(rt, cfg.params.fc_expected_hold_days) + cfg.params.fc_entry_buffer_ann
    excess = p["excess_funding_ann"].to_numpy(float)
    hurdle = np.full(len(p), hurdle_value)
    target, reason = _simple_loop(excess, hurdle, cfg.params.fc_exit_excess_ann, cfg.params.min_hold_bars, False)
    return pd.DataFrame(
        {"target": target, "contract": None, "excess": excess, "hurdle": hurdle, "reason": reason}, index=p.index
    )


def random_maturity_targets(p: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """S4 (HMRW): long spot / short perp when the annualized premium deviation leaves the no-arbitrage band.

    The band is asymmetric (kappa*ln(1 - C) < -kappa*ln(1 + C)); only the upper edge is used because the
    reverse trade would need a short-spot leg.
    """
    legs = tuple(leg for leg, _ in STRATEGY_LEGS["random_maturity"])
    rt = cfg.costs.round_trip_frac(legs)
    lower, upper = fv.no_arb_band_ann(rt, KAPPA)
    excess = p["rm_dev_ann"].to_numpy(float)
    hurdle = np.full(len(p), upper)
    target, reason = _simple_loop(
        excess, hurdle, cfg.params.rm_exit_dev_ann, cfg.params.min_hold_bars, False, hurdle_lo=np.full(len(p), lower)
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
