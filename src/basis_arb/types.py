"""Shared types and the aligned-panel schema contract used by data/, model/, backtest/ and live/."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

Leg = Literal["spot", "perp", "fut"]
Side = Literal["buy", "sell"]

STRATEGIES: tuple[str, ...] = ("cash_and_carry", "funding_carry", "calendar", "random_maturity")

# Leg composition for target = +1 (the "carry" direction). target = -1 flips every sign.
STRATEGY_LEGS: dict[str, tuple[tuple[Leg, int], ...]] = {
    "cash_and_carry": (("spot", +1), ("fut", -1)),
    "funding_carry": (("spot", +1), ("perp", -1)),
    "calendar": (("perp", +1), ("fut", -1)),
    "random_maturity": (("spot", +1), ("perp", -1)),
}

# --- panel schema (1h UTC grid; index name "ts", tz-aware UTC) --------------------------------
PANEL_REQUIRED_COLUMNS: tuple[str, ...] = (
    "spot_close", "spot_high", "spot_low",
    "perp_close", "perp_high", "perp_low",
    "funding_rate",            # NaN except at funding stamps; value settled AT that stamp
    "fut_cq_symbol", "fut_cq_close", "fut_cq_high", "fut_cq_low", "fut_cq_expiry", "fut_cq_dte_days",
    "fut_nq_symbol", "fut_nq_close", "fut_nq_high", "fut_nq_low", "fut_nq_expiry", "fut_nq_dte_days",
    "r_usd",                   # annual decimal, forward-filled FRED DTB3/100
    "y_coin",                  # annual decimal coin yield (r')
)
PANEL_OPTIONAL_COLUMNS: tuple[str, ...] = ("premium_index", "perp_mark")


def validate_panel(df: pd.DataFrame) -> None:
    """Raise ValueError if `df` violates the panel contract (fail-secure: callers must not guess)."""
    missing = [c for c in PANEL_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"panel missing columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise ValueError("panel index must be a tz-aware (UTC) DatetimeIndex")
    if df.index.name != "ts":
        raise ValueError("panel index must be named 'ts'")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("panel index must be strictly increasing without duplicates")
    for col in ("spot_close", "perp_close"):
        if (df[col] <= 0).any() or df[col].isna().any():
            raise ValueError(f"panel column {col} must be positive and non-null")


@dataclass(frozen=True)
class Fill:
    ts: datetime
    strategy: str
    leg: Leg
    instrument: str  # e.g. BTCUSDT (spot), BTCUSDT-PERP, BTCUSDT_261225
    side: Side
    qty: float  # coin units, positive
    price: float  # executed price (after slippage)
    fee: float  # quote currency, positive = paid
    reason: str  # ENTER / EXIT / ROLL / EXPIRY / LIQUIDATION / KILL


@dataclass
class LegPosition:
    leg: Leg
    instrument: str
    qty: float  # signed coin units (+ long, - short)
    entry_price: float
    entry_ts: datetime
    expiry: datetime | None = None  # futures only
    peak_adverse_move: float = 0.0  # worst adverse price move since entry, as a fraction (margin monitor)
