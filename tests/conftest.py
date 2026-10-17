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
) -> pd.DataFrame:
    """Flat spot, constant annualized basis on two quarterlies, constant funding: deterministic by design."""
    ts = pd.date_range(start, periods=n_hours, freq="1h", tz="UTC", name="ts")
    exp_cq = pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=60, hours=8)
    exp_nq = exp_cq + pd.Timedelta(days=91)
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
    for k, exp, b in (("cq", exp_cq, basis_ann_cq), ("nq", exp_nq, basis_ann_nq)):
        dte = (exp - ts) / pd.Timedelta(days=1)
        active = dte > 0
        fut = spot * np.exp(b * dte / 365.0)
        df[f"fut_{k}_symbol"] = np.where(active, f"SYN_{k.upper()}", None)
        df[f"fut_{k}_close"] = np.where(active, fut, np.nan)
        df[f"fut_{k}_high"] = df[f"fut_{k}_close"] * 1.001
        df[f"fut_{k}_low"] = df[f"fut_{k}_close"] * 0.999
        df[f"fut_{k}_expiry"] = pd.Series(np.where(active, exp, pd.NaT), index=ts).astype("datetime64[ns, UTC]")
        df[f"fut_{k}_dte_days"] = np.where(active, dte, np.nan)
    df["r_usd"] = r_usd
    df["y_coin"] = y_coin
    return df


@pytest.fixture
def panel() -> pd.DataFrame:
    return make_panel()
