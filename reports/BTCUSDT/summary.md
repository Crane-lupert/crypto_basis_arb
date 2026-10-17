# Backtest report: BTCUSDT

Backtest (simulated taker fills on historical hourly bars), not paper or live results.
Sample 2020-01-01T00:00:00+00:00 to 2026-10-17T20:00:00+00:00 (59,565 hourly bars, 6.80 years), panel `data\panel_BTCUSDT_1h.parquet`.
Target size: 10,000 USDT per leg, equal coin quantity on both legs, resized to target when the pair notional drifts more than 25%. All figures are net of fees, slippage, delivery fees, liquidation penalties and resize costs.

## Headline (excess of the T-bill)

| Strategy | Excess ann. return | Excess Sharpe | Raw ann. return | Raw Sharpe | Max DD | Round trips | Hit rate | Avg hold (h) | Time in position | Turnover (x/yr) | Worst 30d | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.91% | 0.26 | 3.77% | 1.06 | 6.45% | 12 | 100% | 1,626 | 33% | 5.9 | -2.47% | 0 | 13 |
| S2 funding_carry | 2.77% | 3.14 | 5.63% | 6.68 | 2.00% | 5 | 100% | 5,397 | 45% | 3.1 | -1.08% | 0 | 22 |
| S3 calendar | -1.55% | -0.37 | 1.31% | 0.32 | 12.54% | 36 | 53% | 490 | 30% | 22.0 | -6.20% | 0 | 6 |
| S4 random_maturity | -1.36% | -2.24 | 1.50% | 2.59 | 1.32% | 8 | 100% | 159 | 2% | 3.2 | 0.00% | 0 | 1 |

Excess ann. return = total excess return / years, where the excess return deducts the T-bill rate (panel r_usd) on the capital in use, flat days included. Raw figures assume idle cash earns nothing. Worst 30d = worst rolling 30-day sum of daily raw returns. Turnover = traded notional / mean capital per year. Round trips exclude a position still open on the last bar.

## P&L decomposition (USDT, whole sample)

| Strategy | Target capital | Mean capital | Basis convergence | Funding | Fees + slippage | Delivery fees | Liquidation penalties | Resize costs | Total P&L | T-bill on capital | Excess P&L |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 15,000 | 15,112 | 4,636.46 | 0.00 | -491.60 | 0.00 | 0.00 | -67.52 | 4,077.34 | -2,937.46 | 1,139.88 |
| S2 funding_carry | 15,000 | 14,884 | 28.57 | 6,152.94 | -181.52 | 0.00 | 0.00 | -113.00 | 5,886.98 | -2,924.85 | 2,962.13 |
| S3 calendar | 10,000 | 10,069 | 71.28 | 1,757.63 | -1,030.45 | 0.00 | 0.00 | -23.28 | 775.20 | -1,962.09 | -1,186.90 |
| S4 random_maturity | 15,000 | 15,014 | 470.51 | 1,473.85 | -306.85 | 0.00 | 0.00 | -5.32 | 1,632.20 | -2,918.73 | -1,286.53 |

## Margin

| Strategy | Margin mode | Leverage | Peak utilization (close) | Bars above isolated threshold | Intrabar breach bars (diagnostic) | Peak intrabar utilization | Peak pair utilization | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | isolated | fut 2x | 0.622 | 0 | 0 | 0.669 | n/a | 0 | 13 |
| S2 funding_carry | isolated | perp 2x | 0.626 | 0 | 0 | 0.662 | n/a | 0 | 22 |
| S3 calendar | cross_collateral | perp 2x, fut 2x | 0.683 | 0 | 0 | 0.845 | 0.072 | 0 | 6 |
| S4 random_maturity | isolated | perp 2x | 0.557 | 0 | 0 | 0.596 | n/a | 0 | 1 |

Liquidation is decided on close prices; 'intrabar breach bars' counts bars whose high/low would have crossed the isolated threshold (diagnostic only, never triggers). Peak pair utilization = net loss of the two futures legs / their posted margin (S3).

## Year by year

Bars are labeled by close time, so the bar closing at 00:00 UTC on 1 January belongs to the previous year (a sample starting at 00:00 shows a one-bar row for that year). Returns are period sums (not annualized); round trips are counted in the year they close; costs = fees + slippage + delivery + liquidation + resize.

### S1 cash_and_carry

| Year | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | -0.00% | n/a | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2020 | 366 | 0.00 | 0.00% | -0.37% | 0.00 | -13.17 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2021 | 365 | 2,374.64 | 13.81% | 13.76% | 1.62 | 1.62 | 6.45% | 2,657.94 | 0.00 | -283.30 | 6 | 83% | 0 | 5 |
| 2022 | 365 | 302.45 | 2.07% | 0.07% | 2.03 | 0.07 | 0.69% | 367.43 | 0.00 | -64.98 | 2 | 28% | 0 | 2 |
| 2023 | 365 | -11.84 | -0.03% | -5.10% | -0.05 | -7.88 | 0.98% | 24.52 | 0.00 | -36.36 | 0 | 16% | 0 | 0 |
| 2024 | 366 | 1,160.72 | 8.05% | 3.06% | 2.41 | 0.92 | 1.56% | 1,308.64 | 0.00 | -147.93 | 3 | 78% | 0 | 5 |
| 2025 | 365 | 251.37 | 1.75% | -2.31% | 2.95 | -3.90 | 0.39% | 277.93 | 0.00 | -26.56 | 1 | 18% | 0 | 1 |
| 2026 | 290 | 0.00 | 0.00% | -2.93% | 0.00 | -493.62 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |

### S2 funding_carry

| Year | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | -0.00% | n/a | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2020 | 366 | 1,539.76 | 9.70% | 9.34% | 7.80 | 7.53 | 2.00% | 3.23 | 1,621.05 | -84.52 | 1 | 80% | 0 | 5 |
| 2021 | 365 | 2,983.21 | 19.69% | 19.65% | 14.01 | 13.98 | 1.08% | 5.35 | 3,067.71 | -89.84 | 1 | 88% | 0 | 11 |
| 2022 | 365 | 256.66 | 1.80% | -0.21% | 4.99 | -0.54 | 0.16% | 0.47 | 292.69 | -36.50 | 1 | 66% | 0 | 4 |
| 2023 | 365 | 121.29 | 0.83% | -4.23% | 3.50 | -17.82 | 0.10% | 2.25 | 138.04 | -19.01 | 0 | 6% | 0 | 0 |
| 2024 | 366 | 883.23 | 5.64% | 0.66% | 10.85 | 1.27 | 0.23% | 17.28 | 912.85 | -46.90 | 1 | 51% | 0 | 2 |
| 2025 | 365 | 102.83 | 0.60% | -3.47% | 3.40 | -19.75 | 0.11% | -0.01 | 120.60 | -17.76 | 1 | 17% | 0 | 0 |
| 2026 | 290 | 0.00 | 0.00% | -2.93% | 0.00 | -493.62 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |

### S3 calendar

| Year | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | -0.00% | n/a | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2020 | 366 | 0.00 | 0.00% | -0.37% | 0.00 | -13.17 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2021 | 365 | 1,568.17 | 16.52% | 16.48% | 1.71 | 1.70 | 9.39% | -14.28 | 1,993.40 | -410.95 | 14 | 75% | 0 | 3 |
| 2022 | 365 | -2.74 | 0.01% | -2.00% | 0.00 | -1.79 | 1.44% | 270.06 | -34.50 | -238.29 | 8 | 19% | 0 | 0 |
| 2023 | 365 | 1.80 | 0.07% | -4.99% | 0.07 | -5.37 | 2.14% | -18.83 | 70.09 | -49.46 | 2 | 32% | 0 | 2 |
| 2024 | 366 | -856.32 | -8.33% | -13.32% | -1.85 | -2.95 | 7.66% | -366.08 | -189.44 | -300.81 | 10 | 61% | 0 | 1 |
| 2025 | 365 | 110.64 | 1.12% | -2.94% | 2.34 | -6.14 | 0.39% | 192.58 | -54.75 | -27.20 | 1 | 8% | 0 | 0 |
| 2026 | 290 | -46.35 | -0.47% | -3.41% | -1.86 | -13.34 | 0.48% | 7.84 | -27.18 | -27.01 | 1 | 8% | 0 | 0 |

### S4 random_maturity

| Year | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2019 | 1 | 0.00 | 0.00% | -0.00% | n/a | n/a | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2020 | 366 | 331.11 | 2.12% | 1.76% | 3.20 | 2.68 | 1.32% | 273.67 | 246.08 | -188.65 | 5 | 4% | 0 | 0 |
| 2021 | 365 | 1,301.09 | 8.09% | 8.04% | 6.20 | 6.16 | 1.19% | 196.84 | 1,227.77 | -123.52 | 3 | 11% | 0 | 1 |
| 2022 | 365 | 0.00 | 0.00% | -2.00% | 0.00 | -26.08 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2023 | 365 | 0.00 | 0.00% | -5.06% | 0.00 | -333.31 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2024 | 366 | 0.00 | 0.00% | -4.98% | 0.00 | -260.59 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2025 | 365 | 0.00 | 0.00% | -4.07% | 0.00 | -351.29 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| 2026 | 290 | 0.00 | 0.00% | -2.93% | 0.00 | -493.62 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |

## Stress episodes

### S1 cash_and_carry

| Episode | Window (UTC days) | Coverage | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 61 | 0.00 | 0.00% | -0.04% | 0.00 | -16.25 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | 107 | 552.09 | 3.51% | 3.50% | 2.49 | 2.48 | 1.41% | 672.05 | 0.00 | -119.95 | 3 | 86% | 0 | 2 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | 61 | 0.00 | 0.00% | -0.70% | 0.00 | -977.36 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 61 | 99.77 | 0.67% | -0.15% | 4.62 | -1.07 | 0.13% | 115.76 | 0.00 | -15.99 | 1 | 7% | 0 | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 121 | 0.00 | 0.00% | -1.19% | 0.00 | -5,465.84 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |

### S2 funding_carry

| Episode | Window (UTC days) | Coverage | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 61 | -35.19 | -0.31% | -0.35% | -1.03 | -1.17 | 1.90% | 12.11 | -20.86 | -26.45 | 1 | 44% | 0 | 1 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | 107 | 207.78 | 1.57% | 1.56% | 6.63 | 6.58 | 0.37% | 5.88 | 232.94 | -31.05 | 1 | 64% | 0 | 3 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | 61 | 0.00 | 0.00% | -0.70% | 0.00 | -977.36 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 61 | 0.00 | 0.00% | -0.82% | 0.00 | -458.64 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 121 | 0.00 | 0.00% | -1.19% | 0.00 | -5,465.84 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |

### S3 calendar

| Episode | Window (UTC days) | Coverage | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 61 | 0.00 | 0.00% | -0.04% | 0.00 | -16.25 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | 107 | -193.09 | -1.61% | -1.62% | -0.90 | -0.91 | 4.23% | -248.62 | 203.95 | -148.42 | 5 | 77% | 0 | 2 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | 61 | -31.28 | -0.29% | -0.99% | -1.03 | -3.49 | 1.01% | 40.60 | -29.14 | -42.75 | 1 | 15% | 0 | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 61 | 49.93 | 0.54% | -0.28% | 2.12 | -1.09 | 0.64% | 134.47 | -29.14 | -55.40 | 2 | 41% | 0 | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 121 | -46.35 | -0.47% | -1.67% | -2.89 | -10.16 | 0.48% | 7.84 | -27.18 | -27.01 | 1 | 18% | 0 | 0 |

### S4 random_maturity

| Episode | Window (UTC days) | Coverage | Days | P&L (USDT) | Return | Excess return | Raw Sharpe | Excess Sharpe | Max DD | Basis | Funding | Costs | Round trips | Time in position | Liquidations | Resizes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Mar-2020 | 2020-03-01 .. 2020-04-30 | 100% | 61 | 0.00 | 0.00% | -0.04% | 0.00 | -16.25 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| May-2021 | 2021-05-01 .. 2021-08-15 | 100% | 107 | 0.00 | 0.00% | -0.01% | 0.00 | -39.63 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| Nov-2022 FTX | 2022-11-01 .. 2022-12-31 | 100% | 61 | 0.00 | 0.00% | -0.70% | 0.00 | -977.36 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| Aug-2024 | 2024-08-01 .. 2024-09-30 | 100% | 61 | 0.00 | 0.00% | -0.82% | 0.00 | -458.64 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |
| Q1/Q2-2026 funding compression | 2026-01-31 .. 2026-05-31 | 100% | 121 | 0.00 | 0.00% | -1.19% | 0.00 | -5,465.84 | 0.00% | 0.00 | 0.00 | 0.00 | 0 | 0% | 0 | 0 |

## Funding diagnostics

Share of funding prints exactly equal to 0.000100 (Binance's interest component: the average premium sat inside the clamp band) and the cadence of prints (hours since the previous print), by year of the print.

| Year | Funding prints | Share exactly 0.000100 | 1h gaps | 4h gaps | 8h gaps | Other gaps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2020 | 1098 | 50.5% | 0 | 0 | 1097 | 0 |
| 2021 | 1095 | 43.1% | 0 | 0 | 1095 | 0 |
| 2022 | 1095 | 30.8% | 0 | 0 | 1095 | 0 |
| 2023 | 1095 | 38.5% | 0 | 0 | 1095 | 0 |
| 2024 | 1098 | 42.1% | 0 | 0 | 1098 | 0 |
| 2025 | 1095 | 17.2% | 0 | 0 | 1095 | 0 |
| 2026 | 870 | 6.7% | 0 | 0 | 870 | 0 |

## Sensitivity grid

Mode `small`: **N_configs = 12** grid cells per strategy x 4 strategies, plus 3 HMRW replication cells for S4 and S2. Discount the best cell accordingly; the full grid is in `sensitivity.md` / `sensitivity.csv`.

| Strategy | Base excess Sharpe | Median | Min | Max | Base excess ann. return | Median | Min | Max | Cells with excess return > 0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 cash_and_carry | 0.26 | 0.26 | -0.22 | 0.38 | 0.91% | 0.91% | -0.65% | 1.41% | 10/12 |
| S2 funding_carry | 3.14 | 3.14 | 2.02 | 3.56 | 2.77% | 2.77% | 1.36% | 3.47% | 12/12 |
| S3 calendar | -0.37 | -0.37 | -1.06 | -0.07 | -1.55% | -1.54% | -2.85% | -0.27% | 0/12 |
| S4 random_maturity | -2.24 | -2.24 | -3.74 | -0.48 | -1.36% | -1.36% | -1.74% | -0.34% | 0/12 |

HMRW replication cells (HMRW's fee levels, no slippage; other settings pre-registered):

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

## Figures

![Cumulative return over the T-bill](equity_curves.png)

Cumulative sum of hourly excess returns (return on the capital in use minus the T-bill), net of all costs.

![Drawdown](drawdown.png)

Drawdown from the running equity peak (raw P&L); one panel per strategy, worst value of each UTC day.

![Theory vs market](theory_vs_market.png)

Top: annualized current-quarter basis b = ln(F/S)/tau vs the cost-of-carry fair basis b* = r - y (daily means; bars inside the 2-day roll window are masked because ln(F/S)/tau explodes as tau -> 0). Middle: trailing funding f-hat vs HMRW equilibrium funding f*. Bottom: HMRW premium deviation rho - rho* (hourly and daily mean) with the Prop. 2 no-arbitrage band; S4 enters when the hourly value is above the upper edge.

## Assumptions

### Configuration used

- Notional per leg: 10,000 USDT; costs: spot fee 10 bp, futures fee 5 bp, slippage 2 bp per leg, delivery fee 5 bp, liquidation penalty 50 bp.
- Margin: spot-leg strategies isolated, futures pair (S3) cross_collateral; perp leverage 2x, quarterly leverage 2x, maintenance 0.40%.
- Strategy parameters: {'cc_entry_buffer_ann': 0.03, 'cc_exit_excess_ann': 0.0, 'cc_roll_days': 2.0, 'cc_hold_to_expiry': False, 'fc_lookback_prints': 90, 'fc_entry_buffer_ann': 0.03, 'fc_exit_excess_ann': 0.0, 'fc_expected_hold_days': 30.0, 'cal_entry_buffer_ann': 0.03, 'cal_exit_excess_ann': 0.0, 'cal_roll_days': 2.0, 'cal_hold_to_expiry': False, 'cal_allow_reverse': True, 'rm_exit_dev_ann': 0.0, 'min_hold_bars': 8, 'fill_lag_bars': 0, 'allow_reverse': False}

### Engine implementation choices

- Headline = excess of the T-bill: the risk-free rate accrues hourly at r_usd/8760 on the previous bar's capital ((mean r of the day)/365 x capital for a full day), including flat days, because the capital is committed to the strategy. Raw figures (idle cash earns nothing) are reported second.
- Capital = capital actually required by the open position, re-measured each bar: spot value + futures notional / leverage (S3: both futures legs at their leverage); the target capital while flat. Hourly return = P&L / previous bar's capital; daily return = sum of hourly returns (no compounding).
- Equal coin quantity on both legs: qty = notional_per_leg / close of the first leg (spot for S1/S2/S4, perpetual for S3). Fills at close x (1 +/- slippage) with fees on the executed notional; the 'basis' component is marked at reference (close) prices, so slippage appears under fees.
- Bar order: mark, settle expiries, funding, margin monitor, trade, resize. Funding is paid to perp legs held before the bar's trades (a leg opened on a stamp bar does not receive that stamp; a leg closed on a stamp bar does). Decisions are filled fill_lag_bars bars later (0 pre-registered).
- Expiry: a held quarterly with expiry <= ts settles at spot_close with the delivery fee; the remaining leg is closed on the same bar at close +/- slippage (round trip exit_reason EXPIRY).
- Margin monitor on CLOSE prices: worst adverse close move since entry (or since the last resize). Isolated mode (S1/S2/S4 by default) liquidates a leg whose move exceeds 1/leverage - maintenance at that bar's close (+/- slippage, taker fee, liquidation penalty) and closes the other leg. S3 uses futures_pair_mode (cross_collateral: no per-leg liquidation). The intrabar high/low worst case never triggers; it is counted as a diagnostic. Utilization = adverse close move / (1/leverage - maintenance).
- Resize: at a funding stamp, if |pair notional / target - 1| > 25% (pair notional = |qty| x close of the first leg), both legs are traded back to the target notional at close +/- slippage with fees (P&L component 'resize'); the futures margin is re-established, so the margin monitor restarts.
- After a forced close (expiry or liquidation) the engine stays flat until the target's (direction, contract) pair changes; it never re-enters on the bar of the forced close.
- A roll closes the old quarterly and opens the next one (same coin quantity) on the same bar; the spot/perp leg is untouched and the round trip continues (contract column 'A>B').
- A futures leg without a price on a bar keeps its last mark (closing at it logs STALE_PRICE; a resize waits for a priced stamp); an entry or roll into an unpriced contract is skipped (NO_PRICE) and retried.
- Positions open on the last bar are marked to market without exit costs (trades exit_reason OPEN; excluded from round-trip counts and hit rate).
- Daily figures by UTC day; the bar closing at 00:00 UTC belongs to the previous day. Sharpe = mean/std x sqrt(365) of daily returns; an identically zero series has Sharpe 0. Max drawdown is a fraction of the running equity peak.

### Pre-registered spec excerpt (docs/strategy_spec.md, sections 4, 6 and 7, verbatim)

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
  At VIP0 taker costs this is 4.15 in annualized-premium units, i.e. a 38 bp perpetual premium at an hourly
  close, about 2.3× HMRW's widest tested band, so S4 is expected to be nearly inactive at VIP0; the HMRW fee
  levels are run as replication cells (§8).

Delivery fee 5 bp on a quarterly carried into settlement. Liquidation penalty 50 bp (isolated mode only).
Cost tiers for the grid: VIP0 (10 / 5 / 2 bp), VIP0 with BNB discount (spot −25%, futures −10%: 7.5 / 4.5 / 2),
VIP3 (6 / 3.2 / 2); HMRW replication (2.25 / 0.18, 4.5 / 0.72, 6.75 / 1.44 bp, no slippage).

#### 6. Backtest engine semantics (backtest/engine.py)

Event loop over the 1h panel, one strategy at a time, equal coin quantity on both legs:

1. **Mark**: equity = cash + spot_qty × spot_close + Σ fut_qty × (close − entry_price).
2. **Settle expiries**: a held quarterly with `expiry <= ts` settles at `spot_close` of that bar (proxy for
   Binance's index average over the final 30 minutes) and pays the delivery fee, regardless of the target.
3. **Funding**: at a funding stamp, perp positions held *before* this bar's trades receive
   `−qty × funding_rate × perp_close` (a short with positive funding receives cash). Positions opened at
   the stamp bar do not receive that stamp's funding.
4. **Margin monitor** (futures legs): track the worst adverse **close** move since entry (Binance liquidates
   on mark price; last-trade wicks on thin quarterlies are not mark prices, so the intrabar high/low worst case
   is reported as a diagnostic only). Spot-leg strategies (S1, S2, S4) use isolated mode: if the adverse move
   exceeds `1/leverage − maintenance` the leg is liquidated at that bar's close, the other leg is closed, the
   penalty is charged, and the event is logged. S3's two futures legs share one USDⓈ-M wallet
   (`futures_pair_mode = cross_collateral`): no per-leg liquidation; peak utilization is reported.
5. **Trade**: if `target` or `contract` changed, close/open legs at `close ± slippage` and pay fees, at the
   decision bar (`fill_lag_bars = 0`, pre-registered) or the next bar (`fill_lag_bars = 1`, sensitivity axis).
   A roll closes the old contract and opens the new one on the same bar; the spot leg is untouched.
6. **Resize**: at a funding stamp, if the pair's notional deviates from the target by more than 25%, both legs
   are resized to the target notional (fees charged, event logged), so exposure never drifts far from capital.
7. **Capital** for return computation is the capital actually required by the open position, re-measured each
   bar: spot-leg strategies `spot value + futures margin at target leverage`; futures-only (S3)
   `2 × notional / leverage`. Idle cash earns nothing in the raw figures; the headline figures are excess of
   the T-bill (§7).

P&L decomposition per strategy: basis convergence (mark-to-market of the two legs), funding received or
paid, fees + slippage, delivery fees, liquidation penalties, resize costs.

#### 7. Metrics (backtest/metrics.py)

* Daily aggregation of P&L (UTC days). **Headline figures are excess of the risk-free rate**: daily excess
  P&L = P&L − (r_t/365) × capital, Sharpe = mean/std × √365 (24/7 market) on the excess series, net of all
  costs. Raw (non-excess) figures are reported second, never as the headline.
* Annualized excess return on capital, max drawdown (on equity), hit rate per round trip, average holding time,
  number of round trips, turnover, time-in-position, worst 30-day window, liquidation and resize counts,
  peak margin utilization, intrabar worst-case diagnostic, share of funding prints exactly equal to 0.01% by year.
* Year-by-year table and the five stress episodes named in the research log (Mar-2020, May-2021,
  Nov-2022 FTX, Aug-2024, Q1/Q2-2026 funding compression).
* "Theory vs market" diagnostics: time series of `b` vs `b*`, `f̂` vs `f*`, `ρ` vs `ρ*`, with the band.
