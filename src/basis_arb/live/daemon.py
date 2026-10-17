"""Paper-live daemon (strategy_spec.md section 9): live quotes -> the backtest's signal functions -> fills.

    python -m basis_arb.live.daemon --root BTCUSDT --mode paper --once
    python -m basis_arb.live.daemon --root BTCUSDT --mode paper --interval 60
    python -m basis_arb.live.daemon --root BTCUSDT --mode testnet --interval 60   (testnet keys in env)

Research/live parity by construction: the decision path is basis_arb.model.signals.build_targets, the
function the backtest uses, applied to the cached 1h panel with one live row appended; the daemon reads
only the LAST row's target/contract per strategy and reconciles the book to it.

Loop, every interval_s seconds:
  1. history: rebuild the recent panel on the first loop of each UTC hour (history.recent_panel)
  2. feed snapshot (FeedError or a quote older than 5 min -> SAFE)
  3. funding: when a stamp was crossed since the last loop, fetch the settled print(s) from
     /fapi/v1/fundingRate and credit/debit the perp legs held before the stamp; patch the panel
  4. expiry: settle held quarterlies whose expiry has passed at the index price (+ delivery fee)
  5. decision: append the live row, build_targets, adopt the last row's target/contract once per bar
     (the first loop after each hourly bar closes, as the backtest decides at bar closes)
  6. reconcile the book to the adopted targets via the broker: exits always; entries and rolls only on
     a decision loop and only when not SAFE; never leave an unhedged remainder
  7. mark-to-market, drawdown kill, SNAPSHOT + HEARTBEAT events, atomic book save
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from basis_arb.config import BacktestConfig
from basis_arb.live import history
from basis_arb.live.broker import BrokerError, Order, PaperBroker, TestnetBroker
from basis_arb.live.feed import (
    FeedError,
    LiveFeed,
    LiveQuote,
    append_live_row,
    perp_instrument,
)
from basis_arb.live.state import (
    Book,
    Ledger,
    StateLock,
    StateLocked,
    from_ms,
    iso,
    state_paths,
    to_ms,
    utc,
)
from basis_arb.model import signals as model_signals
from basis_arb.types import STRATEGIES, STRATEGY_LEGS, LegPosition, validate_panel

log = logging.getLogger(__name__)

DEFAULT_STRATEGIES: tuple[str, ...] = ("funding_carry", "cash_and_carry", "calendar", "random_maturity")
QUOTE_SAFE_AGE_S = 5 * 60.0  # quote older than 5 min: SAFE (no new entries)
QUOTE_HALT_AGE_S = 30 * 60.0  # no fresh quote for 30 min: flatten + HALT
MAX_DRAWDOWN_FRAC = 0.05  # equity more than 5% below equity_0: flatten + HALT
FUNDING_PENDING_WARN_S = 2 * 3600.0
HISTORY_RETRY_S = 300.0
ENV_API_KEY = "BINANCE_TESTNET_API_KEY"
EXIT_HALT = 4  # distinct from 1 (uncaught crash) so a supervisor can restart crashes but never a HALT
ENV_API_SECRET = "BINANCE_TESTNET_API_SECRET"

SAFE_FEED = "FEED_ERROR"  # non-200, maintenance, malformed quote (clears when the feed recovers)
SAFE_STALE = "STALE_QUOTE"  # quote older than QUOTE_SAFE_AGE_S (clears when fresh)
SAFE_NO_PANEL = "NO_PANEL"  # history unavailable at start (clears when loaded)
SAFE_DECISION = "DECISION_EXCEPTION"  # exception in the decision path: sticky until restart
SAFE_BROKER = "BROKER_FAILURE"  # order rejected / unknown state: sticky until restart


class MissingCredentials(RuntimeError):
    pass


class StartupRefused(RuntimeError):
    pass


def testnet_broker_from_env(cfg: BacktestConfig, env: dict | None = None) -> TestnetBroker:
    env = os.environ if env is None else env
    key, secret = env.get(ENV_API_KEY, ""), env.get(ENV_API_SECRET, "")
    missing = [name for name, val in ((ENV_API_KEY, key), (ENV_API_SECRET, secret)) if not val]
    if missing:
        raise MissingCredentials(
            f"testnet mode requires {' and '.join(missing)} in the environment (testnet keys only, never in a "
            "file); refusing to start"
        )
    return TestnetBroker(key, secret, cfg.costs)


class Daemon:
    """One underlying, one book, one process (state/{ROOT}/daemon.lock)."""

    def __init__(
        self,
        root: str,
        mode: str = "paper",
        *,
        cfg: BacktestConfig | None = None,
        strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
        state_dir: str | os.PathLike = "state",
        interval_s: float = 60,
        feed: Any = None,
        broker: Any = None,
        history_fn: Callable[[str], pd.DataFrame] | None = None,
        funding_fn: Callable[..., pd.DataFrame] | None = None,
        clock: Callable[[], Any] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        clear_halt: bool = False,
    ) -> None:
        if mode not in ("paper", "testnet"):
            raise ValueError(f"mode must be 'paper' or 'testnet', got {mode!r}")
        strategies = tuple(strategies)
        unknown = [s for s in strategies if s not in STRATEGIES]
        if unknown or not strategies:
            raise ValueError(f"unknown strategies {unknown}; choose from {STRATEGIES}")
        self.root, self.mode, self.strategies = root.upper(), mode, strategies
        self.cfg = cfg or BacktestConfig()
        self.interval_s = float(interval_s)
        self.clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self.sleep_fn = sleep_fn or time.sleep
        if broker is None:
            broker = PaperBroker(self.cfg.costs) if mode == "paper" else testnet_broker_from_env(self.cfg)
        self.broker = broker
        self.feed = feed if feed is not None else LiveFeed()
        rates = self.cfg.rates
        self.history_fn = history_fn or (lambda r: history.recent_panel(r, rates=rates))
        self.funding_fn = funding_fn or history.funding_prints
        self.paths = state_paths(state_dir, self.root)
        self.lock = StateLock(self.paths.lock)
        self.lock.acquire()
        try:
            self.ledger = Ledger(self.paths.events)
            self.started_at = utc(self.clock())
            self._now = self.started_at
            self.book = self._load_or_create(clear_halt)
        except BaseException:
            self.lock.release()
            raise
        self.panel: pd.DataFrame | None = None
        self.panel_hour: pd.Timestamp | None = None
        self.history_retry_at: pd.Timestamp | None = None
        self.r_usd_source: str | None = None
        self.last_fresh_quote: LiveQuote | None = None
        self.sticky_safe: tuple[str, str] | None = None
        self.safe_codes_prev: tuple[str, ...] = ()
        self.halted = False
        self.loops = 0
        self.funding_warned = False

    # -- lifecycle ------------------------------------------------------------------------------
    def _load_or_create(self, clear_halt: bool) -> Book:
        path = self.paths.book
        if not path.exists():
            book = Book.new(self.root, self.mode, self.strategies, self.cfg, self.started_at)
            book.save(path)
            return book
        book = Book.load(path)
        if book.root != self.root or book.mode != self.mode:
            raise StartupRefused(
                f"{path} belongs to root={book.root} mode={book.mode}, this run is root={self.root} "
                f"mode={self.mode}; use another --state-dir or reset the state (docs/operations.md)")
        if set(book.strategies) != set(self.strategies):
            raise StartupRefused(
                f"{path} trades {sorted(book.strategies)}, this run asks for {sorted(self.strategies)}; "
                "reset the state or pass the same --strategies")
        if book.halted:
            if not clear_halt:
                raise StartupRefused(
                    f"book is HALTED ({book.halted.get('switch')}: {book.halted.get('detail')}); inspect "
                    f"{self.paths.events}, then reset the state or rerun with --clear-halt")
            self.ledger.append({"kind": "HEARTBEAT", "ts": self.started_at, "phase": "clear_halt",
                                "previous_halt": book.halted})
            book.halted = None
            book.save(path)
        return book

    def close(self) -> None:
        self.lock.release()

    def __enter__(self) -> Daemon:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def run_loop(self, once: bool = False, max_loops: int | None = None,
                 out: Callable[[str], None] | None = print) -> dict:
        self.ledger.append({
            "kind": "HEARTBEAT", "ts": utc(self.clock()), "phase": "start", "mode": self.mode, "root": self.root,
            "broker": getattr(self.broker, "name", type(self.broker).__name__), "strategies": list(self.strategies),
            "interval_s": self.interval_s, "pid": os.getpid(), "equity_0": self.book.equity_0,
        })
        st: dict = {}
        try:
            while True:
                t0 = time.monotonic()
                st = self.step()
                if out is not None:
                    out(format_status(st, self) if once else format_line(st))
                if self.halted or once or (max_loops is not None and self.loops >= max_loops):
                    break
                self.sleep_fn(max(0.0, self.interval_s - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            log.info("interrupted; stopping")
        finally:
            self.ledger.append({"kind": "HEARTBEAT", "ts": utc(self.clock()), "phase": "stop", "loops": self.loops,
                                "halted": self.halted})
            self.book.save(self.paths.book)
        return st

    # -- one loop -------------------------------------------------------------------------------
    def step(self) -> dict:
        now = utc(self.clock())
        if self.halted:  # a kill switch fired: nothing may trade until a deliberate restart
            return {"ts": now, "loop": self.loops, "mode": self.mode, "root": self.root, "halted": True,
                    "safe": [], "safe_codes": [], "fills": [], "errors": [], "signals": {}, "actions": {}}
        self._now = now
        self.loops += 1
        self.book.counters["loops"] = self.book.counters.get("loops", 0) + 1
        st: dict = {"ts": now, "loop": self.loops, "mode": self.mode, "root": self.root, "safe": [], "fills": [],
                    "errors": [], "funding": [], "expiries": [], "decision_loop": False, "signals": {},
                    "actions": {}, "quote": None, "quote_fresh": False, "quote_age_s": None, "halted": False}
        try:
            self._step(now, st)
        except Exception as exc:  # bookkeeping failure outside the decision path: block entries, keep looping
            self._set_sticky(SAFE_DECISION, f"loop exception {type(exc).__name__}: {exc}")
            self._error("loop", exc, st)
        self.book.last_loop_ts = st["ts"]
        self.book.save(self.paths.book)
        st["halted"] = self.halted
        st["safe_codes"] = sorted({c for c, _ in st["safe"]})
        return st

    def _step(self, now: pd.Timestamp, st: dict) -> None:
        b = self.book
        self._refresh_history(now, st)

        quote: LiveQuote | None = None
        try:
            quote = self.feed.snapshot(self.root)
        except Exception as exc:  # FeedError or anything unexpected from the feed: SAFE, never a guess
            st["safe"].append((SAFE_FEED, str(exc) if isinstance(exc, FeedError) else f"{type(exc).__name__}: {exc}"))
        now = utc(self.clock())  # after the (hourly, ~seconds) history fetch and the snapshot
        self._now = st["ts"] = now
        fresh = False
        if quote is not None:
            age = quote.age_s(now)
            if age > QUOTE_SAFE_AGE_S:
                st["safe"].append((SAFE_STALE, f"quote age {age:.0f}s > {QUOTE_SAFE_AGE_S:.0f}s"))
            else:
                fresh = True
                self.last_fresh_quote = quote
        ref = quote if fresh else self.last_fresh_quote
        st["quote"], st["quote_fresh"] = ref, fresh
        st["quote_age_s"] = ref.age_s(now) if ref is not None else None

        self._funding(now, quote if fresh else None, st)  # settled prints do not depend on the quote
        stale_for = (self.last_fresh_quote.age_s(now) if self.last_fresh_quote is not None
                     else (now - self.started_at).total_seconds())
        if stale_for > QUOTE_HALT_AGE_S:
            self._kill("STALE_QUOTE_30MIN", f"no fresh quote for {stale_for:.0f}s (> {QUOTE_HALT_AGE_S:.0f}s)",
                       ref, now, st)
            return
        if fresh:
            self._settle_expiries(now, quote, st)
        if self.sticky_safe is not None:
            st["safe"].append(self.sticky_safe)
        if fresh and self.panel is None:
            st["safe"].append((SAFE_NO_PANEL, "recent history panel unavailable"))
        if fresh and self.panel is not None:
            self._decide(now, quote, st)
        self._safe_transition(now, st)

        mtm = b.mark_to_market(ref.prices() if ref is not None else {})
        st["mtm"] = mtm
        if mtm["drawdown"] > MAX_DRAWDOWN_FRAC:
            self._kill("DRAWDOWN", f"equity {mtm['equity']:.2f} is {100 * mtm['drawdown']:.2f}% below equity_0 "
                                   f"{mtm['equity_0']:.2f} (limit {100 * MAX_DRAWDOWN_FRAC:.0f}%)", ref, now, st)
            return
        self.ledger.append({
            "kind": "SNAPSHOT", "ts": now, "quote": ref.to_event() if ref is not None else None,
            "quote_fresh": fresh, "equity": mtm["equity"], "equity_0": mtm["equity_0"],
            "drawdown": mtm["drawdown"], "missing_marks": mtm["missing_marks"], "strategies": mtm["strategies"],
            "r_usd_source": self.r_usd_source,
        })
        self.ledger.append({
            "kind": "HEARTBEAT", "ts": now, "loop": self.loops, "status": "SAFE" if st["safe"] else "OK",
            "safe": sorted({c for c, _ in st["safe"]}), "decision_loop": st["decision_loop"],
            "fills": len(st["fills"]), "equity": mtm["equity"],
            "quote_age_s": None if st["quote_age_s"] is None else round(st["quote_age_s"], 3),
            "latency_ms": None if quote is None else round(quote.latency_ms, 1),
        })

    # -- 1. history -----------------------------------------------------------------------------
    def _refresh_history(self, now: pd.Timestamp, st: dict) -> None:
        hour = now.floor("h")
        if self.panel is not None and self.panel_hour is not None and hour <= self.panel_hour:
            return
        if self.history_retry_at is not None and now < self.history_retry_at:
            return
        try:
            fresh = self.history_fn(self.root)
            validate_panel(fresh)
            source = fresh.attrs.get("r_usd_source")
            fresh = fresh.copy()
            if self.panel is not None and len(self.panel) and self.panel.index[-1] > fresh.index[-1]:
                # keep provisional live rows for hours the refresh does not cover yet
                tail = self.panel[self.panel.index > fresh.index[-1]].reindex(columns=fresh.columns)
                fresh = pd.concat([fresh, tail])
                fresh.index.name = "ts"
            self.panel, self.panel_hour, self.history_retry_at = fresh, hour, None
            self.r_usd_source = source
            st["history"] = f"refreshed ({len(fresh)} rows, r_usd {source})"
        except Exception as exc:
            self.history_retry_at = now + pd.Timedelta(seconds=HISTORY_RETRY_S)
            self._error("history refresh", exc, st)
            st["history"] = "refresh failed" + (" (using cached panel)" if self.panel is not None else "")

    # -- 3. funding -----------------------------------------------------------------------------
    def _funding(self, now: pd.Timestamp, quote: LiveQuote | None, st: dict) -> None:
        b = self.book
        now_ms = to_ms(now)
        if b.funding_due_ms is None and b.next_funding_ms is not None and now_ms >= b.next_funding_ms:
            b.funding_due_ms = int(b.next_funding_ms)
        if b.funding_due_ms is not None:
            self._accrue_due(now, now_ms, quote, st)
        if quote is not None:
            b.next_funding_ms = to_ms(quote.next_funding_time)

    def _accrue_due(self, now: pd.Timestamp, now_ms: int, quote: LiveQuote | None, st: dict) -> None:
        b = self.book
        try:
            prints = self.funding_fn(self.root, b.funding_cursor_ms + 1, now_ms)
        except Exception as exc:
            self._error("funding fetch", exc, st)
            return
        for stamp, row in prints.sort_index().iterrows():
            s_ms = to_ms(stamp)
            if s_ms <= b.funding_cursor_ms or s_ms > now_ms:
                continue
            rate = float(row["rate"])
            mark = float(row.get("mark_price", np.nan))
            if not (math.isfinite(mark) and mark > 0):
                mark = quote.perp_mark if quote is not None else np.nan
            if not (math.isfinite(rate) and math.isfinite(mark) and mark > 0):
                break  # cannot price this stamp yet: cursor not advanced, retried next loop
            for exp in b.perp_exposure_at(stamp):
                amount = -exp["qty"] * rate * mark  # a short with positive funding receives cash
                b.apply_funding(exp["strategy"], amount)
                ev = {"stamp": stamp, "strategy": exp["strategy"], "instrument": exp["instrument"],
                      "qty": exp["qty"], "rate": rate, "mark_price": mark, "amount": amount,
                      "closed_leg": exp["closed"]}
                self.ledger.append({"kind": "FUNDING_ACCRUAL", "ts": now, **ev})
                st["funding"].append(ev)
            b.funding_cursor_ms = s_ms
            if self.panel is not None and stamp in self.panel.index:
                self.panel.loc[stamp, "funding_rate"] = rate
        b.prune_closed_perp()
        due_stamp_ms = to_ms(from_ms(b.funding_due_ms).floor("h"))
        if b.funding_cursor_ms >= due_stamp_ms:
            b.funding_due_ms, self.funding_warned = None, False
        elif now_ms - b.funding_due_ms > FUNDING_PENDING_WARN_S * 1000 and not self.funding_warned:
            self.funding_warned = True
            self._error("funding pending", RuntimeError(
                f"settled rate for stamp {iso(from_ms(due_stamp_ms))} not published after "
                f"{FUNDING_PENDING_WARN_S / 3600:.0f}h; still retrying"), st)

    # -- 4. expiry ------------------------------------------------------------------------------
    def _settle_expiries(self, now: pd.Timestamp, quote: LiveQuote, st: dict) -> None:
        b = self.book
        for name, sb in b.strategies.items():
            for p in list(sb.positions):
                if p.leg != "fut" or p.expiry is None or utc(p.expiry) > now:
                    continue
                use_index = math.isfinite(quote.perp_index) and quote.perp_index > 0
                price = quote.perp_index if use_index else quote.spot_mid
                fee = abs(p.qty) * price * self.cfg.costs.delivery_fee_bps / 1e4
                res = b.settle_expiry(name, p.instrument, price, fee, now)
                ev = {"strategy": name, "instrument": p.instrument, "expiry": p.expiry,
                      "price_source": "index" if use_index else "spot_mid", **res}
                self.ledger.append({"kind": "EXPIRY", "ts": now, **ev})
                st["expiries"].append(ev)

    # -- 5. decision ----------------------------------------------------------------------------
    def _decide(self, now: pd.Timestamp, quote: LiveQuote, st: dict) -> None:
        b = self.book
        try:
            panel = append_live_row(self.panel, quote)
            self.panel = panel
            targets = model_signals.build_targets(panel, self.cfg, self.strategies)
            signals = {}
            for name in self.strategies:
                row = targets[name].iloc[-1]
                contract = row["contract"]
                signals[name] = {"target": int(row["target"]),
                                 "contract": contract if isinstance(contract, str) else None,
                                 "excess": float(row["excess"]), "hurdle": float(row["hurdle"]),
                                 "reason": str(row["reason"])}
            st["signals"] = signals
            st["live_row_ts"] = panel.index[-1]
            bar = now.floor("h")
            decision = b.last_decision_bar is None or bar > b.last_decision_bar
            st["decision_loop"] = decision
            if decision:
                for name, sig in signals.items():
                    sb = b.strategies[name]
                    sb.signal_target, sb.signal_contract, sb.signal_reason = sig["target"], sig["contract"], sig["reason"]
        except Exception as exc:
            self._set_sticky(SAFE_DECISION, f"{type(exc).__name__}: {exc}")
            self._error("decision", exc, st)
            st["safe"].append(self.sticky_safe)
            return
        entries_allowed = decision and self.sticky_safe is None
        actions: dict[str, str] = {}
        for name in self.strategies:
            try:
                actions[name] = self._reconcile(name, quote, decision, st)
            except BrokerError as exc:  # an exit/flatten order failed: legs stay booked, retried next loop
                self._set_sticky(SAFE_BROKER, f"{name}: {exc}")
                self._error(f"order {name}", exc, st)
                actions[name] = "BROKER_ERROR"
            except Exception as exc:
                self._set_sticky(SAFE_DECISION, f"reconcile {name}: {type(exc).__name__}: {exc}")
                self._error(f"reconcile {name}", exc, st)
                actions[name] = "ERROR"
        if self.sticky_safe is not None and self.sticky_safe not in st["safe"]:
            st["safe"].append(self.sticky_safe)
        st["actions"] = actions
        if decision:
            b.last_decision_bar = bar
            self.ledger.append({
                "kind": "DECISION", "ts": now, "bar": bar, "live_row_ts": panel.index[-1],
                "entries_allowed": entries_allowed,
                "strategies": {n: {**signals[n], "action": actions.get(n),
                                   "position_target": b.strategies[n].target,
                                   "position_contract": b.strategies[n].contract} for n in self.strategies},
            })

    # -- 6. reconcile ---------------------------------------------------------------------------
    def _desired_legs(self, name: str, target: int, contract: str | None,
                      quote: LiveQuote) -> list[tuple[str, str, int]]:
        if target == 0:
            return []
        out = []
        for leg, sgn in STRATEGY_LEGS[name]:
            if leg == "spot":
                inst = self.root
            elif leg == "perp":
                inst = perp_instrument(self.root)
            else:
                if not contract:
                    return []
                try:
                    quote.book(contract)
                except KeyError:
                    return []  # contract no longer quoted (expired / delisted): the strategy cannot hold it
                inst = contract
            out.append((leg, inst, int(sgn * target)))
        return out

    def _reconcile(self, name: str, quote: LiveQuote, decision: bool, st: dict) -> str:
        sb = self.book.strategies[name]
        want = self._desired_legs(name, sb.signal_target, sb.signal_contract, quote)
        want_sign = {(leg, inst): sgn for leg, inst, sgn in want}
        to_close = [p for p in sb.positions if want_sign.get((p.leg, p.instrument)) != int(np.sign(p.qty))]
        keep = [p for p in sb.positions if p not in to_close]
        kept = {(p.leg, p.instrument) for p in keep}
        to_open = [(leg, inst, sgn) for leg, inst, sgn in want if (leg, inst) not in kept]
        if not to_close and not to_open:
            return "HOLD" if sb.positions else "FLAT"

        blocked = None
        if to_open:
            if not decision:
                blocked = "WAIT_BAR"  # opens happen only on the first loop after a bar closes
            elif self.sticky_safe is not None:
                blocked = "BLOCKED_SAFE"
            elif not all(self.broker.supports(leg, inst) for leg, inst, _ in to_open):
                blocked = "UNSUPPORTED"
        if blocked is not None:
            if not keep and not to_close:
                return blocked  # nothing held, entry not allowed
            for p in list(sb.positions):  # never leave an unhedged remainder: flatten the strategy
                self._close(name, p, "EXIT", quote, st)
            sb.n_exits += 1
            return f"EXIT({blocked})"

        is_roll = (bool(keep) and bool(to_close) and bool(to_open)
                   and all(p.leg == "fut" for p in to_close) and all(leg == "fut" for leg, _, _ in to_open))
        for p in to_close:
            self._close(name, p, "ROLL" if is_roll else "EXIT", quote, st)
        if not to_open:
            sb.n_exits += 1
            return "EXIT"
        if to_close and not is_roll:
            sb.n_exits += 1  # flip: the old direction was closed above
        qty = self._entry_qty(keep, to_open, quote)
        if not (math.isfinite(qty) and qty > 0):
            raise ValueError(f"cannot size {name}: qty {qty}")
        reason = "ROLL" if is_roll else "ENTER"
        for leg, inst, sgn in to_open:
            try:
                self._execute(Order(name, leg, inst, "buy" if sgn > 0 else "sell", qty, reason), quote, st)
            except BrokerError as exc:
                self._set_sticky(SAFE_BROKER, f"{name} {reason} {inst}: {exc}")
                self._error(f"{reason} {name}", exc, st)
                for p in list(sb.positions):  # unwind whatever this strategy now holds
                    try:
                        self._close(name, p, "KILL", quote, st)
                    except BrokerError as exc2:
                        self._error(f"unwind {name}", exc2, st)
                return "UNWOUND"
        if is_roll:
            sb.n_rolls += 1
            return "ROLL"
        sb.n_entries += 1
        return "FLIP" if to_close else "ENTER"

    def _entry_qty(self, keep: list[LegPosition], to_open: list[tuple[str, str, int]], quote: LiveQuote) -> float:
        """Equal coin quantity on both legs: notional_per_leg / first-leg mid (or the kept leg's qty on a roll)."""
        base = abs(keep[0].qty) if keep else self.cfg.notional_per_leg / quote.mid(to_open[0][1])
        return min(self.broker.round_qty(leg, inst, base) for leg, inst, _ in to_open)

    def _close(self, name: str, p: LegPosition, reason: str, quote: LiveQuote, st: dict, stale: bool = False):
        side = "sell" if p.qty > 0 else "buy"
        return self._execute(Order(name, p.leg, p.instrument, side, abs(p.qty), reason), quote, st, stale)

    def _execute(self, order: Order, quote: LiveQuote, st: dict, stale: bool = False):
        fill = self.broker.place(order, quote)
        try:
            mid = quote.mid(order.instrument)
        except KeyError:
            mid = None
        expiry = quote.expiry_of(order.instrument) if order.leg == "fut" else None
        res = self.book.apply_fill(fill, mid, expiry)
        ev = {"strategy": fill.strategy, "leg": fill.leg, "instrument": fill.instrument, "side": fill.side,
              "qty": fill.qty, "price": fill.price, "fee": fill.fee, "reason": fill.reason, "fill_ts": fill.ts,
              "mid": mid, "slippage_cost": res["slippage_cost"], "realized": res["realized"],
              "broker": getattr(self.broker, "name", type(self.broker).__name__), "quote_ts": quote.ts,
              "stale_quote": stale}
        self.ledger.append({"kind": "FILL", "ts": self._now, **ev})
        st["fills"].append(ev)
        return fill

    # -- kill switches / SAFE / errors ----------------------------------------------------------
    def _kill(self, switch: str, detail: str, quote: LiveQuote | None, now: pd.Timestamp, st: dict) -> None:
        b = self.book
        b.counters["kills"] = b.counters.get("kills", 0) + 1
        age = quote.age_s(now) if quote is not None else None
        self.ledger.append({"kind": "KILL", "ts": now, "switch": switch, "detail": detail,
                            "quote_ts": quote.ts if quote is not None else None, "quote_age_s": age})
        stale = age is not None and age > QUOTE_SAFE_AGE_S
        unflattened = []
        for name, sb in b.strategies.items():
            for p in list(sb.positions):
                if quote is None:
                    unflattened.append({"strategy": name, "instrument": p.instrument, "qty": p.qty,
                                        "why": "no quote available"})
                    continue
                try:
                    self._close(name, p, "KILL", quote, st, stale=stale)
                except (BrokerError, KeyError, ValueError) as exc:
                    unflattened.append({"strategy": name, "instrument": p.instrument, "qty": p.qty, "why": str(exc)})
                    self._error(f"flatten {name}", exc, st)
            sb.signal_target, sb.signal_contract, sb.signal_reason = 0, None, "KILL"
        mtm = b.mark_to_market(quote.prices() if quote is not None else {})
        st["mtm"] = mtm
        b.halted = {"switch": switch, "detail": detail, "ts": iso(now), "unflattened": unflattened,
                    "equity": mtm["equity"]}
        self.ledger.append({"kind": "HALT", "ts": now, "switch": switch, "detail": detail,
                            "unflattened": unflattened, "equity": mtm["equity"], "equity_0": mtm["equity_0"]})
        self.halted = True
        st["halt"] = {"switch": switch, "detail": detail, "unflattened": unflattened}

    def _set_sticky(self, code: str, detail: str) -> None:
        if self.sticky_safe is None:
            self.sticky_safe = (code, detail + " (entries blocked until restart)")

    def _safe_transition(self, now: pd.Timestamp, st: dict) -> None:
        reasons: dict[str, str] = {}
        for code, detail in st["safe"]:
            reasons.setdefault(code, detail)
        codes = tuple(sorted(reasons))
        if codes:
            self.book.counters["safe_loops"] = self.book.counters.get("safe_loops", 0) + 1
        if codes != self.safe_codes_prev:
            self.ledger.append({"kind": "SAFE", "ts": now, "active": bool(codes), "codes": list(codes),
                                "reasons": [{"code": c, "detail": reasons[c]} for c in codes],
                                "sticky": self.sticky_safe is not None})
            self.safe_codes_prev = codes

    def _error(self, where: str, exc: BaseException, st: dict) -> None:
        self.book.counters["errors"] = self.book.counters.get("errors", 0) + 1
        msg = f"{type(exc).__name__}: {exc}"
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=6))
        self.ledger.append({"kind": "ERROR", "ts": self._now, "where": where, "error": msg, "traceback": tb})
        st["errors"].append(f"{where}: {msg}")
        log.error("%s: %s", where, msg)


# --- status output ----------------------------------------------------------------------------
def _f(x: Any, fmt: str = ".2f") -> str:
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return "-"
    return format(xf, fmt) if math.isfinite(xf) else "nan"


def _hm(ts: Any) -> str:
    return "-" if ts is None else utc(ts).strftime("%Y-%m-%dT%H:%MZ")


def format_line(st: dict) -> str:
    mtm = st.get("mtm") or {}
    sig = " ".join(f"{n[:4]}:{s['target']:d}" for n, s in (st.get("signals") or {}).items())
    status = "HALT" if st.get("halted") else ("SAFE" if st.get("safe") else "OK")
    return (f"{utc(st['ts']).strftime('%Y-%m-%dT%H:%M:%SZ')} {st['root']} {st['mode']} loop={st['loop']} {status} "
            f"eq={_f(mtm.get('equity'))} dd={_f(100 * (mtm.get('drawdown') or 0.0))}% fills={len(st['fills'])} "
            f"sig[{sig}] safe={','.join(st.get('safe_codes') or []) or '-'}")


def format_status(st: dict, daemon: Daemon | None = None) -> str:
    q: LiveQuote | None = st.get("quote")
    status = "HALT" if st.get("halted") else ("SAFE" if st.get("safe") else "OK")
    lines = [f"basis_arb live | {st['root']} | {st['mode']} | {utc(st['ts']).strftime('%Y-%m-%dT%H:%M:%SZ')} | "
             f"loop {st['loop']} | {status}"]
    if q is not None:
        lines.append(f"quote  spot {_f(q.spot_mid)}  perp {_f(q.perp_mid)} (mark {_f(q.perp_mark)}, index "
                     f"{_f(q.perp_index)})  latency {_f(q.latency_ms, '.0f')}ms  age {_f(st.get('quote_age_s'), '.1f')}s"
                     f"{'' if st.get('quote_fresh') else '  [STALE]'}")
        legs = []
        for k, sym, exp in (("cq", q.cq_symbol, q.cq_expiry), ("nq", q.nq_symbol, q.nq_expiry)):
            if sym is not None:
                dte = (utc(exp) - utc(q.ts)) / pd.Timedelta(days=1)
                legs.append(f"{k} {sym} {_f(q.mid(sym))} dte {_f(dte, '.1f')}")
        lines.append(f"       funding last {_f(100 * q.last_funding_rate, '.4f')}% next {_hm(q.next_funding_time)}"
                     f" | {' | '.join(legs) or 'no quarterlies'}")
    else:
        lines.append("quote  none")
    if daemon is not None and daemon.panel is not None:
        p = daemon.panel
        lines.append(f"panel  {len(p)} rows {_hm(p.index[0])} .. {_hm(p.index[-1])} (last = live row) | r_usd "
                     f"{_f(p['r_usd'].iloc[-1], '.4f')} ({daemon.r_usd_source}) | decision loop: "
                     f"{'yes' if st.get('decision_loop') else 'no'}")
    for i, (name, sig) in enumerate((st.get("signals") or {}).items()):
        book_s = daemon.book.strategies[name] if daemon is not None else None
        legs = ", ".join(f"{p.instrument} {p.qty:+.6f}@{_f(p.entry_price)}" for p in book_s.positions) if book_s else ""
        tgt = f"{sig['target']:+d}" if sig["target"] else " 0"
        lines.append(f"{'signal' if i == 0 else '      '} {name:<16} target {tgt} {sig['reason']:<6} "
                     f"excess {_f(sig['excess'], '+.4f')} hurdle {_f(sig['hurdle'], '.4f')} "
                     f"{sig['contract'] or '':<15} action {st.get('actions', {}).get(name, '-'):<10} pos {legs or 'flat'}")
    mtm = st.get("mtm") or {}
    lines.append(f"book   equity {_f(mtm.get('equity'))} / equity_0 {_f(mtm.get('equity_0'))} (dd "
                 f"{_f(100 * (mtm.get('drawdown') or 0.0))}%) | fills this loop {len(st['fills'])} | funding events "
                 f"{len(st.get('funding') or [])} | safe: "
                 f"{'; '.join(f'{c}: {d}' for c, d in dict(st['safe']).items()) or '-'}")
    if st.get("errors"):
        lines.append("errors " + " | ".join(st["errors"])[:400])
    if st.get("halt"):
        lines.append(f"HALT   {st['halt']['switch']}: {st['halt']['detail']}")
    if daemon is not None:
        lines.append(f"state  {daemon.paths.book} | {daemon.paths.events}")
    return "\n".join(lines)


# --- entry points -----------------------------------------------------------------------------
def run(
    root: str,
    mode: str = "paper",
    interval_s: int = 60,
    once: bool = False,
    max_loops: int | None = None,
    strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
    state_dir: str | os.PathLike = "state",
    cfg: BacktestConfig = BacktestConfig(),
    *,
    clear_halt: bool = False,
    feed: Any = None,
    broker: Any = None,
    history_fn: Callable[[str], pd.DataFrame] | None = None,
    funding_fn: Callable[..., pd.DataFrame] | None = None,
    clock: Callable[[], Any] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    out: Callable[[str], None] | None = print,
) -> dict:
    """Run the daemon; returns the last loop's status dict (status["halted"] is True after a kill switch)."""
    if mode not in ("paper", "testnet"):
        raise ValueError(f"mode must be 'paper' or 'testnet', got {mode!r}")
    if mode == "testnet" and broker is None:
        broker = testnet_broker_from_env(cfg)  # raises MissingCredentials before any state is touched
        try:
            balances = broker.check()
        except (BrokerError, KeyError, TypeError, ValueError) as exc:
            raise StartupRefused(f"testnet credential/connectivity check failed: {exc}") from None
        log.info("testnet balances: %s", balances)
    daemon = Daemon(root, mode, cfg=cfg, strategies=strategies, state_dir=state_dir, interval_s=interval_s,
                    feed=feed, broker=broker, history_fn=history_fn, funding_fn=funding_fn, clock=clock,
                    sleep_fn=sleep_fn, clear_halt=clear_halt)
    try:
        return daemon.run_loop(once=once, max_loops=max_loops, out=out)
    finally:
        daemon.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m basis_arb.live.daemon",
        description="Paper-live daemon: paper fills on live Binance quotes, or orders to the Binance TESTNET only.")
    ap.add_argument("--root", required=True, help="underlying, e.g. BTCUSDT or ETHUSDT")
    ap.add_argument("--mode", choices=("paper", "testnet"), default="paper")
    ap.add_argument("--interval", type=int, default=60, help="seconds between loops (spec: 60)")
    ap.add_argument("--once", action="store_true", help="run a single loop, print a compact status and exit")
    ap.add_argument("--max-loops", type=int, default=None)
    ap.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--clear-halt", action="store_true", help="deliberately clear a HALTED book, then start")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    strategies = tuple(s.strip() for s in args.strategies.split(",") if s.strip())
    try:
        st = run(args.root, args.mode, interval_s=args.interval, once=args.once, max_loops=args.max_loops,
                 strategies=strategies, state_dir=args.state_dir, clear_halt=args.clear_halt)
    except MissingCredentials as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (StartupRefused, StateLocked) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    return EXIT_HALT if st.get("halted") else 0


if __name__ == "__main__":
    sys.exit(main())
