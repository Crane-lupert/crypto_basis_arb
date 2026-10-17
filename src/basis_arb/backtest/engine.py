"""Event-driven 1h backtest engine (docs/strategy_spec.md section 6, spec v1.0.2).

One strategy at a time, equal coin quantity on both legs. Per bar, in this order:

1. Mark      every held leg at this bar's close (a futures leg without a price keeps its last mark).
2. Settle    a held quarterly with ``expiry <= ts`` at ``spot_close`` (delivery fee), regardless of target.
3. Funding   perp legs held *before* this bar's trades receive ``-qty * funding_rate * perp_close``.
4. Margin    worst adverse CLOSE move since entry per futures leg. Isolated mode liquidates a leg whose move
             exceeds ``1/leverage - maintenance`` at that bar's close (+ penalty) and closes the other leg.
             Spot-leg strategies use ``cfg.margin.mode``; futures-only strategies (S3) use
             ``cfg.margin.futures_pair_mode``. The intrabar high/low worst case is a diagnostic only.
5. Trade     move to the target (direction, contract) decided ``fill_lag_bars`` bars earlier, at
             ``close * (1 +/- slippage)`` plus fees; a roll swaps the quarterly leg only.
6. Resize    at a funding stamp, if |pair notional / target notional - 1| > 25%, both legs are traded back to
             the target notional (fees + slippage booked as the "resize" component; event RESIZE).
7. Capital   required capital re-measured at the bar's close: spot value + futures notional / leverage
             (target capital while flat). returns_t = P&L_t / capital_{t-1}.

Accounting
----------
equity = cash + sum(spot_qty * spot_mark) + sum(fut_qty * (mark - entry_price)), starting at the target
capital. P&L components (PER-BAR signed USDT, they add up to the equity change bar by bar):

* basis       mark-to-market of the legs at reference prices (close or settlement price)
* funding     funding cash flows
* fees        -(exchange fees + slippage) of entries, exits, rolls and liquidations
* delivery    -(delivery fees on settled quarterlies)
* liquidation -(liquidation penalties)
* resize      -(fees + slippage of resize trades)

Implementation choices the spec leaves open (also listed in the report's assumptions block):

* qty = notional_per_leg / close of the first leg (spot for S1/S2/S4, perpetual for S3); the pair notional
  used by the resize rule is |qty| x close of that first leg.
* A resize re-establishes the futures margin at target leverage, so the margin monitor's reference price
  resets to the resize price.
* A forced close (expiry settlement or liquidation) closes the remaining legs on the same bar at
  close +/- slippage and ends the round trip; the engine then stays flat until the target's
  (direction, contract) pair differs from the force-closed one, and never re-enters on that bar.
* The margin monitor ignores the entry bar (the fill happens at that bar's close).
* A futures leg whose contract has no price on a bar keeps its last mark (closing at it logs STALE_PRICE;
  a resize waits for a priced stamp); an entry or roll into an unpriced contract is skipped (NO_PRICE).
* A position still open on the last bar is marked to market, not closed (trades row exit_reason OPEN).
"""
from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from basis_arb.config import DELIVERY_HOUR_UTC, BacktestConfig
from basis_arb.model import signals
from basis_arb.types import STRATEGY_LEGS, Fill, LegPosition, validate_panel

log = logging.getLogger(__name__)

NAN = float("nan")
MARGIN_MODES = ("isolated", "cross_collateral")
RESIZE_THRESHOLD = 0.25  # spec section 6.6: resize when |pair notional / target - 1| > 25% at a funding stamp
PNL_COLUMNS = ("basis", "funding", "fees", "delivery", "liquidation", "resize")
TRADE_COLUMNS = (
    "entry_ts", "exit_ts", "direction", "contract", "entry_excess",
    "pnl_total", "pnl_basis", "pnl_funding", "pnl_costs", "hold_hours", "exit_reason",
    "n_rolls", "n_resizes", "qty", "cost_fees", "cost_slippage", "cost_delivery", "cost_liquidation", "cost_resize",
)
EVENT_COLUMNS = ("ts", "event", "leg", "instrument", "qty", "price", "amount", "detail")
FILL_COLUMNS = tuple(f.name for f in dataclasses.fields(Fill))
TARGET_EXIT_REASONS = ("EXIT", "EXPIRY", "KILL")
_NS_PER_HOUR = 3_600_000_000_000


@dataclass
class BacktestResult:
    """Output of :func:`run_backtest`. All series share the panel's hourly index."""

    strategy: str
    cfg: BacktestConfig
    equity: pd.Series  # USDT, end of bar (after trades), starts from the target capital
    capital: float  # target capital unit: spot notional + futures notional / leverage (spec section 6.7)
    returns_hourly: pd.Series  # P&L_t / capital_{t-1} (simple, non-compounding)
    pnl_components: pd.DataFrame  # PER-BAR signed USDT: PNL_COLUMNS
    fills: list[Fill]
    trades: pd.DataFrame  # one row per round trip (rolls and resizes stay inside the round trip)
    events: pd.DataFrame  # LIQUIDATION / EXPIRY / ROLL / RESIZE / STALE_PRICE / NO_PRICE
    margin_stats: dict
    position: pd.Series = field(default_factory=pd.Series)  # direction held at bar close (-1/0/+1)
    utilization: pd.Series = field(default_factory=pd.Series)  # adverse close move / liquidation move (max over legs)
    capital_t: pd.Series = field(default_factory=pd.Series)  # required capital at bar close (target when flat)
    r_usd: pd.Series = field(default_factory=pd.Series)  # panel risk-free rate (annual decimal), for excess metrics
    final_positions: list[LegPosition] = field(default_factory=list)  # legs still open on the last bar
    root: str = ""


class _Leg:
    __slots__ = (
        "leg", "instrument", "qty", "entry_exec", "entry_ref", "entry_bar", "expiry_ns",
        "peak_adverse", "mark", "stale", "liq_move",
    )

    def __init__(self, leg, instrument, qty, entry_exec, entry_ref, entry_bar, expiry_ns, liq_move):
        self.leg = leg
        self.instrument = instrument
        self.qty = qty
        self.entry_exec = entry_exec
        self.entry_ref = entry_ref
        self.entry_bar = entry_bar
        self.expiry_ns = expiry_ns
        self.peak_adverse = 0.0
        self.mark = entry_ref
        self.stale = False
        self.liq_move = liq_move


class _Trip:
    __slots__ = (
        "entry_bar", "direction", "contracts", "entry_excess", "qty",
        "basis", "funding", "fees", "slippage", "delivery", "liq", "resize", "n_rolls", "n_resizes",
    )

    def __init__(self, entry_bar, direction, contracts, entry_excess, qty):
        self.entry_bar = entry_bar
        self.direction = direction
        self.contracts = contracts
        self.entry_excess = entry_excess
        self.qty = qty
        self.basis = 0.0
        self.funding = 0.0
        self.fees = 0.0
        self.slippage = 0.0
        self.delivery = 0.0
        self.liq = 0.0
        self.resize = 0.0
        self.n_rolls = 0
        self.n_resizes = 0


# --- input preparation -------------------------------------------------------------------------
def _float_list(s: pd.Series) -> list[float]:
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype=float, na_value=np.nan).tolist()


def _symbol_list(s: pd.Series) -> list[str | None]:
    return [v if isinstance(v, str) and v else None for v in s.to_numpy(dtype=object)]


def _ns_list(s: pd.Series) -> list[int | None]:
    """Expiry column -> UTC epoch nanoseconds (None for NaT). Numeric columns are refused (unit ambiguity)."""
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_datetime64_any_dtype(s):
        raise ValueError(f"column {s.name} must hold datetimes, not numbers (unit would be a guess)")
    if pd.api.types.is_datetime64_any_dtype(s) and getattr(s.dt, "tz", None) is None:
        log.warning("column %s is tz-naive; interpreting it as UTC per the panel contract", s.name)
        dt = pd.DatetimeIndex(s).tz_localize("UTC")
    else:
        dt = pd.DatetimeIndex(pd.to_datetime(s, utc=True, errors="coerce"))
    ns = dt.as_unit("ns").asi8
    mask = dt.isna()
    return [None if m else int(v) for v, m in zip(ns.tolist(), mask.tolist())]


def _expiry_from_symbol(symbol: str) -> int | None:
    """ROOT_YYMMDD -> 08:00 UTC on that date (fallback when the panel's expiry cell is missing)."""
    suffix = symbol.rsplit("_", 1)[-1]
    if len(suffix) != 6 or not suffix.isdigit():
        return None
    try:
        ts = pd.Timestamp(
            year=2000 + int(suffix[:2]), month=int(suffix[2:4]), day=int(suffix[4:6]),
            hour=DELIVERY_HOUR_UTC, tz="UTC",
        )
    except ValueError:
        return None
    return int(ts.as_unit("ns").value)


def _infer_root(panel: pd.DataFrame) -> str:
    root = panel.attrs.get("root") if hasattr(panel, "attrs") else None
    if isinstance(root, str) and root:
        return root
    for col in ("fut_cq_symbol", "fut_nq_symbol"):
        for v in panel[col].to_numpy(dtype=object):
            if isinstance(v, str) and "_" in v:
                return v.split("_")[0]
    return "SPOT"


class PanelArrays:
    """The engine's per-panel inputs as Python lists. Build once with `prepare_panel` and pass to
    `run_backtest(prepared=...)` to reuse across runs on the same panel (the sensitivity grid does)."""

    def __init__(self, panel: pd.DataFrame):
        self.index = panel.index
        self.n = len(panel)
        self.ts_ns = panel.index.as_unit("ns").asi8.tolist()
        self.root = _infer_root(panel)
        self.s_c = _float_list(panel["spot_close"])
        self.p_c = _float_list(panel["perp_close"])
        self.p_h = _float_list(panel["perp_high"])
        self.p_l = _float_list(panel["perp_low"])
        self.fr = _float_list(panel["funding_rate"])
        self.r_usd = pd.to_numeric(panel["r_usd"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        self.cq_sym = _symbol_list(panel["fut_cq_symbol"])
        self.nq_sym = _symbol_list(panel["fut_nq_symbol"])
        self.cq_c = _float_list(panel["fut_cq_close"])
        self.cq_h = _float_list(panel["fut_cq_high"])
        self.cq_l = _float_list(panel["fut_cq_low"])
        self.nq_c = _float_list(panel["fut_nq_close"])
        self.nq_h = _float_list(panel["fut_nq_high"])
        self.nq_l = _float_list(panel["fut_nq_low"])
        self.cq_exp = _ns_list(panel["fut_cq_expiry"])
        self.nq_exp = _ns_list(panel["fut_nq_expiry"])


def prepare_panel(panel: pd.DataFrame) -> PanelArrays:
    """Validate the panel and convert it once for repeated `run_backtest` calls."""
    validate_panel(panel)
    return PanelArrays(panel)


def _shift(values: list, lag: int, fill) -> list:
    if lag <= 0:
        return values
    n = len(values)
    return [fill] * min(lag, n) + values[: max(n - lag, 0)]


def _target_arrays(targets: pd.DataFrame, panel_index: pd.DatetimeIndex, has_fut: bool, lag: int):
    """Target frame -> per-bar lists, shifted by `lag` bars (decision at t, fill at t + lag)."""
    for col in ("target", "contract"):
        if col not in targets.columns:
            raise ValueError(f"targets frame missing column {col!r}")
    if not targets.index.equals(panel_index):
        raise ValueError("targets index must equal the panel index (same timestamps, same order)")
    tv = pd.to_numeric(targets["target"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    if np.isnan(tv).any() or not np.isin(tv, (-1.0, 0.0, 1.0)).all():
        raise ValueError("targets['target'] must be -1, 0 or +1 on every row")
    tgt = tv.astype(np.int64).tolist()
    con: list[str | None] = [None] * len(tgt)
    if has_fut:
        raw = targets["contract"].to_numpy(dtype=object)
        for i in np.flatnonzero(tv != 0).tolist():
            v = raw[i]
            if not (isinstance(v, str) and v):
                raise ValueError(f"targets row {targets.index[i]}: non-zero target without a contract")
            con[i] = v
    if "excess" in targets.columns:
        exc = pd.to_numeric(targets["excess"], errors="coerce").to_numpy(dtype=float, na_value=np.nan).tolist()
    else:
        exc = [NAN] * len(tgt)
    rsn = targets["reason"].tolist() if "reason" in targets.columns else [""] * len(tgt)
    return _shift(tgt, lag, 0), _shift(con, lag, None), _shift(exc, lag, NAN), _shift(rsn, lag, "")


# --- engine ------------------------------------------------------------------------------------
class _Engine:
    def __init__(self, arrays: PanelArrays, strategy, cfg, tgt, con, exc, rsn, root):
        self.a = arrays
        self.strategy = strategy
        self.cfg = cfg
        self.idx = arrays.index
        self.n = arrays.n
        self.tgt, self.con, self.exc, self.rsn = tgt, con, exc, rsn
        self.legs_spec = STRATEGY_LEGS[strategy]
        names = [leg for leg, _ in self.legs_spec]
        self.has_fut = "fut" in names
        self.first_leg = names[0]
        self.root = root
        self.perp_name = f"{root}-PERP"

        costs, margin = cfg.costs, cfg.margin
        self.mode = margin.mode if "spot" in names else getattr(margin, "futures_pair_mode", margin.mode)
        if self.mode not in MARGIN_MODES:
            raise ValueError(f"margin mode must be one of {MARGIN_MODES}, got {self.mode!r}")
        self.isolated = self.mode == "isolated"
        self.notional = float(cfg.notional_per_leg)
        self.slip = costs.slippage_bps / 1e4
        self.fee_rate = {"spot": costs.spot_fee_bps / 1e4, "perp": costs.fut_fee_bps / 1e4,
                         "fut": costs.fut_fee_bps / 1e4}
        self.delivery_rate = costs.delivery_fee_bps / 1e4
        self.penalty_rate = costs.liquidation_penalty_bps / 1e4
        self.lev = {"perp": float(margin.perp_leverage), "fut": float(margin.fut_leverage)}
        self.liq_move = {}
        capital = 0.0
        for leg in names:
            if leg == "spot":
                capital += self.notional
                continue
            if not self.lev[leg] > 0:
                raise ValueError(f"{leg} leverage must be positive")
            capital += self.notional / self.lev[leg]
            move = margin.liquidation_move(leg)
            if not move > 0:
                raise ValueError(f"{leg} liquidation move 1/leverage - maintenance must be positive, got {move}")
            self.liq_move[leg] = move
        self.capital = capital

        self.cash = capital
        self.legs: dict[str, _Leg] = {}
        self.trip: _Trip | None = None
        self.fills: list[Fill] = []
        self.events: list[dict] = []
        self.trips: list[dict] = []
        self.i = 0
        self.b_basis = self.b_funding = self.b_costs = self.b_delivery = self.b_liq = self.b_resize = 0.0
        self.peak_util = 0.0
        self.peak_util_bar = -1
        self.peak_adverse = 0.0
        self.peak_intrabar_util = 0.0
        self.intrabar_breach_bars = 0
        self.peak_pair_util = 0.0
        self.n_liquidations = 0
        self.n_resizes = 0

    # -- quotes -----------------------------------------------------------------------------
    def _fut_close(self, inst, i):
        a = self.a
        if a.cq_sym[i] == inst:
            return a.cq_c[i]
        if a.nq_sym[i] == inst:
            return a.nq_c[i]
        return NAN

    def _fut_expiry(self, inst, i):
        a = self.a
        exp = None
        if a.cq_sym[i] == inst:
            exp = a.cq_exp[i]
        elif a.nq_sym[i] == inst:
            exp = a.nq_exp[i]
        if exp is None:
            exp = _expiry_from_symbol(inst)
            if exp is None:
                log.warning("%s: no expiry for %s at %s; it will not be settled", self.strategy, inst, self.idx[i])
        return exp

    # -- bookkeeping ------------------------------------------------------------------------
    def _event(self, event, leg, inst, qty, price, amount, detail):
        self.events.append({
            "ts": self.idx[self.i], "event": event, "leg": leg, "instrument": inst, "qty": qty,
            "price": price, "amount": amount, "detail": detail,
        })

    def _fill(self, leg, inst, qty_change, price, fee, reason):
        self.fills.append(Fill(
            ts=self.idx[self.i], strategy=self.strategy, leg=leg, instrument=inst,
            side="buy" if qty_change > 0 else "sell", qty=abs(qty_change), price=price, fee=fee, reason=reason,
        ))

    def _open(self, leg, inst, qty, ref, reason, expiry_ns):
        s = self.slip
        exec_px = ref * (1.0 + s) if qty > 0 else ref * (1.0 - s)
        aq = abs(qty)
        fee = aq * exec_px * self.fee_rate[leg]
        slip = aq * ref * s
        if leg == "spot":
            self.cash -= qty * exec_px
        self.cash -= fee
        self.b_costs += fee + slip
        self.trip.fees += fee
        self.trip.slippage += slip
        self.legs[leg] = _Leg(leg, inst, qty, exec_px, ref, self.i, expiry_ns, self.liq_move.get(leg, NAN))
        self._fill(leg, inst, qty, exec_px, fee, reason)
        return fee + slip

    def _close(self, leg, ref, reason, kind="trade"):
        """Close a leg at reference price `ref`. kind: trade | settle | liquidation."""
        lg = self.legs.pop(leg)
        q = lg.qty
        adj = q * (ref - lg.mark)  # move the mark to the exit reference price
        self.b_basis += adj
        self.trip.basis += adj
        lg.mark = ref
        aq = abs(q)
        penalty = 0.0
        if kind == "settle":
            exec_px, slip = ref, 0.0
            fee = aq * ref * self.delivery_rate
        else:
            s = self.slip
            exec_px = ref * (1.0 + s) if q < 0 else ref * (1.0 - s)
            fee = aq * exec_px * self.fee_rate[leg]
            slip = aq * ref * s
            if kind == "liquidation":
                penalty = aq * ref * self.penalty_rate
        if leg == "spot":
            self.cash += q * exec_px
        else:
            self.cash += q * (exec_px - lg.entry_exec)
        self.cash -= fee + penalty
        if kind == "settle":
            self.b_delivery += fee
            self.trip.delivery += fee
        else:
            self.b_costs += fee + slip
            self.trip.fees += fee
            self.trip.slippage += slip
        if penalty:
            self.b_liq += penalty
            self.trip.liq += penalty
        self._fill(leg, lg.instrument, -q, exec_px, fee + penalty, reason)
        return lg, fee + slip + penalty

    def _close_at_mark(self, leg, reason):
        lg = self.legs[leg]
        if lg.stale:
            self._event("STALE_PRICE", leg, lg.instrument, lg.qty, lg.mark, 0.0,
                        "no price on this bar; closed at the last valid mark")
        return self._close(leg, lg.mark, reason)

    def _finish_trip(self, reason):
        t = self.trip
        i = self.i
        ts_ns = self.a.ts_ns
        costs = t.fees + t.slippage + t.delivery + t.liq + t.resize
        self.trips.append({
            "entry_ts": self.idx[t.entry_bar],
            "exit_ts": self.idx[i],
            "direction": t.direction,
            "contract": ">".join(t.contracts) if t.contracts else None,
            "entry_excess": t.entry_excess,
            "pnl_total": t.basis + t.funding - costs,
            "pnl_basis": t.basis,
            "pnl_funding": t.funding,
            "pnl_costs": -costs,
            "hold_hours": (ts_ns[i] - ts_ns[t.entry_bar]) / _NS_PER_HOUR,
            "exit_reason": reason,
            "n_rolls": t.n_rolls,
            "n_resizes": t.n_resizes,
            "qty": t.qty,
            "cost_fees": t.fees,
            "cost_slippage": t.slippage,
            "cost_delivery": t.delivery,
            "cost_liquidation": t.liq,
            "cost_resize": t.resize,
        })
        self.trip = None

    # -- actions ----------------------------------------------------------------------------
    def _enter(self, d, c):
        i = self.i
        a = self.a
        plan = []
        for leg, sign in self.legs_spec:
            if leg == "spot":
                inst, ref = self.root, a.s_c[i]
            elif leg == "perp":
                inst, ref = self.perp_name, a.p_c[i]
            else:
                inst, ref = c, self._fut_close(c, i)
            if not (ref == ref and ref > 0):
                self._event("NO_PRICE", leg, inst, NAN, ref, 0.0, "entry skipped: no price on this bar")
                return False
            plan.append((leg, sign, inst, ref))
        qty = self.notional / plan[0][3]
        self.trip = _Trip(i, d, [c] if self.has_fut else [], float(self.exc[i]), qty)
        for leg, sign, inst, ref in plan:
            expiry = self._fut_expiry(inst, i) if leg == "fut" else None
            self._open(leg, inst, sign * d * qty, ref, "ENTER", expiry)
        return True

    def _roll(self, c_new):
        i = self.i
        ref_new = self._fut_close(c_new, i)
        if not (ref_new == ref_new and ref_new > 0):
            self._event("NO_PRICE", "fut", c_new, NAN, ref_new, 0.0, "roll skipped: no price on this bar")
            return False
        old, cost_old = self._close_at_mark("fut", "ROLL")
        cost_new = self._open("fut", c_new, old.qty, ref_new, "ROLL", self._fut_expiry(c_new, i))
        self.trip.contracts.append(c_new)
        self.trip.n_rolls += 1
        self._event("ROLL", "fut", f"{old.instrument}>{c_new}", old.qty, ref_new, cost_old + cost_new,
                    f"closed {old.instrument} at {old.mark:.6g}, opened {c_new} at {ref_new:.6g}")
        return True

    def _maybe_resize(self):
        """Spec 6.6: trade both legs back to the target notional when the pair notional drifted > 25%."""
        legs = self.legs
        if any(lg.stale for lg in legs.values()):
            return
        ref_first = legs[self.first_leg].mark
        ratio = abs(legs[self.first_leg].qty) * ref_first / self.notional
        if abs(ratio - 1.0) <= RESIZE_THRESHOLD:
            return
        q_abs = self.notional / ref_first
        s = self.slip
        cost = 0.0
        for lg in legs.values():
            m = lg.mark
            q0 = lg.qty
            q1 = math.copysign(q_abs, q0)
            dq = q1 - q0
            x = m * (1.0 + s) if dq > 0 else m * (1.0 - s)
            fee = abs(dq) * x * self.fee_rate[lg.leg]
            slip = abs(dq) * m * s
            if lg.leg == "spot":
                self.cash -= dq * x
            else:
                self.cash += q0 * (m - lg.entry_exec)  # realize the futures leg at the mark ...
                self.cash -= slip  # ... and book the traded part's slippage (new entry = mark)
            self.cash -= fee
            lg.qty, lg.entry_exec, lg.entry_ref, lg.peak_adverse = q1, m, m, 0.0  # margin re-established
            self._fill(lg.leg, lg.instrument, dq, x, fee, "RESIZE")
            cost += fee + slip
        self.b_resize += cost
        self.trip.resize += cost
        self.trip.n_resizes += 1
        self.n_resizes += 1
        self._event("RESIZE", self.first_leg, legs[self.first_leg].instrument, q_abs, ref_first, cost,
                    f"pair notional / target = {ratio:.3f}; both legs traded back to {self.notional:,.0f} USDT")

    def _exit_reason(self, i):
        r = self.rsn[i]
        return r if isinstance(r, str) and r in TARGET_EXIT_REASONS else "EXIT"

    # -- main loop --------------------------------------------------------------------------
    def run(self):
        a = self.a
        n = self.n
        legs = self.legs
        target_cap = self.capital
        equity = [0.0] * n
        pos_arr = [0] * n
        util_arr = [NAN] * n
        cap_arr = [target_cap] * n
        c_basis, c_fund, c_fees, c_deliv, c_liq, c_resize = ([0.0] * n for _ in range(6))
        tgt, con = self.tgt, self.con
        has_fut, isolated, lev = self.has_fut, self.isolated, self.lev
        pair_futures = not any(leg == "spot" for leg, _ in self.legs_spec)
        ts_ns, s_c, p_c, p_h, p_l, fr = a.ts_ns, a.s_c, a.p_c, a.p_h, a.p_l, a.fr
        cq_sym, nq_sym = a.cq_sym, a.nq_sym
        cq_c, cq_h, cq_l, nq_c, nq_h, nq_l = a.cq_c, a.cq_h, a.cq_l, a.nq_c, a.nq_h, a.nq_l
        cur_dir, cur_con, blocked = 0, None, None
        for i in range(n):
            if not legs and tgt[i] == 0:
                # flat and staying flat: nothing can happen on this bar (a pending block is released)
                blocked = None
                equity[i] = self.cash
                continue
            self.i = i
            self.b_basis = self.b_funding = self.b_costs = self.b_delivery = self.b_liq = self.b_resize = 0.0
            forced = None
            util_i = NAN
            if legs:
                # 1. mark
                bb = 0.0
                for lg in legs.values():
                    name = lg.leg
                    if name == "spot":
                        px = s_c[i]
                    elif name == "perp":
                        px = p_c[i]
                    else:
                        inst = lg.instrument
                        px = cq_c[i] if cq_sym[i] == inst else (nq_c[i] if nq_sym[i] == inst else NAN)
                    if px == px:
                        bb += lg.qty * (px - lg.mark)
                        lg.mark = px
                        lg.stale = False
                    else:
                        lg.stale = True
                self.b_basis = bb
                self.trip.basis += bb
                # 2. settle expiries (regardless of target)
                fl = legs.get("fut")
                if fl is not None and fl.expiry_ns is not None and ts_ns[i] >= fl.expiry_ns:
                    spot_px = s_c[i]
                    _, fee = self._close("fut", spot_px, "EXPIRY", kind="settle")
                    self._event("EXPIRY", "fut", fl.instrument, fl.qty, spot_px, fee,
                                "quarterly settled at spot_close (delivery fee charged)")
                    forced = "EXPIRY"
                # 3. funding on perp legs held before this bar's trades
                f = fr[i]
                if f == f:
                    pl = legs.get("perp")
                    if pl is not None and pl.entry_bar < i:
                        amt = -pl.qty * f * p_c[i]
                        self.cash += amt
                        self.b_funding += amt
                        self.trip.funding += amt
                # 4. margin monitor on close prices (intrabar high/low worst case = diagnostic only)
                breached = None
                intrabar_breach = False
                pair_loss = 0.0
                for lg in legs.values():
                    name = lg.leg
                    if name == "spot" or lg.entry_bar >= i:
                        continue
                    if name == "perp":
                        c, h, lo = p_c[i], p_h[i], p_l[i]
                    else:
                        inst = lg.instrument
                        if cq_sym[i] == inst:
                            c, h, lo = cq_c[i], cq_h[i], cq_l[i]
                        elif nq_sym[i] == inst:
                            c, h, lo = nq_c[i], nq_h[i], nq_l[i]
                        else:
                            continue
                    if c != c:
                        continue
                    ent = lg.entry_exec
                    if lg.qty < 0:
                        adverse = c / ent - 1.0
                        intra = (h if h == h and h > c else c) / ent - 1.0
                    else:
                        adverse = 1.0 - c / ent
                        intra = 1.0 - (lo if lo == lo and lo < c else c) / ent
                    move = lg.liq_move
                    pair_loss += abs(lg.qty) * ent * adverse
                    if adverse > lg.peak_adverse:
                        lg.peak_adverse = adverse
                        if adverse > self.peak_adverse:
                            self.peak_adverse = adverse
                    u = adverse / move if adverse > 0.0 else 0.0
                    if util_i != util_i or u > util_i:
                        util_i = u
                    if lg.peak_adverse / move > self.peak_util:
                        self.peak_util, self.peak_util_bar = lg.peak_adverse / move, i
                    if intra / move > self.peak_intrabar_util:
                        self.peak_intrabar_util = intra / move
                    if intra > move:
                        intrabar_breach = True
                    if isolated and lg.peak_adverse > move:
                        breached = (breached or []) + [name]
                if intrabar_breach:
                    self.intrabar_breach_bars += 1
                if pair_futures and pair_loss > 0.0:
                    posted = sum(abs(lg.qty) * lg.entry_exec / lev[lg.leg] for lg in legs.values())
                    if posted > 0 and pair_loss / posted > self.peak_pair_util:
                        self.peak_pair_util = pair_loss / posted
                if breached:
                    for name in breached:
                        lg = legs[name]
                        move = lg.liq_move
                        self._close(name, lg.mark, "LIQUIDATION", kind="liquidation")
                        self.n_liquidations += 1
                        self._event("LIQUIDATION", name, lg.instrument, lg.qty, lg.mark,
                                    abs(lg.qty) * lg.mark * self.penalty_rate,
                                    f"adverse close move {lg.peak_adverse:.4f} > {move:.4f} (1/leverage - maintenance)")
                    forced = "LIQUIDATION"
            # 5. trade
            if forced is not None:
                for name in list(legs):
                    self._close_at_mark(name, forced)
                self._finish_trip(forced)
                blocked = (cur_dir, cur_con)
                cur_dir, cur_con = 0, None
            else:
                d = tgt[i]
                c = con[i] if (d != 0 and has_fut) else None
                if blocked is not None and (d, c) != blocked:
                    blocked = None
                if blocked is None and (d != cur_dir or c != cur_con):
                    if cur_dir != 0 and d != cur_dir:
                        reason = self._exit_reason(i)
                        for name in list(legs):
                            self._close_at_mark(name, reason)
                        self._finish_trip(reason)
                        cur_dir, cur_con = 0, None
                    if d != 0:
                        if cur_dir == 0:
                            if self._enter(d, c):
                                cur_dir, cur_con = d, c
                        elif c != cur_con and self._roll(c):
                            cur_con = c
                # 6. resize at a funding stamp
                if legs and fr[i] == fr[i]:
                    self._maybe_resize()
            # 7. record end-of-bar state
            eq = self.cash
            cap = 0.0
            for lg in legs.values():
                if lg.leg == "spot":
                    eq += lg.qty * lg.mark
                    cap += abs(lg.qty) * lg.mark
                else:
                    eq += lg.qty * (lg.mark - lg.entry_exec)
                    cap += abs(lg.qty) * lg.mark / lev[lg.leg]
            equity[i] = eq
            if legs:
                cap_arr[i] = cap
            pos_arr[i] = cur_dir
            util_arr[i] = util_i
            c_basis[i] = self.b_basis
            c_fund[i] = self.b_funding
            c_fees[i] = -self.b_costs
            c_deliv[i] = -self.b_delivery
            c_liq[i] = -self.b_liq
            c_resize[i] = -self.b_resize
        if self.trip is not None:
            self.i = n - 1
            self._finish_trip("OPEN")
        comp = {"basis": c_basis, "funding": c_fund, "fees": c_fees, "delivery": c_deliv, "liquidation": c_liq,
                "resize": c_resize}
        return equity, pos_arr, util_arr, cap_arr, comp


def _margin_stats(eng: _Engine, utilization: np.ndarray) -> dict:
    margin = eng.cfg.margin
    held = utilization[np.isfinite(utilization)]
    pair = not any(leg == "spot" for leg, _ in eng.legs_spec)
    return {
        "mode": eng.mode,
        "perp_leverage": float(margin.perp_leverage),
        "fut_leverage": float(margin.fut_leverage),
        "maintenance_margin_frac": float(margin.maintenance_margin_frac),
        "liquidation_move": dict(eng.liq_move),
        "peak_utilization": float(eng.peak_util),
        "peak_utilization_ts": eng.idx[eng.peak_util_bar] if eng.peak_util_bar >= 0 else None,
        "peak_adverse_move": float(eng.peak_adverse),
        "mean_utilization_in_position": float(held.mean()) if len(held) else NAN,
        "bars_above_isolated_threshold": int((held > 1.0).sum()),
        "intrabar_breach_bars": int(eng.intrabar_breach_bars),  # diagnostic: high/low would have breached
        "peak_intrabar_utilization": float(eng.peak_intrabar_util),
        "peak_pair_utilization": float(eng.peak_pair_util) if pair else NAN,  # futures-pair net loss / posted margin
        "n_liquidations": int(eng.n_liquidations),
        "n_resizes": int(eng.n_resizes),
    }


def fills_frame(fills: list[Fill]) -> pd.DataFrame:
    """Fills as a DataFrame (column order = types.Fill fields)."""
    return pd.DataFrame([[getattr(f, c) for c in FILL_COLUMNS] for f in fills], columns=list(FILL_COLUMNS))


def run_backtest(
    panel: pd.DataFrame,
    strategy: str,
    cfg: BacktestConfig,
    targets: pd.DataFrame | None = None,
    *,
    root: str | None = None,
    prepared: PanelArrays | None = None,
) -> BacktestResult:
    """Run one strategy over a validated 1h panel. Pure function of (panel, cfg, targets).

    If `targets` is None they are built with ``signals.enrich`` + the strategy's target function. Targets are
    decisions; fills happen ``cfg.params.fill_lag_bars`` bars later. `root` (e.g. "BTCUSDT") only labels
    instruments. `prepared` (from :func:`prepare_panel` on this same panel) skips re-validation.
    """
    if strategy not in STRATEGY_LEGS:
        raise ValueError(f"unknown strategy {strategy!r}; expected one of {tuple(STRATEGY_LEGS)}")
    if prepared is None:
        prepared = prepare_panel(panel)
    elif prepared.n != len(panel) or not prepared.index.equals(panel.index):
        raise ValueError("`prepared` was built from a different panel")
    for mode in (cfg.margin.mode, getattr(cfg.margin, "futures_pair_mode", cfg.margin.mode)):
        if mode not in MARGIN_MODES:
            raise ValueError(f"margin mode must be one of {MARGIN_MODES}, got {mode!r}")
    if not (cfg.notional_per_leg > 0 and math.isfinite(cfg.notional_per_leg)):
        raise ValueError("notional_per_leg must be a positive finite number")
    lag = int(getattr(cfg.params, "fill_lag_bars", 0))
    if lag < 0:
        raise ValueError("fill_lag_bars must be >= 0")
    if targets is None:
        targets = signals.STRATEGY_FUNCS[strategy](signals.enrich(panel, cfg), cfg)
    has_fut = any(leg == "fut" for leg, _ in STRATEGY_LEGS[strategy])
    tgt, con, exc, rsn = _target_arrays(targets, prepared.index, has_fut, lag)
    root = root or prepared.root

    eng = _Engine(prepared, strategy, cfg, tgt, con, exc, rsn, root)
    equity_l, pos_l, util_l, cap_l, comp = eng.run()

    idx = prepared.index
    eq_arr = np.asarray(equity_l, dtype=float)
    cap_arr = np.asarray(cap_l, dtype=float)
    prev_eq = np.concatenate([[eng.capital], eq_arr[:-1]])
    prev_cap = np.concatenate([[eng.capital], cap_arr[:-1]])
    util_arr = np.asarray(util_l, dtype=float)
    final_positions = [
        LegPosition(
            leg=lg.leg, instrument=lg.instrument, qty=lg.qty, entry_price=lg.entry_exec,
            entry_ts=idx[lg.entry_bar],
            expiry=pd.Timestamp(lg.expiry_ns, tz="UTC") if lg.expiry_ns is not None else None,
            peak_adverse_move=lg.peak_adverse,
        )
        for lg in eng.legs.values()
    ]
    return BacktestResult(
        strategy=strategy, cfg=cfg,
        equity=pd.Series(eq_arr, index=idx, name="equity"),
        capital=eng.capital,
        returns_hourly=pd.Series((eq_arr - prev_eq) / prev_cap, index=idx, name="return"),
        pnl_components=pd.DataFrame({k: np.asarray(comp[k], dtype=float) for k in PNL_COLUMNS}, index=idx),
        fills=eng.fills,
        trades=pd.DataFrame(eng.trips, columns=list(TRADE_COLUMNS)),
        events=pd.DataFrame(eng.events, columns=list(EVENT_COLUMNS)),
        margin_stats=_margin_stats(eng, util_arr),
        position=pd.Series(np.asarray(pos_l, dtype=np.int8), index=idx, name="position"),
        utilization=pd.Series(util_arr, index=idx, name="utilization"),
        capital_t=pd.Series(cap_arr, index=idx, name="capital"),
        r_usd=pd.Series(prepared.r_usd, index=idx, name="r_usd"),
        final_positions=final_positions,
        root=root,
    )
