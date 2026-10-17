# Sensitivity grid: BTCUSDT

Backtest results. This grid is the only sanctioned variation of the pre-registered configuration (docs/strategy_spec.md section 8); every cell is reported, in grid order, not ranked. Headline metrics are excess of the T-bill; raw figures are in sensitivity.csv.

- Mode `small`; axes: buffer [0.0, 0.03, 0.06, 0.1]; lookback [30, 90, 180]; leverage [1.0, 2.0, 3.0]; hold_to_expiry [False, True]; fill_lag [0, 1]; costs ['VIP0', 'VIP0_BNB', 'VIP3']
- **N_configs = 12** grid cells per strategy x 4 strategies, plus 3 HMRW replication cells for funding_carry, random_maturity (54 backtests, 45 distinct engine runs after de-duplicating cells a strategy's rule cannot distinguish).
- Pre-registered base cell (marked **base**): buffer = 0.03, lookback = 90, leverage = 2.0, hold_to_expiry = False, fill_lag = 0, costs = VIP0
- Cost tiers: VIP0 = spot 10 / futures 5 / slippage 2 / delivery 5 bp; VIP0_BNB = spot 7.5 / futures 4.5 / slippage 2 / delivery 4.5 bp; VIP3 = spot 6 / futures 3.2 / slippage 2 / delivery 3.2 bp; HMRW-low = spot 2.25 / futures 0.18 / slippage 0 / delivery 0.18 bp; HMRW-mid = spot 4.5 / futures 0.72 / slippage 0 / delivery 0.72 bp; HMRW-high = spot 6.75 / futures 1.44 / slippage 0 / delivery 1.44 bp
- Axis relevance: buffer moves the S1-S3 entry hurdles (S4's hurdle is the HMRW band edge, no buffer); lookback moves S2 and S3 (trailing funding window = lookback x 8h); hold_to_expiry sets both cc_hold_to_expiry (S1) and cal_hold_to_expiry (S3); leverage moves the capital and the liquidation threshold of every futures leg; fill_lag delays every fill by that many bars; costs move hurdles and execution costs of all strategies.

## Distribution across grid cells (excess of the T-bill)

| Strategy | Base excess Sharpe | Median | Min | Max | Base excess ann. return | Median | Min | Max | Cells with excess return > 0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.26 | 0.26 | -0.22 | 0.38 | 0.91% | 0.91% | -0.65% | 1.41% | 10/12 |
| S2 funding_carry | 3.14 | 3.14 | 2.02 | 3.56 | 2.77% | 2.77% | 1.36% | 3.47% | 12/12 |
| S3 calendar | -0.37 | -0.37 | -1.06 | -0.07 | -1.55% | -1.54% | -2.85% | -0.27% | 0/12 |
| S4 random_maturity | -2.24 | -2.24 | -3.74 | -0.48 | -1.36% | -1.36% | -1.74% | -0.34% | 0/12 |

## HMRW replication cells

| Strategy | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Round trips | Time in position | Liquidations |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| S2 funding_carry | VIP0 (base cell) | 2.77% | 3.14 | 6.68 | 5 | 45.3% | 0 |
| S2 funding_carry | HMRW-low | 3.67% | 4.22 | 7.83 | 9 | 58.3% | 0 |
| S2 funding_carry | HMRW-mid | 3.50% | 4.01 | 7.61 | 8 | 56.0% | 0 |
| S2 funding_carry | HMRW-high | 3.39% | 3.87 | 7.45 | 8 | 55.0% | 0 |
| S4 random_maturity | VIP0 (base cell) | -1.36% | -2.24 | 2.59 | 8 | 2.1% | 0 |
| S4 random_maturity | HMRW-low | 3.29% | 3.66 | 7.15 | 157 | 22.1% | 0 |
| S4 random_maturity | HMRW-mid | 2.36% | 2.74 | 6.34 | 80 | 16.0% | 0 |
| S4 random_maturity | HMRW-high | 1.37% | 1.68 | 5.43 | 39 | 11.3% | 0 |

## Full grid

### S1 cash_and_carry

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | 0.91% | 0.26 | 1.06 | 6.45% | 12 | 33% | 0 | 13 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | 1.29% | 0.36 | 1.16 | 6.45% | 24 | 46% | 0 | 12 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | 0.84% | 0.24 | 1.05 | 6.45% | 11 | 27% | 0 | 10 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | 0.34% | 0.10 | 0.92 | 6.45% | 10 | 16% | 0 | 7 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | 0.91% | 0.26 | 1.06 | 6.45% | 12 | 33% | 0 | 13 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | 0.91% | 0.26 | 1.06 | 6.45% | 12 | 33% | 0 | 13 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | -0.07% | -0.02 | 1.06 | 4.91% | 12 | 33% | 0 | 13 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | 1.41% | 0.35 | 1.06 | 7.20% | 12 | 33% | 0 | 13 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | 0.90% | 0.25 | 1.06 | 6.45% | 15 | 33% | 0 | 12 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | -0.65% | -0.22 | 0.75 | 7.03% | 12 | 33% | 0 | 13 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | 1.25% | 0.35 | 1.15 | 6.44% | 15 | 36% | 0 | 13 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | 1.37% | 0.38 | 1.18 | 6.44% | 17 | 37% | 0 | 12 |  |

### S2 funding_carry

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | 2.77% | 3.14 | 6.68 | 2.00% | 5 | 45% | 0 | 22 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | 3.17% | 3.56 | 7.06 | 2.01% | 8 | 55% | 0 | 23 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | 2.56% | 2.90 | 6.44 | 2.19% | 5 | 44% | 0 | 22 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | 2.25% | 2.61 | 6.20 | 2.07% | 5 | 40% | 0 | 17 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | 2.77% | 3.09 | 6.55 | 2.21% | 10 | 40% | 0 | 24 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | 2.34% | 2.80 | 6.50 | 2.41% | 4 | 51% | 0 | 24 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | 1.36% | 2.02 | 6.67 | 1.52% | 5 | 45% | 0 | 22 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | 3.47% | 3.53 | 6.68 | 2.24% | 5 | 45% | 0 | 22 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | 2.77% | 3.14 | 6.68 | 2.00% | 5 | 45% | 0 | 22 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | 2.78% | 3.18 | 6.73 | 1.97% | 5 | 45% | 0 | 22 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | 2.88% | 3.28 | 6.83 | 1.95% | 5 | 46% | 0 | 22 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | 3.03% | 3.44 | 7.00 | 1.95% | 6 | 49% | 0 | 24 |  |
| 12 | replication | 3% | 90 | 2 | False | 0 | HMRW-low | 3.67% | 4.22 | 7.83 | 1.90% | 9 | 58% | 0 | 21 |  |
| 13 | replication | 3% | 90 | 2 | False | 0 | HMRW-mid | 3.50% | 4.01 | 7.61 | 1.90% | 8 | 56% | 0 | 23 |  |
| 14 | replication | 3% | 90 | 2 | False | 0 | HMRW-high | 3.39% | 3.87 | 7.45 | 1.90% | 8 | 55% | 0 | 17 |  |

### S3 calendar

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | -1.55% | -0.37 | 0.32 | 12.54% | 36 | 30% | 0 | 6 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | -2.71% | -0.62 | 0.03 | 18.98% | 75 | 59% | 0 | 9 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | -1.40% | -0.35 | 0.36 | 11.32% | 17 | 16% | 0 | 4 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | -1.02% | -0.26 | 0.47 | 9.37% | 10 | 10% | 0 | 2 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | -0.27% | -0.07 | 0.66 | 10.90% | 52 | 34% | 0 | 6 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | -1.53% | -0.37 | 0.32 | 11.68% | 26 | 28% | 0 | 12 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | -2.21% | -1.06 | 0.32 | 6.89% | 36 | 30% | 0 | 6 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | -0.90% | -0.14 | 0.32 | 17.24% | 36 | 30% | 0 | 6 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | -1.55% | -0.37 | 0.32 | 12.76% | 36 | 29% | 0 | 6 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | -2.85% | -0.75 | 0.00 | 17.07% | 36 | 30% | 0 | 6 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | -1.55% | -0.37 | 0.32 | 12.35% | 36 | 31% | 0 | 6 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | -1.32% | -0.32 | 0.37 | 11.55% | 40 | 35% | 0 | 6 |  |

### S4 random_maturity

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | -1.36% | -2.24 | 2.59 | 1.32% | 8 | 2% | 0 | 1 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | -1.36% | -2.24 | 2.59 | 1.32% | 8 | 2% | 0 | 1 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | -1.36% | -2.24 | 2.59 | 1.32% | 8 | 2% | 0 | 1 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | -1.36% | -2.24 | 2.59 | 1.32% | 8 | 2% | 0 | 1 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | -1.36% | -2.24 | 2.59 | 1.32% | 8 | 2% | 0 | 1 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | -1.36% | -2.24 | 2.59 | 1.32% | 8 | 2% | 0 | 1 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | -1.74% | -3.74 | 2.59 | 1.00% | 8 | 2% | 0 | 1 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | -1.17% | -1.72 | 2.59 | 1.48% | 8 | 2% | 0 | 1 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | -1.36% | -2.24 | 2.59 | 1.32% | 8 | 2% | 0 | 1 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | -1.52% | -2.01 | 1.83 | 1.20% | 8 | 2% | 0 | 1 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | -1.23% | -1.95 | 2.71 | 1.28% | 9 | 3% | 0 | 1 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | -0.34% | -0.48 | 3.67 | 1.21% | 18 | 6% | 0 | 1 |  |
| 12 | replication | 3% | 90 | 2 | False | 0 | HMRW-low | 3.29% | 3.66 | 7.15 | 1.48% | 157 | 22% | 0 | 6 |  |
| 13 | replication | 3% | 90 | 2 | False | 0 | HMRW-mid | 2.36% | 2.74 | 6.34 | 1.47% | 80 | 16% | 0 | 5 |  |
| 14 | replication | 3% | 90 | 2 | False | 0 | HMRW-high | 1.37% | 1.68 | 5.43 | 1.50% | 39 | 11% | 0 | 4 |  |
