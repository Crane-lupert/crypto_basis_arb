# Backtest report: ETHUSDT

Backtest (simulated taker fills on historical hourly bars), not paper or live results.
Sample 2020-01-01T00:00:00+00:00 to 2026-10-17T20:00:00+00:00 (59,565 hourly bars, 6.80 years), panel `data\panel_ETHUSDT_1h.parquet`.
Capital unit: 10,000 USDT per leg, equal coin quantity on both legs. All figures are net of fees, slippage, delivery fees and liquidation penalties.

## Headline

| Strategy | Ann. return on capital | Sharpe | Max DD | Round trips | Hit rate | Avg hold (h) | Time in position | Turnover (x/yr) | Worst 30d | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.59% | 0.17 | 10.16% | 15 | 67% | 1,443 | 36% | 6.6 | -5.79% | 6 |
| S2 funding_carry | 0.74% | 0.64 | 3.23% | 4 | 75% | 2,333 | 16% | 1.9 | -2.60% | 3 |
| S3 calendar | -0.00% | -0.00 | 17.37% | 33 | 36% | 461 | 26% | 20.0 | -6.30% | 1 |
| S4 random_maturity | 2.17% | 1.38 | 3.21% | 9 | 89% | 211 | 3% | 3.7 | -2.81% | 1 |

Ann. return = total P&L / capital / years. Worst 30d = worst rolling 30-day P&L / capital. Turnover = traded notional / capital per year. Round trips exclude a position still open on the last bar.

## P&L decomposition (USDT, whole sample)

| Strategy | Capital | Basis convergence | Funding | Fees + slippage | Delivery fees | Liquidation penalties | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 15,000 | 1,711.73 | 0.00 | -628.04 | 0.00 | -481.72 | 601.96 |
| S2 funding_carry | 15,000 | -398.90 | 1,563.78 | -180.73 | 0.00 | -224.62 | 759.54 |
| S3 calendar | 10,000 | -1,000.68 | 2,028.94 | -954.24 | 0.00 | -74.79 | -0.76 |
| S4 random_maturity | 15,000 | 166.58 | 2,477.83 | -356.00 | 0.00 | -75.10 | 2,213.30 |

## Margin

| Strategy | Margin mode | Leverage | Peak utilization | Bars above isolated threshold | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | isolated | fut 2x | 1.035 | 6 | 6 |
| S2 funding_carry | isolated | perp 2x | 1.017 | 3 | 3 |
| S3 calendar | isolated | perp 2x, fut 2x | 1.016 | 1 | 1 |
| S4 random_maturity | isolated | perp 2x | 1.100 | 1 | 1 |

## Year by year

Bars are labeled by close time, so the bar closing at 00:00 UTC on 1 January belongs to the previous year (a sample starting at 00:00 shows a one-bar row for that year). Round trips are counted in the year they close; costs = fees + slippage + delivery fees + liquidation penalties.

### S1 cash_and_carry

| Year | Days | P&L (USDT) | Return | Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2020 | 366 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2021 | 365 | -848.69 | -5.66% | -0.74 | 10.16% | -362.85 | 0.00 | -485.84 | 5 | 52% | 3 |
| 2022 | 365 | 348.49 | 2.32% | 2.57 | 0.69% | 389.56 | 0.00 | -41.07 | 2 | 41% | 0 |
| 2023 | 365 | 13.98 | 0.09% | 0.09 | 1.18% | 85.81 | 0.00 | -71.83 | 1 | 28% | 0 |
| 2024 | 366 | 768.89 | 5.13% | 1.29 | 3.67% | 1,112.32 | 0.00 | -343.43 | 4 | 86% | 2 |
| 2025 | 365 | 319.29 | 2.13% | 2.02 | 1.09% | 486.88 | 0.00 | -167.60 | 3 | 40% | 1 |
| 2026 | 290 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |

### S2 funding_carry

| Year | Days | P&L (USDT) | Return | Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2020 | 366 | -95.98 | -0.64% | -0.23 | 3.11% | -359.74 | 385.77 | -122.01 | 1 | 4% | 1 |
| 2021 | 365 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2022 | 365 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2023 | 365 | 696.74 | 4.64% | 10.22 | 1.86% | 20.65 | 734.00 | -57.90 | 1 | 78% | 0 |
| 2024 | 366 | 158.77 | 1.06% | 0.85 | 0.94% | -59.81 | 444.01 | -225.43 | 2 | 25% | 2 |
| 2025 | 365 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2026 | 290 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |

### S3 calendar

| Year | Days | P&L (USDT) | Return | Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2020 | 366 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2021 | 365 | 1,035.50 | 10.36% | 0.81 | 8.47% | -595.61 | 2,055.30 | -424.19 | 12 | 53% | 1 |
| 2022 | 365 | -318.83 | -3.19% | -1.90 | 3.34% | 84.82 | -160.03 | -243.61 | 8 | 37% | 0 |
| 2023 | 365 | 260.59 | 2.61% | 1.92 | 1.03% | -0.86 | 351.46 | -90.01 | 3 | 28% | 0 |
| 2024 | 366 | -1,081.44 | -10.81% | -2.18 | 11.25% | -667.93 | -197.81 | -215.70 | 8 | 41% | 0 |
| 2025 | 365 | 77.62 | 0.78% | 1.47 | 0.41% | 134.98 | -31.11 | -26.24 | 1 | 9% | 0 |
| 2026 | 290 | 25.79 | 0.26% | 0.77 | 0.20% | 43.92 | 11.13 | -29.26 | 1 | 8% | 0 |

### S4 random_maturity

| Year | Days | P&L (USDT) | Return | Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2020 | 366 | 829.78 | 5.53% | 4.82 | 0.78% | 87.47 | 823.46 | -81.14 | 2 | 10% | 0 |
| 2021 | 365 | 1,301.22 | 8.67% | 2.25 | 3.21% | -40.42 | 1,653.36 | -311.72 | 6 | 11% | 1 |
| 2022 | 365 | 82.30 | 0.55% | 0.82 | 0.12% | 119.54 | 1.01 | -38.25 | 1 | 0% | 0 |
| 2023 | 365 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2024 | 366 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2025 | 365 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |
| 2026 | 290 | 0.00 | 0.00% | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 |

## Stress episodes

### S1 cash_and_carry

| Episode | Window (UTC days) | Coverage | P&L (USDT) | Return | Max DD | Basis | Funding | Costs | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | -96.53 | -0.64% | 1.93% | 63.91 | 0.00 | -160.44 | 68% | 1 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 140.38 | 0.94% | 0.47% | 174.22 | 0.00 | -33.84 | 100% | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |

### S2 funding_carry

| Episode | Window (UTC days) | Coverage | P&L (USDT) | Return | Max DD | Basis | Funding | Costs | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |

### S3 calendar

| Episode | Window (UTC days) | Coverage | P&L (USDT) | Return | Max DD | Basis | Funding | Costs | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | 99.17 | 0.99% | 0.63% | 190.36 | -7.68 | -83.51 | 9% | 0 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | -11.02 | -0.11% | 0.84% | 25.04 | -9.41 | -26.64 | 36% | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 25.79 | 0.26% | 0.20% | 43.92 | 11.13 | -29.26 | 19% | 0 |

### S4 random_maturity

| Episode | Window (UTC days) | Coverage | P&L (USDT) | Return | Max DD | Basis | Funding | Costs | Time in position | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 0.00 | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 | 0% | 0 |

## Sensitivity grid

Mode `small`: **N_configs = 11** cells per strategy x 4 strategies. Discount the best cell accordingly; the full grid is in `sensitivity.md` / `sensitivity.csv`.

| Strategy | Base Sharpe | Median Sharpe | Min Sharpe | Max Sharpe | Base ann. return | Median ann. return | Min ann. return | Max ann. return | Cells with return > 0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.17 | 0.17 | -0.01 | 0.78 | 0.59% | 0.59% | -0.04% | 2.60% | 10/11 |
| S2 funding_carry | 0.64 | 0.64 | 0.30 | 4.44 | 0.74% | 0.77% | 0.38% | 3.32% | 11/11 |
| S3 calendar | -0.00 | 0.04 | -0.06 | 0.65 | -0.00% | 0.22% | -0.35% | 3.51% | 7/11 |
| S4 random_maturity | 1.38 | 1.38 | 1.38 | 2.93 | 2.17% | 2.17% | 2.05% | 4.75% | 11/11 |

## Figures

![Cumulative return on capital](equity_curves.png)

Cumulative P&L divided by each strategy's capital unit, hourly, net of all costs.

![Drawdown](drawdown.png)

Drawdown from the running equity peak; one panel per strategy, worst value of each UTC day.

![Theory vs market](theory_vs_market.png)

Top: annualized current-quarter basis b = ln(F/S)/tau vs the cost-of-carry fair basis b* = r - y (daily means; bars inside the 2-day roll window are masked because ln(F/S)/tau explodes as tau -> 0). Middle: trailing funding f-hat vs HMRW equilibrium funding f*. Bottom: HMRW premium deviation rho - rho* (hourly and daily mean) with the Prop. 2 no-arbitrage band; S4 enters when the hourly value is above the upper edge.

## Assumptions

### Configuration used

- Notional per leg: 10,000 USDT; costs: spot fee 10 bp, futures fee 5 bp, slippage 2 bp per leg, delivery fee 5 bp, liquidation penalty 50 bp.
- Margin: mode isolated, perp leverage 2x, quarterly leverage 2x, maintenance 0.40%.
- Strategy parameters: {'cc_entry_buffer_ann': 0.03, 'cc_exit_excess_ann': 0.0, 'cc_roll_days': 2.0, 'cc_hold_to_expiry': False, 'fc_lookback_prints': 90, 'fc_entry_buffer_ann': 0.03, 'fc_exit_excess_ann': 0.0, 'fc_expected_hold_days': 30.0, 'cal_entry_buffer_ann': 0.03, 'cal_exit_excess_ann': 0.0, 'cal_roll_days': 2.0, 'cal_hold_to_expiry': False, 'cal_allow_reverse': True, 'rm_exit_dev_ann': 0.0, 'min_hold_bars': 8, 'allow_reverse': False}

### Engine implementation choices

- Capital unit per strategy: spot leg = notional; each futures leg = notional / its leverage (spec: spot-leg strategies notional x (1 + 1/leverage), S3 2 x notional / leverage). Returns are P&L / capital, not compounded; idle cash earns nothing.
- Equal coin quantity on both legs: qty = notional_per_leg / close of the first leg at the entry bar (spot for S1/S2/S4, perpetual for S3).
- Fills at close x (1 +/- slippage); fees are charged on the executed notional. The P&L 'basis' component is the mark-to-market at reference (close) prices, so slippage appears under fees.
- Bar order: mark, settle expiries, funding, margin monitor, trade. Funding is paid to perp legs held before the bar's trades (a leg opened on a stamp bar does not receive that stamp; a leg closed on a stamp bar does).
- Expiry: a held quarterly with expiry <= ts settles at spot_close with the delivery fee; the remaining leg is closed on the same bar at close +/- slippage (round trip exit_reason EXPIRY).
- Margin monitor: worst adverse close/high/low move since entry, excluding the entry bar's own range. Isolated mode liquidates a leg whose move exceeds 1/leverage - maintenance at that trigger price (+/- slippage, taker fee) plus the liquidation penalty; the other leg is closed at close +/- slippage. Utilization = adverse move / (1/leverage - maintenance): 1.0 is the isolated liquidation point. Cross-collateral mode never liquidates and reports peak utilization.
- After a forced close (expiry or liquidation) the engine stays flat until the target's (direction, contract) pair changes; it never re-enters on the bar of the forced close.
- A roll closes the old quarterly and opens the next one (same coin quantity) on the same bar; the spot/perp leg is untouched and the round trip continues (contract column 'A>B').
- A futures leg without a price on a bar keeps its last mark (closing at it logs STALE_PRICE); an entry or roll into an unpriced contract is skipped (NO_PRICE) and retried on the next bar.
- Positions open on the last bar are marked to market without exit costs (trades exit_reason OPEN; excluded from round-trip counts and hit rate).
- Daily P&L by UTC day; the bar closing at 00:00 UTC belongs to the previous day. Sharpe = mean/std x sqrt(365) on daily P&L. Max drawdown is a fraction of the running equity peak.

### Pre-registered spec excerpt (docs/strategy_spec.md, sections 4 and 6, verbatim)

#### 4. Costs and hurdles

Per-leg cost = fee + slippage, in bp of leg notional: spot 10 + 2, futures 5 + 2 (Binance VIP0, taker,
no BNB discount). Round trip for a two-leg position = 2 × (leg₁ + leg₂):

| Pair | Round trip `C` |
|---|---|
| spot + futures (S1, S2, S4) | 38 bp |
| perp + quarterly (S3) | 28 bp |

Hurdle (annualized) = `C × 365 / H + buffer`, where `H` is the expected holding horizon in days and
`buffer = 3%/yr`:

* S1/S3: `H = days to expiry` of the contract considered (the hurdle rises mechanically as expiry nears).
* S2: `H = 30` days (fixed, matches the trailing-funding lookback).
* S4: the hurdle is HMRW's band edge `κ·ln(1 + C)` (a one-off cost expressed in annualized-premium units); no buffer.

Delivery fee 5 bp on a quarterly carried into settlement. Liquidation penalty 50 bp (isolated mode only).

#### 6. Backtest engine semantics (backtest/engine.py)

Event loop over the 1h panel, one strategy at a time, equal coin quantity on both legs:

1. **Mark**: equity = cash + spot_qty × spot_close + Σ fut_qty × (close − entry_price).
2. **Settle expiries**: a held quarterly with `expiry <= ts` settles at `spot_close` of that bar (proxy for
   Binance's index average over the final 30 minutes) and pays the delivery fee, regardless of the target.
3. **Funding**: at a funding stamp, perp positions held *before* this bar's trades receive
   `−qty × funding_rate × perp_close` (a short with positive funding receives cash). Positions opened at
   the stamp bar do not receive that stamp's funding.
4. **Margin monitor** (futures legs): track the worst adverse close/high/low move since entry. Isolated
   mode: if it exceeds `1/leverage − maintenance` the leg is liquidated at that bar, the other leg is
   closed, the penalty is charged, and the event is logged. Cross-collateral mode: no liquidation; the
   peak utilization is reported.
5. **Trade**: if `target` or `contract` changed, close/open legs at `close ± slippage` and pay fees.
   A roll closes the old contract and opens the new one on the same bar; the spot leg is untouched.
6. **Capital** for return computation: spot-leg strategies `notional × (1 + 1/leverage)`; futures-only
   (S3) `2 × notional / leverage`. Idle cash earns nothing (conservative; `r` is already inside the
   fair-value hurdle).

P&L decomposition per strategy: basis convergence (mark-to-market of the two legs), funding received or
paid, fees + slippage, delivery fees, liquidation penalties.
