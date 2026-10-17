"""Shared types and the aligned-panel schema contract used by data/, model/, backtest/ and live/."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
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

RATE_BOUNDS = {"r_usd": (-0.05, 0.5), "y_coin": (0.0, 0.5)}  # decimal annual rates; catches percent-unit mistakes
DTE_TOLERANCE_DAYS = 1e-6


def validate_panel(df: pd.DataFrame) -> None:
    """Raise ValueError if `df` violates the panel contract (fail-secure: callers must not guess)."""
    missing = [c for c in PANEL_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"panel missing columns: {missing}")
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise ValueError("panel index must be a tz-aware (UTC) DatetimeIndex")
    if len(idx) == 0:
        raise ValueError("panel is empty")
    for ts in (idx[0], idx[-1]):
        if ts.utcoffset() != timedelta(0):
            raise ValueError("panel index must be in UTC (zero offset)")
    if idx.name != "ts":
        raise ValueError("panel index must be named 'ts'")
    if not idx.is_monotonic_increasing or idx.has_duplicates:
        raise ValueError("panel index must be strictly increasing without duplicates")
    for col in ("spot_close", "perp_close"):
        if (df[col] <= 0).any() or df[col].isna().any():
            raise ValueError(f"panel column {col} must be positive and non-null")
    for k in ("cq", "nq"):
        close = df[f"fut_{k}_close"].dropna()
        if (close <= 0).any():
            raise ValueError(f"panel column fut_{k}_close must be positive where present")
        expiry = df[f"fut_{k}_expiry"]
        dte = df[f"fut_{k}_dte_days"]
        both = expiry.notna() & dte.notna()
        if both.any():
            expected = (expiry[both] - pd.Series(idx, index=idx)[both]).dt.total_seconds() / 86400.0
            if (np.abs(expected.to_numpy(float) - dte[both].to_numpy(float)) > DTE_TOLERANCE_DAYS).any():
                raise ValueError(f"fut_{k}_dte_days disagrees with fut_{k}_expiry - ts")
    for col, (lo, hi) in RATE_BOUNDS.items():
        vals = df[col].to_numpy(float)
        if not np.isfinite(vals).all():
            raise ValueError(f"panel column {col} must be finite")
        if (vals < lo).any() or (vals > hi).any():
            raise ValueError(f"panel column {col} must be a decimal annual rate within [{lo}, {hi}]")


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
