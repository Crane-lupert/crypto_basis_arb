# Sensitivity grid: ETHUSDT

Backtest results. This grid is the only sanctioned variation of the pre-registered configuration (docs/strategy_spec.md section 8); every cell is reported, in grid order, not ranked. Headline metrics are excess of the T-bill; raw figures are in sensitivity.csv.

- Mode `small`; axes: buffer [0.0, 0.03, 0.06, 0.1]; lookback [30, 90, 180]; leverage [1.0, 2.0, 3.0]; hold_to_expiry [False, True]; fill_lag [0, 1]; costs ['VIP0', 'VIP0_BNB', 'VIP3']
- **N_configs = 12** grid cells per strategy x 4 strategies, plus 3 HMRW replication cells for funding_carry, random_maturity (54 backtests, 45 distinct engine runs after de-duplicating cells a strategy's rule cannot distinguish).
- Pre-registered base cell (marked **base**): buffer = 0.03, lookback = 90, leverage = 2.0, hold_to_expiry = False, fill_lag = 0, costs = VIP0
- Cost tiers: VIP0 = spot 10 / futures 5 / slippage 2 / delivery 5 bp; VIP0_BNB = spot 7.5 / futures 4.5 / slippage 2 / delivery 4.5 bp; VIP3 = spot 6 / futures 3.2 / slippage 2 / delivery 3.2 bp; HMRW-low = spot 2.25 / futures 0.18 / slippage 0 / delivery 0.18 bp; HMRW-mid = spot 4.5 / futures 0.72 / slippage 0 / delivery 0.72 bp; HMRW-high = spot 6.75 / futures 1.44 / slippage 0 / delivery 1.44 bp
- Axis relevance: buffer moves the S1-S3 entry hurdles (S4's hurdle is the HMRW band edge, no buffer); lookback moves S2 and S3 (trailing funding window = lookback x 8h); hold_to_expiry sets both cc_hold_to_expiry (S1) and cal_hold_to_expiry (S3); leverage moves the capital and the liquidation threshold of every futures leg; fill_lag delays every fill by that many bars; costs move hurdles and execution costs of all strategies.

## Distribution across grid cells (excess of the T-bill)

| Strategy | Base excess Sharpe | Median | Min | Max | Base excess ann. return | Median | Min | Max | Cells with excess return > 0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.07 | 0.07 | -0.22 | 0.17 | 0.24% | 0.24% | -0.60% | 0.65% | 8/12 |
| S2 funding_carry | 4.03 | 4.03 | 3.15 | 4.32 | 4.63% | 4.63% | 2.75% | 5.57% | 12/12 |
| S3 calendar | -0.35 | -0.36 | -0.95 | 0.12 | -1.67% | -1.68% | -2.26% | 0.57% | 1/12 |
| S4 random_maturity | -0.44 | -0.44 | -1.48 | 1.01 | -0.40% | -0.40% | -1.01% | 1.04% | 2/12 |

## HMRW replication cells

| Strategy | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Round trips | Time in position | Liquidations |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| S2 funding_carry | VIP0 (base cell) | 4.63% | 4.03 | 6.73 | 4 | 52.1% | 0 |
| S2 funding_carry | HMRW-low | 5.23% | 4.56 | 7.29 | 6 | 57.2% | 0 |
| S2 funding_carry | HMRW-mid | 5.11% | 4.46 | 7.18 | 5 | 55.7% | 0 |
| S2 funding_carry | HMRW-high | 4.97% | 4.34 | 7.06 | 4 | 53.7% | 0 |
| S4 random_maturity | VIP0 (base cell) | -0.40% | -0.44 | 2.81 | 10 | 3.2% | 0 |
| S4 random_maturity | HMRW-low | 5.22% | 3.95 | 6.30 | 255 | 25.5% | 0 |
| S4 random_maturity | HMRW-mid | 4.29% | 3.25 | 5.57 | 103 | 18.9% | 0 |
| S4 random_maturity | HMRW-high | 3.28% | 2.84 | 5.49 | 59 | 13.5% | 0 |

## Full grid

### S1 cash_and_carry

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | 0.24% | 0.07 | 0.90 | 6.52% | 14 | 32% | 0 | 16 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | 0.33% | 0.10 | 0.92 | 6.52% | 22 | 38% | 0 | 19 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | -0.17% | -0.05 | 0.79 | 6.52% | 9 | 25% | 0 | 17 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | -0.52% | -0.15 | 0.69 | 6.52% | 7 | 18% | 0 | 10 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | 0.24% | 0.07 | 0.90 | 6.52% | 14 | 32% | 0 | 16 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | 0.24% | 0.07 | 0.90 | 6.52% | 14 | 32% | 0 | 16 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | -0.56% | -0.22 | 0.90 | 4.94% | 14 | 32% | 0 | 16 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | 0.65% | 0.17 | 0.90 | 7.29% | 14 | 32% | 0 | 16 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | 0.10% | 0.03 | 0.87 | 6.07% | 18 | 32% | 0 | 15 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | -0.60% | -0.18 | 0.69 | 6.53% | 14 | 32% | 0 | 16 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | 0.37% | 0.11 | 0.93 | 6.51% | 15 | 32% | 0 | 16 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | 0.46% | 0.13 | 0.96 | 6.50% | 15 | 32% | 0 | 16 |  |

### S2 funding_carry

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | 4.63% | 4.03 | 6.73 | 1.25% | 4 | 52% | 0 | 43 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | 4.79% | 4.17 | 6.87 | 1.30% | 4 | 54% | 0 | 43 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | 4.31% | 3.76 | 6.46 | 1.00% | 3 | 46% | 0 | 39 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | 4.19% | 3.66 | 6.37 | 1.00% | 3 | 46% | 0 | 40 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | 4.45% | 3.82 | 6.47 | 1.17% | 9 | 46% | 0 | 37 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | 3.77% | 3.46 | 6.31 | 0.84% | 3 | 49% | 0 | 34 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | 2.75% | 3.15 | 6.73 | 1.02% | 4 | 52% | 0 | 43 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | 5.57% | 4.32 | 6.73 | 1.36% | 4 | 52% | 0 | 43 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | 4.63% | 4.03 | 6.73 | 1.25% | 4 | 52% | 0 | 43 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | 4.63% | 4.05 | 6.77 | 1.25% | 4 | 52% | 0 | 43 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | 4.73% | 4.13 | 6.84 | 1.27% | 4 | 53% | 0 | 43 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | 4.81% | 4.20 | 6.91 | 1.24% | 4 | 53% | 0 | 43 |  |
| 12 | replication | 3% | 90 | 2 | False | 0 | HMRW-low | 5.23% | 4.56 | 7.29 | 1.29% | 6 | 57% | 0 | 45 |  |
| 13 | replication | 3% | 90 | 2 | False | 0 | HMRW-mid | 5.11% | 4.46 | 7.18 | 1.31% | 5 | 56% | 0 | 43 |  |
| 14 | replication | 3% | 90 | 2 | False | 0 | HMRW-high | 4.97% | 4.34 | 7.06 | 1.29% | 4 | 54% | 0 | 41 |  |

### S3 calendar

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | -1.67% | -0.35 | 0.25 | 9.85% | 26 | 27% | 0 | 5 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | -1.97% | -0.40 | 0.18 | 13.06% | 91 | 58% | 0 | 19 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | -1.88% | -0.41 | 0.21 | 10.44% | 16 | 17% | 0 | 6 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | -1.77% | -0.39 | 0.24 | 9.42% | 13 | 14% | 0 | 4 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | 0.57% | 0.12 | 0.74 | 8.16% | 54 | 35% | 0 | 14 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | -1.70% | -0.37 | 0.25 | 11.33% | 23 | 30% | 0 | 15 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | -2.26% | -0.95 | 0.25 | 5.35% | 26 | 27% | 0 | 5 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | -1.07% | -0.15 | 0.25 | 13.69% | 26 | 27% | 0 | 5 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | -1.54% | -0.33 | 0.28 | 9.22% | 28 | 27% | 0 | 5 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | -2.25% | -0.47 | 0.13 | 11.89% | 26 | 27% | 0 | 5 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | -1.60% | -0.34 | 0.27 | 9.57% | 27 | 27% | 0 | 5 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | -1.30% | -0.27 | 0.33 | 10.51% | 32 | 30% | 0 | 7 |  |

### S4 random_maturity

| Cell | Kind | Buffer | Lookback | Leverage | Hold to expiry | Fill lag | Costs | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position | Liquidations | Resizes |  |
| ---: | --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | grid | 3% | 90 | 2 | False | 0 | VIP0 | -0.40% | -0.44 | 2.81 | 0.78% | 10 | 3% | 0 | 3 | **base** |
| 1 | grid | 0% | 90 | 2 | False | 0 | VIP0 | -0.40% | -0.44 | 2.81 | 0.78% | 10 | 3% | 0 | 3 |  |
| 2 | grid | 6% | 90 | 2 | False | 0 | VIP0 | -0.40% | -0.44 | 2.81 | 0.78% | 10 | 3% | 0 | 3 |  |
| 3 | grid | 10% | 90 | 2 | False | 0 | VIP0 | -0.40% | -0.44 | 2.81 | 0.78% | 10 | 3% | 0 | 3 |  |
| 4 | grid | 3% | 30 | 2 | False | 0 | VIP0 | -0.40% | -0.44 | 2.81 | 0.78% | 10 | 3% | 0 | 3 |  |
| 5 | grid | 3% | 180 | 2 | False | 0 | VIP0 | -0.40% | -0.44 | 2.81 | 0.78% | 10 | 3% | 0 | 3 |  |
| 6 | grid | 3% | 90 | 1 | False | 0 | VIP0 | -1.01% | -1.48 | 2.81 | 0.60% | 10 | 3% | 0 | 3 |  |
| 7 | grid | 3% | 90 | 3 | False | 0 | VIP0 | -0.09% | -0.09 | 2.81 | 0.86% | 10 | 3% | 0 | 3 |  |
| 8 | grid | 3% | 90 | 2 | True | 0 | VIP0 | -0.40% | -0.44 | 2.81 | 0.78% | 10 | 3% | 0 | 3 |  |
| 9 | grid | 3% | 90 | 2 | False | 1 | VIP0 | -0.85% | -1.07 | 2.63 | 0.88% | 10 | 3% | 0 | 3 |  |
| 10 | grid | 3% | 90 | 2 | False | 0 | VIP0_BNB | 0.15% | 0.15 | 3.25 | 0.89% | 16 | 5% | 0 | 3 |  |
| 11 | grid | 3% | 90 | 2 | False | 0 | VIP3 | 1.04% | 1.01 | 3.92 | 0.75% | 27 | 7% | 0 | 7 |  |
| 12 | replication | 3% | 90 | 2 | False | 0 | HMRW-low | 5.22% | 3.95 | 6.30 | 0.76% | 255 | 26% | 0 | 13 |  |
| 13 | replication | 3% | 90 | 2 | False | 0 | HMRW-mid | 4.29% | 3.25 | 5.57 | 0.77% | 103 | 19% | 0 | 14 |  |
| 14 | replication | 3% | 90 | 2 | False | 0 | HMRW-high | 3.28% | 2.84 | 5.49 | 0.73% | 59 | 13% | 0 | 11 |  |
