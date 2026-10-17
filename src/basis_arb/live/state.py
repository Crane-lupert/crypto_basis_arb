"""Live book state, atomic persistence and the append-only event ledger (strategy_spec.md section 9).

Files, per underlying AND per mode, under the state directory (default ./state, gitignored), so paper and
testnet never share a book or a ledger:
    state/{ROOT}/{mode}/book.json      the Book (positions, cash, P&L components, counters); replaced atomically
    state/{ROOT}/{mode}/events.jsonl   append-only ledger, one JSON object per line, UTC ISO timestamps
    state/{ROOT}/{mode}/daemon.lock    OS-level exclusive lock: one daemon per book

Accounting (same conventions as the backtest, strategy_spec.md section 6):
    equity = cash + sum(spot_qty * spot_mark) + sum(fut_qty * (fut_mark - entry_price))
    spot fills move cash by -signed_qty * price; futures fills move cash only by fees and realized P&L.
    P&L components per strategy: funding (+ = received), fees (+ = paid, incl. delivery), slippage
    (+ = cost of crossing the spread vs mid, incl. modeled slippage), penalties (+ = paid, paper liquidation
    penalty on a margin breach), basis = total - funding + fees + slippage + penalties (the mid-to-mid
    convergence of the legs).
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

from basis_arb.config import BacktestConfig
from basis_arb.types import STRATEGY_LEGS, Fill, LegPosition

SCHEMA_VERSION = 1
EVENT_KINDS: tuple[str, ...] = (
    "HEARTBEAT", "SNAPSHOT", "DECISION", "FILL", "FUNDING_ACCRUAL", "EXPIRY", "KILL", "SAFE", "HALT", "ERROR",
)
QTY_EPS = 1e-12


# --- time + JSON helpers (UTC everywhere) ------------------------------------------------------
def utc(ts: Any = None) -> pd.Timestamp:
    """tz-aware UTC Timestamp; naive input is interpreted as UTC; None means now."""
    t = pd.Timestamp.now(tz="UTC") if ts is None else pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def to_ms(ts: Any) -> int:
    return int(utc(ts).value // 1_000_000)


def from_ms(ms: float) -> pd.Timestamp:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC")


def iso(ts: Any) -> str | None:
    if ts is None or ts is pd.NaT:
        return None
    return utc(ts).isoformat()


def parse_ts(value: Any) -> pd.Timestamp | None:
    if value is None or value == "" or value is pd.NaT:
        return None
    return utc(value)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert to strict-JSON types: NaN/inf -> None, timestamps -> UTC ISO strings."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if obj is pd.NaT:
        return None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (datetime, np.datetime64)):
        return iso(obj)
    if isinstance(obj, pd.Timedelta):
        return obj.total_seconds()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [to_jsonable(v) for v in obj.tolist()]
    return str(obj)


def _finite(x: Any) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


# --- capital ----------------------------------------------------------------------------------
def strategy_capital(name: str, cfg: BacktestConfig) -> float:
    """Capital base per strategy (strategy_spec.md section 6): spot-leg strategies notional x (1 + 1/leverage);
    futures-only (calendar) notional/lev_perp + notional/lev_fut (= 2 x notional / leverage)."""
    n, m = cfg.notional_per_leg, cfg.margin
    legs = [leg for leg, _ in STRATEGY_LEGS[name]]
    if "spot" in legs:
        other = next(leg for leg in legs if leg != "spot")
        lev = m.perp_leverage if other == "perp" else m.fut_leverage
        return n * (1.0 + 1.0 / lev)
    return n / m.perp_leverage + n / m.fut_leverage


# --- positions --------------------------------------------------------------------------------
def _pos_to_dict(p: LegPosition) -> dict:
    return {
        "leg": p.leg, "instrument": p.instrument, "qty": p.qty, "entry_price": p.entry_price,
        "entry_ts": iso(p.entry_ts), "expiry": iso(p.expiry), "peak_adverse_move": p.peak_adverse_move,
    }


def _pos_from_dict(d: dict) -> LegPosition:
    return LegPosition(
        leg=d["leg"], instrument=d["instrument"], qty=float(d["qty"]), entry_price=float(d["entry_price"]),
        entry_ts=parse_ts(d["entry_ts"]), expiry=parse_ts(d.get("expiry")),
        peak_adverse_move=float(d.get("peak_adverse_move") or 0.0),
    )


@dataclass
class StrategyBook:
    name: str
    capital: float  # equity_0 of this strategy
    cash: float
    target: int = 0  # ACTUAL position direction derived from the legs (+1 carry, -1 reverse, 0 flat)
    contract: str | None = None  # quarterly held (cash_and_carry, calendar)
    positions: list[LegPosition] = field(default_factory=list)
    funding: float = 0.0  # cumulative funding, + = received
    fees: float = 0.0  # cumulative exchange fees, + = paid (incl. delivery fees)
    slippage: float = 0.0  # cumulative execution cost vs mid, + = cost
    penalties: float = 0.0  # cumulative liquidation penalties (paper, margin breach), + = paid
    n_fills: int = 0
    n_entries: int = 0
    n_exits: int = 0
    n_rolls: int = 0
    n_expiries: int = 0
    n_funding: int = 0
    signal_target: int = 0  # last ADOPTED decision from model.signals (desired state)
    signal_contract: str | None = None
    signal_reason: str | None = None
    last_trade_ts: pd.Timestamp | None = None

    def position(self, instrument: str) -> LegPosition | None:
        return next((p for p in self.positions if p.instrument == instrument), None)

    def equity(self, marks: dict[str, float]) -> tuple[float, list[str]]:
        eq, missing = self.cash, []
        for p in self.positions:
            m = marks.get(p.instrument)
            if not _finite(m):
                missing.append(p.instrument)
                m = p.entry_price  # no mark at all: zero unrealized (flagged in missing_marks)
            eq += p.qty * m if p.leg == "spot" else p.qty * (m - p.entry_price)
        return eq, missing

    def derive_state(self) -> None:
        """Set target/contract from the legs actually held."""
        if not self.positions:
            self.target, self.contract = 0, None
            return
        signs = dict(STRATEGY_LEGS[self.name])
        first_leg = STRATEGY_LEGS[self.name][0][0]
        ref = next((p for p in self.positions if p.leg == first_leg), self.positions[0])
        self.target = int(np.sign(ref.qty) * signs.get(ref.leg, 1))
        fut = next((p for p in self.positions if p.leg == "fut"), None)
        self.contract = fut.instrument if fut else None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "capital": self.capital, "cash": self.cash, "target": self.target,
            "contract": self.contract, "positions": [_pos_to_dict(p) for p in self.positions],
            "funding": self.funding, "fees": self.fees, "slippage": self.slippage, "penalties": self.penalties,
            "n_fills": self.n_fills, "n_entries": self.n_entries, "n_exits": self.n_exits,
            "n_rolls": self.n_rolls, "n_expiries": self.n_expiries, "n_funding": self.n_funding,
            "signal_target": self.signal_target, "signal_contract": self.signal_contract,
            "signal_reason": self.signal_reason, "last_trade_ts": iso(self.last_trade_ts),
        }

    @classmethod
    def from_dict(cls, d: dict) -> StrategyBook:
        return cls(
            name=d["name"], capital=float(d["capital"]), cash=float(d["cash"]), target=int(d.get("target", 0)),
            contract=d.get("contract"), positions=[_pos_from_dict(p) for p in d.get("positions", [])],
            funding=float(d.get("funding", 0.0)), fees=float(d.get("fees", 0.0)),
            slippage=float(d.get("slippage", 0.0)), penalties=float(d.get("penalties", 0.0)),
            n_fills=int(d.get("n_fills", 0)),
            n_entries=int(d.get("n_entries", 0)), n_exits=int(d.get("n_exits", 0)),
            n_rolls=int(d.get("n_rolls", 0)), n_expiries=int(d.get("n_expiries", 0)),
            n_funding=int(d.get("n_funding", 0)), signal_target=int(d.get("signal_target", 0)),
            signal_contract=d.get("signal_contract"), signal_reason=d.get("signal_reason"),
            last_trade_ts=parse_ts(d.get("last_trade_ts")),
        )


def _default_counters() -> dict[str, int]:
    return {"loops": 0, "fills": 0, "funding_events": 0, "expiries": 0, "safe_loops": 0, "errors": 0, "kills": 0}


@dataclass
class Book:
    root: str
    mode: str  # "paper" | "testnet"
    created_ts: pd.Timestamp
    strategies: dict[str, StrategyBook]
    funding_cursor_ms: int  # funding stamps <= cursor (hour-floored, ms) have been accrued
    next_funding_ms: int | None = None  # nextFundingTime seen on the previous loop
    funding_due_ms: int | None = None  # a crossed stamp whose settled rate is not yet accrued
    closed_perp: list[dict] = field(default_factory=list)  # closed perp legs still eligible for a pending stamp
    marks: dict[str, float] = field(default_factory=dict)  # last known mid per instrument
    last_decision_bar: pd.Timestamp | None = None
    last_loop_ts: pd.Timestamp | None = None
    last_equity: float | None = None
    halted: dict | None = None
    counters: dict[str, int] = field(default_factory=_default_counters)
    schema_version: int = SCHEMA_VERSION

    # -- construction --------------------------------------------------------------------------
    @classmethod
    def new(cls, root: str, mode: str, strategies: tuple[str, ...], cfg: BacktestConfig, now: Any) -> Book:
        sbs = {}
        for name in strategies:
            cap = strategy_capital(name, cfg)
            sbs[name] = StrategyBook(name=name, capital=cap, cash=cap)
        return cls(root=root, mode=mode, created_ts=utc(now), strategies=sbs, funding_cursor_ms=to_ms(now))

    @property
    def equity_0(self) -> float:
        return float(sum(s.capital for s in self.strategies.values()))

    @property
    def cash(self) -> float:
        return float(sum(s.cash for s in self.strategies.values()))

    # -- accounting ----------------------------------------------------------------------------
    def apply_fill(self, fill: Fill, mid: float | None = None, expiry: Any = None) -> dict:
        """Book one fill: open, add to, reduce, close or flip the strategy's position in that instrument."""
        if not (_finite(fill.qty) and fill.qty > 0 and _finite(fill.price) and fill.price > 0 and _finite(fill.fee)):
            raise ValueError(f"invalid fill: {fill}")
        s = self.strategies[fill.strategy]
        signed = fill.qty if fill.side == "buy" else -fill.qty
        s.cash -= fill.fee
        s.fees += fill.fee
        slip = signed * (fill.price - mid) if _finite(mid) else 0.0
        s.slippage += slip
        if fill.leg == "spot":
            s.cash -= signed * fill.price
        realized = 0.0
        pos = s.position(fill.instrument)
        exp = parse_ts(expiry)
        if pos is None:
            s.positions.append(LegPosition(fill.leg, fill.instrument, signed, fill.price, utc(fill.ts), exp))
        elif np.sign(pos.qty) == np.sign(signed):
            new_qty = pos.qty + signed
            pos.entry_price = (pos.entry_price * abs(pos.qty) + fill.price * abs(signed)) / abs(new_qty)
            pos.qty = new_qty
        else:
            closed = math.copysign(min(abs(signed), abs(pos.qty)), pos.qty)
            realized = closed * (fill.price - pos.entry_price)
            if fill.leg != "spot":
                s.cash += realized  # spot P&L is already in the cash flows
            if fill.leg == "perp":
                self.closed_perp.append({
                    "strategy": fill.strategy, "instrument": fill.instrument, "qty": closed,
                    "entry_ts": iso(pos.entry_ts), "exit_ts": iso(fill.ts),
                })
            remaining = pos.qty + signed
            if abs(remaining) <= QTY_EPS:
                s.positions.remove(pos)
            elif np.sign(remaining) == np.sign(pos.qty):
                pos.qty = remaining
            else:
                s.positions.remove(pos)
                s.positions.append(LegPosition(fill.leg, fill.instrument, remaining, fill.price, utc(fill.ts), exp))
        s.n_fills += 1
        s.last_trade_ts = utc(fill.ts)
        self.counters["fills"] = self.counters.get("fills", 0) + 1
        s.derive_state()
        return {"realized": realized, "slippage_cost": slip}

    def apply_funding(self, strategy: str, amount: float) -> None:
        s = self.strategies[strategy]
        s.cash += amount
        s.funding += amount
        s.n_funding += 1
        self.counters["funding_events"] = self.counters.get("funding_events", 0) + 1

    def apply_penalty(self, strategy: str, amount: float) -> None:
        s = self.strategies[strategy]
        s.cash -= amount
        s.penalties += amount

    def perp_exposure_at(self, stamp: Any) -> list[dict]:
        """Perp legs that were held at `stamp`: opened strictly before it and not closed before it."""
        st = utc(stamp)
        out = []
        for name, s in self.strategies.items():
            for p in s.positions:
                if p.leg == "perp" and utc(p.entry_ts) < st:
                    out.append({"strategy": name, "instrument": p.instrument, "qty": p.qty, "closed": False})
        for rec in self.closed_perp:
            if utc(rec["entry_ts"]) < st <= utc(rec["exit_ts"]):
                out.append({"strategy": rec["strategy"], "instrument": rec["instrument"], "qty": rec["qty"],
                            "closed": True})
        return out

    def prune_closed_perp(self) -> None:
        """Drop closed perp legs that no stamp after the funding cursor can reach (stamps are hour-aligned,
        so a leg closed at exit_ts can only receive stamps s <= floor_hour(exit_ts))."""
        cursor = from_ms(self.funding_cursor_ms)
        self.closed_perp = [r for r in self.closed_perp if utc(r["exit_ts"]).floor("h") > cursor]

    def settle_expiry(self, strategy: str, instrument: str, price: float, fee: float, ts: Any) -> dict:
        s = self.strategies[strategy]
        pos = s.position(instrument)
        if pos is None:
            raise KeyError(f"{strategy} holds no {instrument}")
        realized = pos.qty * (price - pos.entry_price)
        s.cash += realized - fee
        s.fees += fee
        s.positions.remove(pos)
        s.n_expiries += 1
        s.last_trade_ts = utc(ts)
        self.counters["expiries"] = self.counters.get("expiries", 0) + 1
        s.derive_state()
        return {"qty": pos.qty, "entry_price": pos.entry_price, "settle_price": price, "realized": realized,
                "delivery_fee": fee}

    def mark_to_market(self, prices: dict[str, float]) -> dict:
        """Update last-known marks and return equity, drawdown vs equity_0 and P&L components per strategy."""
        for k, v in prices.items():
            if _finite(v) and float(v) > 0:
                self.marks[k] = float(v)
        out: dict[str, Any] = {"strategies": {}, "missing_marks": []}
        total = 0.0
        for name, s in self.strategies.items():
            eq, missing = s.equity(self.marks)
            total += eq
            out["missing_marks"].extend(missing)
            pnl_total = eq - s.capital
            out["strategies"][name] = {
                "equity": eq, "target": s.target, "contract": s.contract,
                "legs": [{"leg": p.leg, "instrument": p.instrument, "qty": p.qty, "entry_price": p.entry_price}
                         for p in s.positions],
                "pnl": {"total": pnl_total, "basis": pnl_total - s.funding + s.fees + s.slippage + s.penalties,
                        "funding": s.funding, "fees": s.fees, "slippage": s.slippage, "penalties": s.penalties},
            }
        eq0 = self.equity_0
        out["equity"] = total
        out["equity_0"] = eq0
        out["drawdown"] = (eq0 - total) / eq0 if eq0 > 0 else 0.0
        self.last_equity = total
        return out

    # -- persistence ---------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version, "root": self.root, "mode": self.mode,
            "created_ts": iso(self.created_ts), "equity_0": self.equity_0, "cash": self.cash,
            "last_equity": self.last_equity, "halted": self.halted,
            "funding_cursor_ms": self.funding_cursor_ms, "next_funding_ms": self.next_funding_ms,
            "funding_due_ms": self.funding_due_ms, "closed_perp": self.closed_perp, "marks": self.marks,
            "last_decision_bar": iso(self.last_decision_bar), "last_loop_ts": iso(self.last_loop_ts),
            "counters": self.counters,
            "strategies": {k: v.to_dict() for k, v in self.strategies.items()},
        }

    def to_json(self) -> str:
        return json.dumps(to_jsonable(self.to_dict()), indent=2, allow_nan=False)

    @classmethod
    def from_dict(cls, d: dict) -> Book:
        if int(d.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError(f"unsupported book schema_version {d.get('schema_version')!r}")
        counters = _default_counters()
        counters.update({k: int(v) for k, v in (d.get("counters") or {}).items()})
        return cls(
            root=d["root"], mode=d["mode"], created_ts=parse_ts(d["created_ts"]),
            strategies={k: StrategyBook.from_dict(v) for k, v in d["strategies"].items()},
            funding_cursor_ms=int(d["funding_cursor_ms"]),
            next_funding_ms=None if d.get("next_funding_ms") is None else int(d["next_funding_ms"]),
            funding_due_ms=None if d.get("funding_due_ms") is None else int(d["funding_due_ms"]),
            closed_perp=list(d.get("closed_perp") or []),
            marks={k: float(v) for k, v in (d.get("marks") or {}).items() if v is not None},
            last_decision_bar=parse_ts(d.get("last_decision_bar")), last_loop_ts=parse_ts(d.get("last_loop_ts")),
            last_equity=None if d.get("last_equity") is None else float(d["last_equity"]),
            halted=d.get("halted"), counters=counters,
        )

    @classmethod
    def from_json(cls, text: str) -> Book:
        return cls.from_dict(json.loads(text))

    def save(self, path: str | os.PathLike) -> None:
        """Atomic replace: write a temp file in the same directory, fsync, then os.replace."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_json()
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: str | os.PathLike) -> Book:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


# --- ledger -----------------------------------------------------------------------------------
class Ledger:
    """Append-only JSONL event log: one strict-JSON object per line, `ts` (UTC ISO) and `kind` first."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict) -> dict:
        kind = event.get("kind")
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}; expected one of {EVENT_KINDS}")
        rec: dict[str, Any] = {"ts": iso(event.get("ts") or utc()), "kind": kind}
        rec.update({k: v for k, v in event.items() if k not in ("ts", "kind")})
        rec = to_jsonable(rec)
        line = json.dumps(rec, allow_nan=False, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
        return rec

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# --- paths + single-instance lock --------------------------------------------------------------
class StatePaths(NamedTuple):
    dir: Path
    book: Path
    events: Path
    lock: Path


def state_paths(state_dir: str | os.PathLike, root: str, mode: str) -> StatePaths:
    if mode not in ("paper", "testnet"):
        raise ValueError(f"mode must be 'paper' or 'testnet', got {mode!r}")
    d = Path(state_dir) / root / mode
    return StatePaths(d, d / "book.json", d / "events.jsonl", d / "daemon.lock")


class StateLocked(RuntimeError):
    pass


class StateLock:
    """Exclusive, non-blocking OS lock on state/{ROOT}/daemon.lock (the OS releases it if the process dies)."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="ascii")
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise StateLocked(f"{self.path} is held by another daemon process") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def __enter__(self) -> StateLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
