"""Frozen configuration: constants, cost model, margin model, strategy parameters.

All strategy thresholds are *derived from cost arithmetic* (round-trip cost amortized over the
expected holding period) plus a fixed buffer. They are pre-registered in docs/strategy_spec.md
and must not be tuned on the full-sample backtest; the sensitivity grid in the report is the
only sanctioned variation and is reported in full.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- market constants -----------------------------------------------------------------------
KAPPA = 1095.0
"""Funding periods per year at the 8-hour cadence (3 x 365). He, Manela, Ross & von Wachter
(arXiv:2212.06888) use the same kappa when writing the perpetual no-arbitrage price
F = kappa / (kappa - (r - r')) * S."""

FUNDING_INTERVAL_HOURS = 8
FUNDING_STAMP_HOURS_UTC = (0, 8, 16)
DAYS_PER_YEAR = 365.0
HOURS_PER_YEAR = 24.0 * DAYS_PER_YEAR

# Binance USDⓈ-M funding mechanics (support article 360033525031, read 2026-10-18)
BINANCE_INTEREST_PER_PERIOD = 0.0001  # 0.01% per 8h = 0.03%/day: the exchange's assumed USDT-coin rate differential
BINANCE_FUNDING_DAMPER = 0.0005  # ±0.05% clamp on (interest - premium)
BINANCE_FUNDING_CAP_MAJORS = 0.003  # BTC/ETH: 0.75 x maintenance margin ratio at max leverage (0.4%) = 0.30%

# Quarterly delivery contracts: ROOT_YYMMDD, expiry 08:00 UTC on the last Friday of Mar/Jun/Sep/Dec
DELIVERY_HOUR_UTC = 8
QUARTER_MONTHS = (3, 6, 9, 12)


# --- cost model -----------------------------------------------------------------------------
@dataclass(frozen=True)
class CostModel:
    """Per-leg execution costs in basis points of leg notional (Binance VIP0, no BNB discount)."""

    spot_fee_bps: float = 10.0  # spot taker
    fut_fee_bps: float = 5.0  # USDⓈ-M taker (perpetual and quarterly)
    slippage_bps: float = 2.0  # half-spread + impact for BTC/ETH majors at ~10k USDT clips
    delivery_fee_bps: float = 5.0  # charged on a quarterly position carried into settlement (conservative = taker)
    liquidation_penalty_bps: float = 50.0  # extra haircut applied on a forced liquidation (isolated margin only)

    def leg_cost_frac(self, leg: str) -> float:
        fee = self.spot_fee_bps if leg == "spot" else self.fut_fee_bps
        return (fee + self.slippage_bps) / 1e4

    def round_trip_frac(self, legs: tuple[str, ...]) -> float:
        """Open + close every leg, as a fraction of ONE leg's notional (legs are equal-notional)."""
        return 2.0 * sum(self.leg_cost_frac(leg) for leg in legs)


# --- margin model ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarginModel:
    """Leverage and liquidation assumptions for the futures legs.

    mode = "isolated": each futures leg is margined on its own; a short leg is liquidated when the
    adverse move since entry exceeds (1/leverage - maintenance). Spot gains are NOT collateral.
    mode = "cross_collateral": the spot leg (or the other futures leg) counts as collateral
    (Binance multi-assets / portfolio margin); no liquidation, but margin utilization is tracked.
    """

    perp_leverage: float = 2.0
    fut_leverage: float = 2.0
    maintenance_margin_frac: float = 0.004  # BTC/ETH USDⓈ-M tier-1 maintenance margin ratio
    mode: str = "isolated"

    def liquidation_move(self, leg: str) -> float:
        lev = self.perp_leverage if leg == "perp" else self.fut_leverage
        return 1.0 / lev - self.maintenance_margin_frac


# --- rate inputs ----------------------------------------------------------------------------
@dataclass(frozen=True)
class RateInputs:
    """r = USD/stablecoin financing rate (FRED DTB3, decimal). y = r' = coin yield (staking proxy)."""

    coin_yield: dict[str, float] = field(default_factory=lambda: {"BTC": 0.0, "ETH": 0.03})
    fallback_r_usd: float = 0.04  # used only if the FRED series cannot be loaded (logged loudly)


# --- strategy parameters (pre-registered) ---------------------------------------------------
@dataclass(frozen=True)
class StrategyParams:
    # S1 cash-and-carry: long spot / short quarterly
    cc_entry_buffer_ann: float = 0.03  # excess basis must exceed amortized cost + 3%/yr
    cc_exit_excess_ann: float = 0.0  # exit once market basis <= fair basis
    cc_roll_days: float = 2.0  # roll/close when days-to-expiry < 2 (unless hold_to_expiry)
    cc_hold_to_expiry: bool = False
    # S2 funding carry: long spot / short perpetual
    fc_lookback_prints: int = 90  # lookback in 8h-print units (90 = 30 days); applied as a calendar window of
    # prints x 8h so that 4h / 1h funding regimes are covered by the same 30 days of history
    fc_entry_buffer_ann: float = 0.03
    fc_exit_excess_ann: float = 0.0
    fc_expected_hold_days: float = 30.0  # amortization horizon for the entry hurdle
    # S3 calendar: long perpetual / short quarterly (reverse allowed: both legs are futures)
    cal_entry_buffer_ann: float = 0.03
    cal_exit_excess_ann: float = 0.0
    cal_roll_days: float = 2.0
    cal_hold_to_expiry: bool = False
    cal_allow_reverse: bool = True
    # S4 random-maturity arbitrage (He-Manela-Ross-von Wachter): spot vs perpetual premium band
    rm_exit_dev_ann: float = 0.0  # close when the annualized deviation first returns to zero
    # common
    min_hold_bars: int = 8  # 8 x 1h: never flip inside one funding interval; counted from entry (rolls do not reset it)
    allow_reverse: bool = False  # reserved: reverse spot-leg trades need a borrow-cost model that v1 does not have,
    # so S1 / S2 / S4 ignore this flag (hard-coded off). S3 uses cal_allow_reverse.


@dataclass(frozen=True)
class BacktestConfig:
    notional_per_leg: float = 10_000.0  # USDT notional per leg (equal coin quantity on both legs)
    costs: CostModel = field(default_factory=CostModel)
    margin: MarginModel = field(default_factory=MarginModel)
    rates: RateInputs = field(default_factory=RateInputs)
    params: StrategyParams = field(default_factory=StrategyParams)
