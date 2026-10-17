# Sensitivity grid: ETHUSDT

Backtest results. This grid is the only sanctioned variation of the pre-registered configuration (docs/strategy_spec.md section 8); every cell is reported, in grid order, not ranked.

- Mode `small`; axes: buffer [0.0, 0.03, 0.06, 0.1]; lookback [30, 90, 180]; leverage [1.0, 2.0, 3.0]; hold_to_expiry [False, True]; costs ['VIP0', 'VIP0_BNB25', 'VIP3']
- **N_configs = 11** cells per strategy x 4 strategies = 44 backtests (35 distinct engine runs after de-duplicating cells a strategy's rule cannot distinguish).
- Pre-registered base cell (marked **base**): buffer = 0.03, lookback = 90, leverage = 2.0, hold_to_expiry = False, costs = VIP0
- Cost tiers: VIP0 = spot 10 / futures 5 / slippage 2 / delivery 5 bp; VIP0_BNB25 = spot 7.5 / futures 3.75 / slippage 2 / delivery 3.75 bp; VIP3 = spot 6 / futures 3.2 / slippage 2 / delivery 3.2 bp
- Axis relevance: buffer moves the S1-S3 entry hurdles (S4's hurdle is the HMRW band edge, no buffer); lookback moves S2 and S3 (trailing funding window = lookback x 8h); hold_to_expiry sets both cc_hold_to_expiry (S1) and cal_hold_to_expiry (S3); leverage moves capital and the liquidation threshold of every futures leg; costs move hurdles and execution costs of all strategies.

## Distribution across cells

| Strategy | Base Sharpe | Median Sharpe | Min Sharpe | Max Sharpe | Base ann. return | Median ann. return | Min ann. return | Max ann. return | Cells with return > 0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.17 | 0.17 | -0.01 | 0.78 | 0.59% | 0.59% | -0.04% | 2.60% | 10/11 |
| S2 funding_carry | 0.64 | 0.64 | 0.30 | 4.44 | 0.74% | 0.77% | 0.38% | 3.32% | 11/11 |
| S3 calendar | -0.00 | 0.04 | -0.06 | 0.65 | -0.00% | 0.22% | -0.35% | 3.51% | 7/11 |
| S4 random_maturity | 1.38 | 1.38 | 1.38 | 2.93 | 2.17% | 2.17% | 2.05% | 4.75% | 11/11 |

## Full grid

### S1 cash_and_carry

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | 0.59% | 0.17 | 10.16% | 15 | 36% | 6 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | 1.04% | 0.30 | 10.16% | 25 | 54% | 6 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | 0.41% | 0.12 | 10.16% | 12 | 30% | 5 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 0.15% | 0.04 | 11.43% | 11 | 21% | 4 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 0.59% | 0.17 | 10.16% | 15 | 36% | 6 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | 0.59% | 0.17 | 10.16% | 15 | 36% | 6 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 2.60% | 0.78 | 5.94% | 12 | 45% | 2 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | 0.73% | 0.20 | 10.39% | 15 | 34% | 6 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | -0.04% | -0.01 | 12.01% | 17 | 36% | 6 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | 1.00% | 0.29 | 9.89% | 17 | 40% | 6 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | 1.45% | 0.42 | 9.74% | 21 | 42% | 6 |  |

### S2 funding_carry

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | 0.74% | 0.64 | 3.23% | 4 | 16% | 3 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | 0.38% | 0.30 | 3.48% | 5 | 12% | 4 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | 1.05% | 0.77 | 3.24% | 4 | 31% | 2 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 0.71% | 0.64 | 3.24% | 3 | 21% | 2 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 0.58% | 0.42 | 3.52% | 10 | 24% | 4 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | 0.96% | 1.35 | 1.70% | 3 | 20% | 2 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 3.32% | 4.44 | 1.29% | 4 | 41% | 2 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | 0.97% | 1.44 | 2.03% | 4 | 9% | 4 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | 0.74% | 0.64 | 3.23% | 4 | 16% | 3 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | 0.77% | 0.62 | 3.45% | 5 | 17% | 3 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | 0.81% | 0.70 | 3.37% | 5 | 17% | 3 |  |

### S3 calendar

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | -0.00% | -0.00 | 17.37% | 33 | 26% | 1 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | -0.35% | -0.06 | 19.72% | 106 | 58% | 2 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | 0.84% | 0.16 | 11.59% | 20 | 16% | 1 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 0.88% | 0.17 | 11.57% | 16 | 12% | 1 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 3.51% | 0.65 | 8.16% | 68 | 34% | 1 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | 0.22% | 0.04 | 14.98% | 30 | 29% | 2 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 0.06% | 0.02 | 9.09% | 33 | 26% | 1 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | -0.30% | -0.04 | 26.48% | 33 | 23% | 3 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | -0.02% | -0.00 | 17.46% | 34 | 26% | 1 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | 0.35% | 0.07 | 15.62% | 38 | 29% | 1 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | 0.51% | 0.10 | 14.68% | 40 | 29% | 1 |  |

### S4 random_maturity

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | 2.17% | 1.38 | 3.21% | 9 | 3% | 1 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | 2.17% | 1.38 | 3.21% | 9 | 3% | 1 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | 2.17% | 1.38 | 3.21% | 9 | 3% | 1 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 2.17% | 1.38 | 3.21% | 9 | 3% | 1 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 2.17% | 1.38 | 3.21% | 9 | 3% | 1 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | 2.17% | 1.38 | 3.21% | 9 | 3% | 1 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 2.05% | 2.84 | 0.60% | 9 | 3% | 0 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | 2.45% | 1.50 | 3.25% | 9 | 3% | 1 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | 2.17% | 1.38 | 3.21% | 9 | 3% | 1 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | 4.11% | 2.52 | 0.85% | 18 | 5% | 1 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | 4.75% | 2.93 | 0.81% | 26 | 7% | 1 |  |
