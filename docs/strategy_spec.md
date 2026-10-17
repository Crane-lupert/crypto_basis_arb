# Strategy specification (pre-registered)

Status: v1.0, frozen 2026-10-18 before the first full-sample backtest. Any change to a threshold,
cost assumption, or rule after this date requires a dated entry in `docs/research_log.md` and a
re-run that reports both the old and the new configuration.

This document is the contract between `data/`, `model/`, `backtest/` and `live/`. Code that
disagrees with it is wrong; the document is not silently updated to match code.

## 1. Scope

| Item | Choice | Why |
|---|---|---|
| Venue | Binance only (spot, USDⓈ-M perpetual, USDⓈ-M quarterly delivery) | one account, one margin pool, no cross-venue transfer risk; the theory papers use Binance data |
| Underlyings | BTC, ETH | deepest books; ETH adds a non-zero coin yield (staking) to the fair-value model |
| Research grid | 1 hour, UTC | funding stamps (00/08/16 UTC) and expiries (08:00 UTC) fall on the grid |
| Sample | 2020-01-01 to the fetch date | perpetual funding history starts 2019-09; quarterlies start 2021-03 |
| Capital unit | 10,000 USDT notional per leg, equal coin quantity on both legs | delta-neutral in coin units |
| Live mode | paper fills on live quotes; order routing only to Binance **testnet** | no real-money path exists in this repository |

## 2. Instruments and panel schema

`data/panel.py` builds one parquet per underlying (`data/panel_{ROOT}_1h.parquet`) on the 1h UTC
grid with index `ts` (tz-aware UTC). Required columns (see `basis_arb.types.PANEL_REQUIRED_COLUMNS`):

| Column | Definition |
|---|---|
| `spot_close/high/low` | spot kline ending at `ts` (bar covers `[ts-1h, ts)`) |
| `perp_close/high/low` | perpetual kline ending at `ts` |
| `funding_rate` | funding rate settled **at** `ts`; NaN on non-stamp rows. Source `/fapi/v1/fundingRate` (actual stamps, so 4h/1h regimes are honored) |
| `fut_cq_*` | current-quarter contract: `symbol` (`BTCUSDT_YYMMDD`), `close/high/low`, `expiry` (08:00 UTC last Friday), `dte_days` = (expiry - ts)/1d |
| `fut_nq_*` | next-quarter contract, same fields |
| `r_usd` | FRED `DTB3`/100, forward-filled (observation date `d` applies from `d` 00:00 UTC onward; no look-ahead) |
| `y_coin` | coin yield r′: BTC 0.00, ETH 0.03 (constant staking proxy, documented in `docs/theory.md`) |

Contract selection: at `ts`, `cq` is the listed quarterly with the nearest `expiry > ts`; `nq` the one
after it. A contract whose `expiry <= ts` leaves the panel at that bar. Rows where a contract did not
trade (no kline) carry NaN in its columns; the signal treats NaN excess as "no decision".

Optional columns: `premium_index` (close of `/fapi/v1/premiumIndexKlines`), `perp_mark`.

## 3. Fair values (model/fair_value.py)

Notation: `S` spot, `P` perpetual, `F` quarterly, `τ` years to expiry, `r` USD financing rate,
`r′ = y` coin yield, `κ = 1095` funding periods per year.

| Quantity | Formula | Source |
|---|---|---|
| Fair quarterly price | `F* = S·exp((r − r′)τ)` | cost of carry |
| Annualized basis | `b = ln(F/S)/τ` | — |
| Fair basis | `b* = r − r′` | — |
| **Excess basis** | `e_fut = b − b*` | Schmeling-Schrimpf-Todorov: carry = convenience yield |
| Fair perpetual price | `P* = κ/(κ − (r − r′))·S` | He-Manela-Ross-von Wachter Prop. 1 |
| Fair funding per period | `(P* − S)/S = (r − r′)/(κ − (r − r′)) ≈ (r − r′)/κ` | same |
| Annualized fair funding | `f* = κ·(r − r′)/(κ − (r − r′)) ≈ r − r′` | same |
| Trailing funding | `f̂ = mean over the trailing 30-day calendar window of per-print annualized funding`, each print annualized by its own interval (`rate × 8760 / interval_hours`, so 8h, 4h and 1h regimes are comparable); NaN until a full 30-day window exists, and NaN again if the funding feed stalls for more than one interval | 90 prints at 8h |
| **Excess funding** | `e_fund = f̂ − f*` | — |
| Annualized perp premium | `ρ = κ·ln(P/S)` | HMRW |
| Fair premium | `ρ* = κ·ln(κ/(κ − (r − r′))) ≈ r − r′` | HMRW |
| **Premium deviation** | `e_rm = ρ − ρ*` | HMRW |
| No-arbitrage band | `[κ·ln(1 − C), κ·ln(1 + C)]`, `C` = round-trip cost fraction | HMRW Prop. 2 |
| **Calendar excess** | `e_cal = b − f̂` | both are the cost of a synthetic long (AHJ random-maturity representation) |

## 4. Costs and hurdles

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

## 5. Strategies (model/signals.py)

All four share one rule shape: **enter** when `excess > hurdle`; **hold** while `excess > exit level`;
**exit** at or below the exit level once `min_hold = 8` bars (one funding interval) have elapsed. Decisions
at bar `t` use data up to `t`'s close; fills happen at `t`'s close price ± slippage.

| ID | Name | Legs for target = +1 | Excess | Exit level | Reverse? |
|---|---|---|---|---|---|
| S1 | cash_and_carry | long spot, short quarterly | `e_fut` | 0 | no (needs spot borrow) |
| S2 | funding_carry | long spot, short perpetual | `e_fund` | 0 | no |
| S3 | calendar | long perpetual, short quarterly | `e_cal` | 0 | **yes** (both legs are futures) |
| S4 | random_maturity | long spot, short perpetual | `e_rm` | 0 (first return to fair) | no |

Quarterly handling (S1, S3): enter the current quarter unless it is inside the roll window
(`dte < 2` days), else the next quarter. Inside the roll window a held position rolls into the next
quarter only if that contract clears its own hurdle; otherwise it exits. `cc_hold_to_expiry=False` by
default (sensitivity: `True`, where the quarterly settles at the delivery price and pays the delivery fee;
`cal_hold_to_expiry` is the S3 counterpart).

Edge rules (frozen with the code, 2026-10-18 review): a NaN excess is "no decision" (no entry; an open
position is held); `min_hold` counts from the original entry and a roll does not reset it; the S4 band is
asymmetric (`κ·ln(1 − C) < −κ·ln(1 + C)`) and only its upper edge is used because the reverse trade needs
a short-spot leg; the `allow_reverse` flag is reserved and has no effect on S1/S2/S4 in v1 (no borrow-cost
model); S3 reverse is governed by `cal_allow_reverse`.

Explicitly not done: parameter search on the full sample, sign flips, regime filters added after seeing
results, or symbol selection after seeing results. The only post-hoc analysis is the sensitivity grid in
§8, reported in full.

## 6. Backtest engine semantics (backtest/engine.py)

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

## 7. Metrics (backtest/metrics.py)

* Daily aggregation of P&L (UTC days), Sharpe = mean/std × √365 (24/7 market), reported net of all costs.
* Annualized return on capital, max drawdown (on equity), hit rate per round trip, average holding time,
  number of round trips, turnover, time-in-position, worst 30-day window.
* Year-by-year table and the five stress episodes named in the research log (Mar-2020, May-2021,
  Nov-2022 FTX, Aug-2024, Q1/Q2-2026 funding compression).
* "Theory vs market" diagnostics: time series of `b` vs `b*`, `f̂` vs `f*`, `ρ` vs `ρ*`, with the band.

## 8. Sensitivity grid (the only sanctioned variation)

`buffer ∈ {0, 3, 6, 10}%/yr`, `lookback ∈ {30, 90, 180}` prints, `leverage ∈ {1, 2, 3}`,
`hold_to_expiry ∈ {False, True}`, costs ∈ {VIP0, VIP0 with 25% BNB discount, VIP3}. The full grid is
reported; the pre-registered cell is marked. `N_configs` is recorded in the report so a reader can
discount the best cell.

## 9. Paper-live daemon (live/)

* Poll interval 60 s; sources: spot `bookTicker`, perp `bookTicker` + `premiumIndex` (mark, index,
  last funding, next funding time), quarterly `bookTicker`, funding history refreshed hourly.
* Signal path is **the same functions** as the backtest (`model/signals.py`) applied to a live row
  appended to the cached panel: research/live parity by construction.
* Paper broker: fills at bid/ask ± slippage; funding accrual when a stamp is crossed, using the actual
  settled rate from `/fapi/v1/fundingRate`; state in `state/{ROOT}/book.json`, append-only ledger
  `state/{ROOT}/events.jsonl`, heartbeat line every loop.
* Testnet broker: HMAC-signed REST to `testnet.binancefuture.com` (futures) and `testnet.binance.vision`
  (spot). The mainnet trading hosts do not appear anywhere in `live/broker.py`; a test asserts this.
* Kill switches (fail-secure, block on uncertainty): quote age > 5 min → no new entries; > 30 min →
  flatten (paper) and HALT; any exception in the decision path → SAFE mode (no new entries) until
  restart; equity drawdown > 5% → flatten and HALT; exchange `maintenance`/non-200 → SAFE.
* Nothing in this repository can be pointed at a real-money endpoint; promotion to real capital is a
  separate, deliberate decision outside this codebase.

## 10. Honesty policy for presenting results

* Backtests are presented as backtests; paper-live results as paper; testnet fills as testnet.
* Every number in `README.md` links to the report that produced it and the commit hash.
* Known weaknesses are listed, not buried: single-venue, taker-only costs, hourly settlement proxy, no
  exchange-risk model (FTX-type), constant ETH yield, isolated-margin liquidation proxy.
