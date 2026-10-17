"""Shared fixtures: a synthetic 1h panel that satisfies the schema contract."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_panel(
    n_hours: int = 24 * 120,
    start: str = "2025-01-01",
    spot: float = 50_000.0,
    basis_ann_cq: float = 0.10,
    basis_ann_nq: float = 0.10,
    funding_per_period: float = 0.0003,
    perp_premium: float = 0.0,
    r_usd: float = 0.04,
    y_coin: float = 0.0,
    n_contracts: int = 2,
) -> pd.DataFrame:
    """Flat spot, constant annualized basis per quarterly, constant funding: deterministic by design.

    A chain of `n_contracts` quarterlies expires 60d8h after `start` and then every 91 days. At each bar the
    nearest unexpired contract is `cq` and the next one `nq` (the real panel builder does the same). The
    first contract carries `basis_ann_cq`, every later one `basis_ann_nq`. Names: SYN_CQ, SYN_NQ, SYN_Q3, ...
    """
    ts = pd.date_range(start, periods=n_hours, freq="1h", tz="UTC", name="ts")
    names = ["SYN_CQ", "SYN_NQ"] + [f"SYN_Q{k}" for k in range(3, n_contracts + 1)]
    first_exp = pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=60, hours=8)
    expiries = [first_exp + pd.Timedelta(days=91 * k) for k in range(n_contracts)]
    basis = [basis_ann_cq] + [basis_ann_nq] * (n_contracts - 1)

    df = pd.DataFrame(index=ts)
    df["spot_close"] = spot
    df["spot_high"] = spot * 1.001
    df["spot_low"] = spot * 0.999
    df["perp_close"] = spot * (1.0 + perp_premium)
    df["perp_high"] = df["perp_close"] * 1.001
    df["perp_low"] = df["perp_close"] * 0.999
    fr = pd.Series(np.nan, index=ts)
    stamps = ts[(ts.hour % 8 == 0)]
    fr.loc[stamps] = funding_per_period
    df["funding_rate"] = fr

    n = len(ts)
    dte_mat = np.column_stack([((exp - ts) / pd.Timedelta(days=1)).to_numpy(float) for exp in expiries])
    active = dte_mat > 0
    rank = np.cumsum(active, axis=1) * active  # 1 for cq, 2 for nq, 0 otherwise
    for slot, r in (("cq", 1), ("nq", 2)):
        idx = np.where((rank == r).any(axis=1), np.argmax(rank == r, axis=1), -1)
        has = idx >= 0
        rows = np.arange(n)
        dte = np.where(has, dte_mat[rows, np.clip(idx, 0, None)], np.nan)
        b = np.where(has, np.asarray(basis)[np.clip(idx, 0, None)], np.nan)
        close = np.where(has, spot * np.exp(b * dte / 365.0), np.nan)
        df[f"fut_{slot}_symbol"] = [names[i] if i >= 0 else None for i in idx]
        df[f"fut_{slot}_close"] = close
        df[f"fut_{slot}_high"] = close * 1.001
        df[f"fut_{slot}_low"] = close * 0.999
        df[f"fut_{slot}_expiry"] = pd.Series(
            [expiries[i] if i >= 0 else pd.NaT for i in idx], index=ts, dtype="datetime64[ns, UTC]"
        )
        df[f"fut_{slot}_dte_days"] = dte
    df["r_usd"] = r_usd
    df["y_coin"] = y_coin
    return df


@pytest.fixture
def panel() -> pd.DataFrame:
    return make_panel()
