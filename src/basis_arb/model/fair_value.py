"""Theoretical (fair) values for dated futures and perpetual futures, and the derived excess measures.

Sources (all verified 2026-10-18; reading notes and URLs in docs/theory.md)
---------------------------------------------------------------------------
Dated futures (cost of carry, no storage, coin yield y):
    F*(t, T) = S_t * exp((r - y)(T - t)),   annualized basis b = ln(F/S)/tau,   fair b* = r - y.
    Schmeling, Schrimpf & Todorov, "Crypto carry", BIS WP 1087 (2023): the market deviation b - b* is a
    volatile convenience yield driven by leveraged speculative demand and scarce arbitrage capital.

Perpetual futures: He, Manela, Ross & von Wachter, "Fundamentals of Perpetual Futures" (arXiv:2212.06888).
    Funding is paid continuously at rate kappa * (F_s - S_s) per unit time (kappa = 1095/yr <=> 8h cadence).
    With a constant stablecoin short rate r and underlying rate r' (kappa > r - r'):
        Prop. 1 (frictionless):      F_t = kappa / (kappa - (r - r')) * S_t
        Prop. 2 (round-trip cost C): |F_t - kappa/(kappa - (r - r')) * S_t| <= C,
    with annualized bounds rho_l = kappa*ln(1 - C), rho_u = kappa*ln(1 + C) on the annualized premium
    rho = kappa*ln(F/S). Their "random-maturity arbitrage" opens when rho leaves the band and closes
    when rho first returns to zero.

    Ackerer, Hugonnier & Jermann, "Perpetual Futures Pricing" (arXiv:2310.11771; Mathematical Finance):
    the perpetual price is the risk-neutral expected spot price at a random (exponential, rate kappa)
    date, i.e. a kappa*exp(-kappa*s)-weighted average of dated futures prices. `perp_price_random_maturity`
    evaluates that representation numerically; with E[S_{t+s}] = S_t*exp((r-r')s) it reproduces Prop. 1.

Binance mechanics (support article 360033525031): F = [avg premium + clamp(I - avg premium, ±0.05%)]
    / (8/N), I = 0.01% per 8h, capped at ±0.75 x maintenance-margin ratio for majors (±0.30% BTC/ETH).
    The 0.01% interest component is the exchange's assumed (USDT - coin) rate differential per period;
    the theory above replaces it with the observed (r - r')/kappa.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from basis_arb.config import (
    BINANCE_FUNDING_CAP_MAJORS,
    BINANCE_FUNDING_DAMPER,
    BINANCE_INTEREST_PER_PERIOD,
    DAYS_PER_YEAR,
    KAPPA,
)

_trapezoid = getattr(np, "trapezoid", None) or getattr(np, "trapz")


def _ret(x):
    """Return a python float for 0-d results, otherwise the array."""
    arr = np.asarray(x, dtype=float)
    return float(arr) if arr.ndim == 0 else arr


# --- dated futures ----------------------------------------------------------------------------
def cost_of_carry_price(spot, r, y, tau_years):
    """F*(t,T) = S * exp((r - y) * tau)."""
    return _ret(
        np.asarray(spot, float)
        * np.exp((np.asarray(r, float) - np.asarray(y, float)) * np.asarray(tau_years, float))
    )


def annualized_basis(fut, spot, tau_years):
    """Continuously-compounded annualized basis ln(F/S)/tau; NaN where tau <= 0."""
    tau = np.asarray(tau_years, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log(np.asarray(fut, float) / np.asarray(spot, float)) / tau
    return _ret(np.where(tau > 0, out, np.nan))


def fair_basis_ann(r, y=0.0):
    """b* = r - y (annualized, continuously compounded)."""
    return _ret(np.asarray(r, float) - np.asarray(y, float))


def excess_basis_ann(fut, spot, tau_years, r, y=0.0):
    """Market annualized basis minus the cost-of-carry fair basis: the tradable 'carry premium'."""
    return _ret(np.asarray(annualized_basis(fut, spot, tau_years)) - np.asarray(fair_basis_ann(r, y)))


# --- perpetual futures (He-Manela-Ross-von Wachter / Ackerer-Hugonnier-Jermann) ---------------
def perp_fair_price(spot, r, r_prime=0.0, kappa: float = KAPPA):
    """Prop. 1: F* = kappa / (kappa - (r - r')) * S. Requires kappa > r - r'."""
    spread = np.asarray(r, float) - np.asarray(r_prime, float)
    if np.any(kappa <= spread):
        raise ValueError("perp_fair_price requires kappa > r - r'")
    return _ret(kappa / (kappa - spread) * np.asarray(spot, float))


def fair_perp_premium_ann(r, r_prime=0.0, kappa: float = KAPPA):
    """Annualized fair log-premium rho* = kappa*ln(F*/S) = kappa*ln(kappa/(kappa - (r - r'))) ~ r - r'."""
    spread = np.asarray(r, float) - np.asarray(r_prime, float)
    return _ret(kappa * np.log(kappa / (kappa - spread)))


def fair_funding_per_period(r, r_prime=0.0, kappa: float = KAPPA):
    """Equilibrium funding a short collects per period: (F* - S)/S = (r - r')/(kappa - (r - r')) ~ (r - r')/kappa."""
    spread = np.asarray(r, float) - np.asarray(r_prime, float)
    return _ret(spread / (kappa - spread))


def fair_funding_ann(r, r_prime=0.0, kappa: float = KAPPA):
    """Annualized equilibrium funding = kappa x per-period fair funding (~ r - r' to first order)."""
    return _ret(kappa * np.asarray(fair_funding_per_period(r, r_prime, kappa), float))


def perp_premium_ann(perp, spot, kappa: float = KAPPA):
    """Annualized market premium rho = kappa*ln(P/S) (HMRW's deviation measure before subtracting rho*)."""
    return _ret(kappa * np.log(np.asarray(perp, float) / np.asarray(spot, float)))


def no_arb_band_ann(round_trip_cost_frac: float, kappa: float = KAPPA) -> tuple[float, float]:
    """Prop. 2 annualized band (rho_l, rho_u) = (kappa*ln(1 - C), kappa*ln(1 + C)) around the fair premium."""
    c = float(round_trip_cost_frac)
    if not 0.0 <= c < 1.0:
        raise ValueError("round-trip cost fraction must be in [0, 1)")
    return float(kappa * np.log1p(-c)), float(kappa * np.log1p(c))


def perp_price_random_maturity(
    expected_spot: Callable[[np.ndarray], np.ndarray],
    kappa: float = KAPPA,
    horizon_years: float = 0.1,
    n_steps: int = 200_001,
) -> float:
    """F_t = kappa * Int_0^inf exp(-kappa*s) E_t[S_{t+s}] ds, evaluated numerically (AHJ representation).

    `expected_spot(s)` maps an array of horizons s (years) to E_t[S_{t+s}]. Beyond `horizon_years`
    the expectation is held flat at its last value (the weight there is exp(-kappa*H), ~0 for kappa=1095).
    """
    s = np.linspace(0.0, horizon_years, n_steps)
    w = kappa * np.exp(-kappa * s)
    vals = np.asarray(expected_spot(s), float)
    integral = _trapezoid(w * vals, s)
    tail = np.exp(-kappa * horizon_years) * vals[-1]
    return float(integral + tail)


# --- exchange mechanics ------------------------------------------------------------------------
def binance_funding_rate(
    avg_premium_index,
    interest_rate_per_period: float = BINANCE_INTEREST_PER_PERIOD,
    damper: float = BINANCE_FUNDING_DAMPER,
    cap: float = BINANCE_FUNDING_CAP_MAJORS,
    floor: float | None = None,
    interval_hours: int = 8,
):
    """Binance USDⓈ-M funding formula: [P_avg + clamp(I - P_avg, ±damper)] / (8/N), then capped."""
    p = np.asarray(avg_premium_index, float)
    f = p + np.clip(interest_rate_per_period - p, -damper, damper)
    f = f / (8.0 / interval_hours)
    lo = -cap if floor is None else floor
    return _ret(np.clip(f, lo, cap))


# --- trailing / amortization helpers ----------------------------------------------------------
def trailing_funding_ann(funding_at_stamps: pd.Series, lookback_prints: int, kappa: float = KAPPA) -> pd.Series:
    """Trailing mean of the last `lookback_prints` funding prints, annualized by kappa.

    Input must be indexed at funding stamps only (no NaN rows); the value at stamp t uses prints <= t.
    """
    if lookback_prints < 1:
        raise ValueError("lookback_prints must be >= 1")
    min_periods = max(1, lookback_prints // 3)
    return funding_at_stamps.rolling(lookback_prints, min_periods=min_periods).mean() * kappa


def amortized_cost_ann(round_trip_cost_frac, horizon_days):
    """Round-trip cost expressed as an annualized hurdle over the expected holding horizon."""
    h = np.maximum(np.asarray(horizon_days, float), 1e-9)
    return _ret(np.asarray(round_trip_cost_frac, float) * DAYS_PER_YEAR / h)


def calendar_excess_ann(basis_fut_ann, expected_funding_ann):
    """Quarterly basis minus expected cumulative funding (both annualized): the perp-vs-quarterly mispricing.

    Under no-arbitrage both are the cost of a synthetic long: holding the quarterly costs the basis,
    holding the perpetual costs the funding stream. A positive excess favors long perp / short quarterly.
    """
    return _ret(np.asarray(basis_fut_ann, float) - np.asarray(expected_funding_ann, float))
