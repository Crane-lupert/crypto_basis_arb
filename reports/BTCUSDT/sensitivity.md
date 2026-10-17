# Sensitivity grid: BTCUSDT

Backtest results. This grid is the only sanctioned variation of the pre-registered configuration (docs/strategy_spec.md section 8); every cell is reported, in grid order, not ranked.

- Mode `small`; axes: buffer [0.0, 0.03, 0.06, 0.1]; lookback [30, 90, 180]; leverage [1.0, 2.0, 3.0]; hold_to_expiry [False, True]; costs ['VIP0', 'VIP0_BNB25', 'VIP3']
- **N_configs = 11** cells per strategy x 4 strategies = 44 backtests (35 distinct engine runs after de-duplicating cells a strategy's rule cannot distinguish).
- Pre-registered base cell (marked **base**): buffer = 0.03, lookback = 90, leverage = 2.0, hold_to_expiry = False, costs = VIP0
- Cost tiers: VIP0 = spot 10 / futures 5 / slippage 2 / delivery 5 bp; VIP0_BNB25 = spot 7.5 / futures 3.75 / slippage 2 / delivery 3.75 bp; VIP3 = spot 6 / futures 3.2 / slippage 2 / delivery 3.2 bp
- Axis relevance: buffer moves the S1-S3 entry hurdles (S4's hurdle is the HMRW band edge, no buffer); lookback moves S2 and S3 (trailing funding window = lookback x 8h); hold_to_expiry sets both cc_hold_to_expiry (S1) and cal_hold_to_expiry (S3); leverage moves capital and the liquidation threshold of every futures leg; costs move hurdles and execution costs of all strategies.

## Distribution across cells

| Strategy | Base Sharpe | Median Sharpe | Min Sharpe | Max Sharpe | Base ann. return | Median ann. return | Min ann. return | Max ann. return | Cells with return > 0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.40 | 0.52 | 0.40 | 1.19 | 1.52% | 1.98% | 1.52% | 3.63% | 11/11 |
| S2 funding_carry | 3.87 | 3.79 | 0.94 | 5.35 | 2.69% | 2.69% | 1.03% | 2.91% | 11/11 |
| S3 calendar | -0.27 | -0.26 | -0.38 | 0.52 | -4.08% | -3.41% | -5.71% | 2.24% | 3/11 |
| S4 random_maturity | 2.22 | 2.22 | 0.96 | 3.27 | 1.48% | 1.48% | 1.01% | 2.46% | 11/11 |

## Full grid

### S1 cash_and_carry

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | 1.52% | 0.40 | 11.91% | 14 | 26% | 5 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | 1.98% | 0.52 | 11.91% | 26 | 39% | 5 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | 2.41% | 0.63 | 11.91% | 12 | 22% | 3 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 2.09% | 0.55 | 11.91% | 10 | 14% | 2 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 1.52% | 0.40 | 11.91% | 14 | 26% | 5 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | 1.52% | 0.40 | 11.91% | 14 | 26% | 5 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 3.63% | 1.19 | 5.26% | 12 | 33% | 0 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | 1.91% | 0.44 | 12.18% | 14 | 24% | 5 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | 1.58% | 0.42 | 11.91% | 15 | 26% | 5 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | 2.00% | 0.52 | 11.94% | 17 | 29% | 5 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | 2.10% | 0.54 | 11.90% | 19 | 30% | 5 |  |

### S2 funding_carry

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | 2.69% | 3.87 | 1.34% | 5 | 33% | 2 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | 1.93% | 1.98 | 1.63% | 8 | 26% | 4 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | 2.31% | 2.29 | 1.79% | 5 | 31% | 2 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 1.59% | 0.94 | 3.85% | 5 | 27% | 2 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 2.71% | 3.79 | 1.37% | 10 | 26% | 3 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | 1.03% | 1.94 | 1.78% | 4 | 16% | 2 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 2.71% | 5.35 | 1.02% | 5 | 36% | 1 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | 2.29% | 2.94 | 1.50% | 5 | 19% | 3 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | 2.69% | 3.87 | 1.34% | 5 | 33% | 2 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | 2.89% | 4.27 | 1.34% | 6 | 36% | 2 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | 2.91% | 4.22 | 1.31% | 6 | 36% | 2 |  |

### S3 calendar

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | -4.08% | -0.27 | 43.14% | 36 | 27% | 4 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | -5.71% | -0.38 | 51.70% | 73 | 54% | 5 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | -3.02% | -0.20 | 37.23% | 25 | 12% | 3 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 1.58% | 0.37 | 10.41% | 13 | 9% | 2 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 2.24% | 0.52 | 10.25% | 58 | 33% | 2 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | -1.06% | -0.18 | 24.64% | 29 | 27% | 3 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 0.54% | 0.23 | 8.54% | 36 | 32% | 0 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | -3.41% | -0.27 | 42.59% | 36 | 24% | 6 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | -3.94% | -0.26 | 42.39% | 37 | 27% | 4 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | -4.09% | -0.27 | 43.10% | 39 | 29% | 4 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | -3.99% | -0.26 | 42.64% | 40 | 29% | 4 |  |

### S4 random_maturity

| Cell | Buffer | Lookback | Leverage | Hold to expiry | Costs | Ann. return | Sharpe | Max DD | Round trips | Time in position | Liquidations |  |
| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3% | 90 | 2 | False | VIP0 | 1.48% | 2.22 | 1.52% | 8 | 2% | 1 | **base** |
| 1 | 0% | 90 | 2 | False | VIP0 | 1.48% | 2.22 | 1.52% | 8 | 2% | 1 |  |
| 2 | 6% | 90 | 2 | False | VIP0 | 1.48% | 2.22 | 1.52% | 8 | 2% | 1 |  |
| 3 | 10% | 90 | 2 | False | VIP0 | 1.48% | 2.22 | 1.52% | 8 | 2% | 1 |  |
| 4 | 3% | 30 | 2 | False | VIP0 | 1.48% | 2.22 | 1.52% | 8 | 2% | 1 |  |
| 5 | 3% | 180 | 2 | False | VIP0 | 1.48% | 2.22 | 1.52% | 8 | 2% | 1 |  |
| 6 | 3% | 90 | 1 | False | VIP0 | 1.32% | 2.54 | 1.16% | 8 | 2% | 0 |  |
| 7 | 3% | 90 | 3 | False | VIP0 | 1.01% | 0.96 | 2.12% | 8 | 2% | 1 |  |
| 8 | 3% | 90 | 2 | True | VIP0 | 1.48% | 2.22 | 1.52% | 8 | 2% | 1 |  |
| 9 | 3% | 90 | 2 | False | VIP0_BNB25 | 1.61% | 2.35 | 1.51% | 10 | 2% | 1 |  |
| 10 | 3% | 90 | 2 | False | VIP3 | 2.46% | 3.27 | 1.49% | 17 | 5% | 1 |  |
