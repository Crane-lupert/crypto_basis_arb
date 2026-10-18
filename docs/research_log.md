# Research log

Dated, append-only. Decisions are recorded before results whenever possible.

## 2026-10-18 — project start, pre-registration

* Scope fixed: Binance spot / USDⓈ-M perpetual / USDⓈ-M quarterly, BTC and ETH, 1h UTC grid, 2020-01-01 to date.
* Theory anchors checked against publisher pages (see `docs/theory.md`): He-Manela-Ross-von Wachter
  (arXiv:2212.06888), Ackerer-Hugonnier-Jermann (arXiv:2310.11771 / Mathematical Finance),
  Angeris-Chitra-Evans-Lorig (arXiv:2209.03307 / SIAM J. Fin. Math.), Schmeling-Schrimpf-Todorov
  (BIS WP 1087). Binance funding and delivery specifications read from the support articles.
* Data endpoints verified keyless and reachable; funding history available from 2019-09-10; quarterly
  contracts from `BTCUSDT_210326`; expired-contract klines still served (`docs/data_sources.md`).
* Strategy rules, costs, hurdles and the sensitivity grid frozen in `docs/strategy_spec.md` v1.0
  **before** any full-sample backtest. Thresholds are cost-derived (round trip amortized over the
  holding horizon + 3%/yr buffer), not fitted.
* Fair-value module and signal state machines written with unit tests (23 tests) before the data layer,
  engine and daemon were implemented.
* Honesty policy adopted (`docs/strategy_spec.md` §10): backtest, paper and testnet results are labelled as
  such; no real-money results are claimed anywhere in this repository.

## 2026-10-18 — model review fixes (before any full-sample run)

A first-pass code review of `model/` produced 16 findings; the behaviour-relevant ones were fixed and
the spec updated in the same commit (`31d7787`), still before the data layer or engine existed:

* Trailing funding is now a 30-day **calendar** window of per-print annualized rates (each print annualized
  by its actual interval), so Binance's 4h/1h funding regimes no longer understate `f̂`; a full window is
  required (the first 30 days of the sample carry no S2/S3 decision) and a stalled feed yields NaN.
* NaN excess = no decision in both state machines (previously the simple loop exited immediately).
* `min_hold` counts from entry; a roll no longer resets it. ROLL rows report the next contract's values.
* S1/S2/S4 cannot reverse regardless of the flag (no borrow-cost model); S4's lower band edge is kept
  asymmetric for documentation only.
* `validate_panel` now rejects non-UTC indexes, non-positive futures closes, percent-unit rates and
  `dte_days` that disagree with `expiry − ts`.
* Synthetic panel fixture now promotes the next quarter to current at expiry, like the real builder.

## 2026-10-18 — panel build, engineering review, spec v1.0.2 (still before the cited run)

Panels: `BTCUSDT` and `ETHUSDT`, 59,565 hourly rows each (2020-01-01 00:00 → 2026-10-17 20:00 UTC),
7,446 funding prints each (all 8h in this sample), 25 quarterlies each (`_210326` … `_270326`, data from
2021-02-03/04), median annualized current-quarter basis 5.66% (BTC) / 5.13% (ETH), maximum with ≥ 7 days
to expiry 80.6% / 95.7% (2021). Three Binance-wide spot gaps (2020-02-19 5h, 2020-12-21 4h, 2021-08-13 4h)
are forward-filled (close only, high/low NaN).

A first engine run on the pre-review model (`reports/`, superseded) produced the findings below. A second,
independent engineering review of spec + model + docs returned one blocker and five high findings. All
decisions taken before the cited run, as spec **v1.0.2**:

1. **Phantom expiry/roll on data holes** (blocker): the held quarterly is keyed to the expiry recorded at
   entry; expiry only when `ts ≥` that expiry; a missing symbol/price/dte on a later bar is a HOLD.
2. **Trailing funding** is the sum of prints over the trailing 30-day calendar window × 365/30 (time-weighted),
   not a mean of per-print annualized rates; full window required; a stalled feed yields NaN.
3. **Headline metrics are excess of the T-bill**: daily excess P&L = P&L − (r_t/365) × capital; raw figures
   are reported second.
4. **ETH spot is unstaked**: `y = 0` in decisions and P&L for both coins; the 3% ETH staking proxy is a
   diagnostic column only (it had been in the signal but never in the P&L).
5. **Binance's 0.01% component is a contract term**, fixed whenever the average premium is inside the clamp
   band; the equilibrium funding `(r − r′)/κ` is the benchmark it is compared with. S2 earns a rent that is
   treated as a risk premium. Reworded in `fair_value.py`, `theory.md`, the interview notes.
6. **Liquidation monitor on close prices**: the first run liquidated S3's long quarterly on a single wick
   (`BTCUSDT_210625` low 35,000 on 2021-04-18 04:00, −44% versus ~50k spot). Binance liquidates on mark price,
   and thin-quarterly wicks are not mark prices. The intrabar worst case is reported as a diagnostic only.
7. **Notional resize rule**: coin quantity was fixed, so after a large price move the pair's notional and its
   funding income scaled far beyond the initial capital (cross-mode utilization reached 51×). Both legs are
   resized to the target notional at a funding stamp when |notional/target − 1| > 25% (fees charged), and
   returns are computed on the capital actually required (spot value + futures margin at target leverage).
8. **S3 margin**: both legs sit in one USDⓈ-M wallet → `futures_pair_mode = cross_collateral` (no per-leg
   liquidation; utilization tracked). Spot-leg strategies stay isolated by default.
9. **Fill timing**: `fill_lag_bars ∈ {0, 1}` becomes a sensitivity axis (0 pre-registered).
10. **S4 replication cells**: HMRW's fee levels (2.25/0.18, 4.5/0.72, 6.75/1.44 bp spot/futures, no slippage)
    are run as labelled replication cells; at VIP0 taker costs S4 is expected to be nearly inactive.
11. **Cost tiers**: the BNB discount is 25% on spot but 10% on USDⓈ-M futures (7.5 / 4.5 bp).
12. **Rate timing**: the DTB3 observation for date `d` applies from `d + 1` 00:00 UTC (publication lag;
    Friday values effectively from Monday — a residual same-day look-ahead of a few hours remains, immaterial
    for a rate that moves by basis points).
13. **Outage rows carry no decision** (`stale_bar`), see the spec §2.
14. **Calendar excess uses the quarterly-vs-perpetual spread** `ln(F/P)/τ` (the spread actually traded)
    minus the trailing funding forecast, and is described as a funding-forecast / term-premium trade.
15. **Live daemon** decides only on closed 1h bars; stale quotes > 30 min → HALT without flattening (never trade
    blind); drawdown > 5% → SAFE, flatten only on a margin-utilization breach; testnet fills in a separate
    ledger; quarterlies are unsupported on the testnet.

## 2026-10-18 — v1.0.2 results (pre-registered cell; reports commit `36f7433`)

Engine and daemon implement v1.0.2 (197 offline tests). Headline (excess of the T-bill on committed capital,
flat days included): BTC S2 +2.77%/yr, excess Sharpe 3.14, max DD 2.00%, 5 round trips; ETH S2 +4.63%/yr,
4.03, 1.25%; S1 +0.91% / +0.24% (Sharpe 0.26 / 0.07); S3 −1.55% / −1.67%; S4 −1.36% / −0.40% at VIP0.
`N_configs` = 12 per strategy (small grid) + 3 HMRW replication cells for S2 and S4.

What surprised: (1) S3 is negative after costs in both roots — trailing realized funding does not forecast
the quarterly-vs-perp spread well enough to pay 28 bp per round trip; (2) S4 is inactive at VIP0 but trades
39-157 times with +1.4% to +3.3% excess at HMRW's fee levels, so the replication cells, not the pre-registered
cell, carry the HMRW comparison; (3) the T-bill convention (charged on committed capital while flat) is the
dominant drag for every rule in 2022-23 and 2025-26; (4) the resize rule fired 13-43 times per strategy and
root, i.e. the fixed-quantity drift the reviewer flagged was real; (5) no liquidation on close prices at 2×,
peak utilization 0.56-0.68, zero intrabar breach bars; (6) the share of BTC funding prints exactly at 0.01%
fell from 50.5% (2020) to 6.7% (2026). Direction check from the design audit: S2 raw Sharpe is positive in
every year 2020-2024 (7.8, 14.0, 5.0, 3.5, 10.9) and lower in 2025-26 (3.4, 0.0), as predicted; in excess
terms the flat years 2022-23 and 2025-26 are negative.

Known gaps carried forward: the paper daemon does not apply the §6 resize rule (fixed coin quantity per
position); `live/history.py` duplicates `data/`; the testnet broker is exercised only against mocked HTTP
(no keys yet); quarterly listing on the testnet varies (`BTCUSDT_261225` was TRADING on 2026-10-18), so the
daemon checks the listing at each entry.

Next entries: paper-live start date and first 14-day operations review.

## 2026-10-18 — cost and latency diagnostics (no rule change)

Question: the pre-registered cell makes no S1/S2/S4 trade since early 2025; is the binding constraint the
rule, the cost of trading, or the timing of fills? Answered only with cells that already exist: the cost-tier
and fill-lag cells of the §8 grid and the HMRW replication cells (`N_configs` unchanged). No threshold, cost
assumption or rule changed. Report: `reports/cost_latency/summary.md` (`scripts/cost_latency_report.py`).

* **Cost decides the short-horizon trade.** S4 (HMRW) at VIP0 taker cost (38 bp round trip) trades 8 / 10
  times (BTC / ETH) with excess −1.36% / −0.40%; at HMRW's fee levels (4.9-16.4 bp) it trades 39-157 /
  59-255 times with +1.37% to +3.29% / +3.28% to +5.22%. S2 is positive at every tier; S3 is negative at every
  tier; S1 improves from +0.91% to +1.37% (BTC) between VIP0 and VIP3.
* **These are fee cells, not maker cells.** Every tier keeps taker fills at the hourly close. A liquidity
  provider earning such fees with resting quotes would also bear fill uncertainty, queue position and adverse
  selection; none is measured here.
* **Latency.** One-bar fill lag removes S1's edge (BTC +0.91% → −0.65%, ETH +0.24% → −0.60%) and leaves S2
  unchanged (its input moves only at funding stamps). For BTC the entire S1 lag gap (−1,650 USDT) comes from
  three bars: the first trading hours of `BTCUSDT_210326`, the 2021-04-18 quarterly wick, and the 2021-09-07
  crash (−1,744 USDT; the other nine trips +94 USDT). For ETH the gap (−770 USDT) is spread more evenly (top
  three trips −401 USDT). Read both ways: a late taker loses on extreme bars, and same-bar close fills are
  optimistic exactly there.
* **2026 year to date (290 days).** No sanctioned cell, HMRW replication cells included, beats the T-bill in
  either root; S4 makes no trade even at HMRW fees and S2 one trade (BTC, HMRW-low). A fee advantage is
  necessary for the short-horizon trade, not sufficient: in 2026 the dislocation itself was small.

Candidate hypothesis for a v1.1 (not run, to be pre-registered before any result): quote S4 passively around
the HMRW fair premium and measure fills by trade-through on 1-minute data plus post-fill markouts (adverse
selection); evaluate on forward paper data recorded by the daemon (`SNAPSHOT` bid/ask every 60 s), not on the
sample already seen.
