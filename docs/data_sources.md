# Data sources

All sources are public and keyless. Verified reachable from the development machine on 2026-10-18
(HTTP 200 unless noted). Fetchers live in `src/basis_arb/data/`.

## Binance (primary)

| Need | Endpoint | Notes |
|---|---|---|
| Spot klines | `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h` | limit 1000/call, paginate with `startTime` |
| Perp klines | `GET https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h` | limit 1500/call |
| Quarterly klines | same endpoint, `symbol=BTCUSDT_YYMMDD` | **expired contracts are still served** (checked `BTCUSDT_210326`) |
| Funding history | `GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1000` | first print 2019-09-10 (`fundingTime` ms, `fundingRate`, `markPrice`); actual stamps, so the 4h/1h regimes introduced in 2025-26 are honored |
| Premium index klines | `GET https://fapi.binance.com/fapi/v1/premiumIndexKlines?symbol=BTCUSDT&interval=1h` | optional diagnostic |
| Live quotes | `GET /api/v3/ticker/bookTicker`, `GET /fapi/v1/ticker/bookTicker`, `GET /fapi/v1/premiumIndex` | `premiumIndex` returns mark, index, `lastFundingRate`, `interestRate`, `nextFundingTime` |
| Listed quarterlies | `GET https://fapi.binance.com/fapi/v1/exchangeInfo` → `contractType in {CURRENT_QUARTER, NEXT_QUARTER}`, `deliveryDate` | on 2026-10-18: `BTCUSDT_261225`, `BTCUSDT_270326`, `ETHUSDT_261225`, `ETHUSDT_270326` |
| Historical quarterly symbols | generated: last Friday of Mar/Jun/Sep/Dec from 2021-03, `ROOT_YYMMDD` | cross-checked against the Binance Vision bucket listing: `BTCUSDT_210326 … BTCUSDT_270326` (25 contracts) |
| Bulk history (optional) | `https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/1h/` | zipped CSV; faster than REST for 1m data, unnecessary at 1h |
| Testnet (futures) | `https://testnet.binancefuture.com/fapi/v1/ping` | 200 |
| Testnet (spot) | `https://testnet.binance.vision/api/v3/ping` | 200 |

Rate limits: REST weight 1200/min (spot) and 2400/min (futures); the client sleeps on `429`/`418`
and respects `Retry-After`.

Contract specifications used by the model (Binance support articles, read 2026-10-18):

* Funding: `F = [avg premium + clamp(I − avg premium, ±0.05%)] / (8/N)`, `I = 0.01%` per 8h, cap
  `±0.75 × maintenance margin ratio` for majors (BTC/ETH: ±0.30%); charged on mark-price notional to
  positions open at the stamp; default interval 8h, switching to 1h after a capped print (since 2025-05)
  and back to 4h after 16 calm prints (since 2026-01).
* Quarterly delivery: 08:00 UTC on the last Friday of the quarter month; settlement price = average of the
  index over the final 30 minutes (1 hour for contracts listed before 2024-11); delivery fee charged at the
  taker rate on settled notional; no new positions in the last 10 minutes.

## FRED (financing rate)

| Need | Endpoint | Notes |
|---|---|---|
| 3-month T-bill (discount basis) | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3` | keyless CSV, daily since 1954; used as `r` (annual decimal = value/100). Alternative used by HMRW: Aave stablecoin supply/borrow rates (not keyless-historical) |

## Alternatives checked (not used in v1)

| Venue | Endpoint | Status |
|---|---|---|
| Bybit | `GET https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT` | 200; candidate for a cross-venue extension |
| OKX | `GET https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` | 200 |
| Deribit | `GET https://www.deribit.com/api/v2/public/get_tradingview_chart_data?instrument_name=BTC-26MAR21&…` | 200 even for expired dated futures (coin-margined, inverse); candidate for a term-structure extension |
| Hyperliquid | `POST https://api.hyperliquid.xyz/info` | GET returns 405 (POST-only API) |

## Verification log

| Date | Check | Result |
|---|---|---|
| 2026-10-18 | all endpoints above, `curl` from the dev machine | 200 (Hyperliquid 405 on GET as expected) |
| 2026-10-18 | `fundingRate` depth | first row `fundingTime = 1568102400000` (2019-09-10 08:00 UTC) |
| 2026-10-18 | expired quarterly klines | `BTCUSDT_210326` 8h klines returned |
| 2026-10-18 | FRED DTB3 | last observation 2026-10-15 = 4.10 |

## Build log (2026-10-18)

**Rules after review (2026-10-18; both panels rebuilt from the cache with `--end "2026-10-17 20:00"`):** (1) DTB3 for date d applies from (d + 1 day) 00:00 UTC, never from d itself (next-business-day publication), so `r_usd` is exactly the earlier series shifted by 24h (range unchanged); (2) `max_ffill_hours` defaults to 5, so the plain command builds both panels: gaps above 5h still raise, every filled spot/perp row is logged at INFO, and filled rows keep NaN high/low, which the model treats as exchange-outage rows with no decision. Apart from `r_usd`, the only change in the rebuilt panels is ETH `y_coin`, now 0.00 per the current `config.RateInputs`.

Command: `python -m basis_arb.data.panel --root {BTCUSDT,ETHUSDT} --start 2020-01-01 --max-ffill-hours 5`,
run 2026-10-17 ~20:15 UTC (2026-10-18 KST). Output `data/panel_{ROOT}_1h.parquet` (4.6 / 4.2 MB), raw
cache `data/raw/` (23 MB, 56 files). Each build takes ~25 s cold (about 180 REST calls), ~3 s from the
cache. A second build from the cache with a fixed `--end` reproduces the saved panel exactly.

**The default command (`--max-ffill-hours 3`) stops by design for both roots.** Binance spot has three
gaps longer than 3h (exchange-wide, identical for BTCUSDT and ETHUSDT); the perpetual has no gap at all:

| Spot gap (first missing bar end, UTC) | Length | BTC perp move in gap | ETH perp move in gap | max abs ln(P/S) on filled rows, BTC / ETH |
|---|---|---|---|---|
| 2020-02-19 13:00 | 5h | 0.95% | 1.33% | 0.63% / 0.78% |
| 2020-12-21 15:00 | 4h | 1.66% | 1.30% | 2.25% / 1.45% |
| 2021-08-13 03:00 | 4h | 0.98% | 1.41% | 0.96% / 1.41% |

Twelve further spot gaps of 1-3h (2020-02-09, 03-04, 04-25, 06-28 (3h), 11-30, 12-25; 2021-02-11, 03-06,
04-20, 04-25 (3h), 09-29; 2023-03-24) are forward-filled under the default rule: 32 filled spot bars in
total. Filled rows carry the previous `spot_close` and NaN `spot_high`/`spot_low`, so they can be
identified downstream. Caveat for the backtest: a stale spot against a moving perp is a fake premium
(2.25% is about 24/yr in `kappa*ln(P/S)` units, against an S4 band edge of about 4.2/yr; even the 3h gap on
2021-04-25 gives 0.47%, about 5.1/yr). No spot-leg fill should happen on a row where `spot_high` is NaN.

| | BTCUSDT | ETHUSDT |
|---|---|---|
| rows (complete 1h grid) | 59,565 | 59,565 |
| first / last ts (UTC) | 2020-01-01 00:00 / 2026-10-17 20:00 | 2020-01-01 00:00 / 2026-10-17 20:00 |
| funding prints | 7,446 (none missing) | 7,446 (none missing) |
| first / last funding stamp | 2020-01-01 00:00 / 2026-10-17 16:00 | 2020-01-01 00:00 / 2026-10-17 16:00 |
| funding stamp hours | 00/08/16 only: 8h throughout, no 4h/1h regime has occurred | same |
| quarterly contracts with data | 25 (`BTCUSDT_210326` .. `BTCUSDT_270326`) | 25 (`ETHUSDT_210326` .. `ETHUSDT_270326`) |
| quarterly coverage (rows with `fut_cq_close`) | 2021-02-03 09:00 .. 2026-10-17 20:00 | 2021-02-04 08:00 .. 2026-10-17 20:00 |
| `fut_cq_close` NaN share: all rows / rows with a listed cq | 16.09% / 0.00% | 16.13% / 0.00% |
| cq switches (all exactly at ts == old expiry) | 23 | 23 |
| rows with a cq but no nq | 20,856 (24 spells) | 20,836 (24 spells) |
| annualized cq basis `ln(F/S)*365/dte`, all rows: min / median / max | -1157% / 5.66% / 1993% | -1484% / 5.13% / 1388% |
| same, dte >= 7 days: min / median / max | -49.1% / 5.65% / 80.6% | -37.8% / 5.04% / 95.7% |
| dte >= 7 days, median (max) by year | 2021 13.6% (80.6%), 2022 2.3% (10.4%), 2023 5.5% (21.8%), 2024 11.0% (47.4%), 2025 5.8% (14.9%), 2026 3.5% (8.9%) | 2021 14.6% (95.7%), 2022 0.8% (20.0%), 2023 4.6% (19.2%), 2024 11.2% (47.1%), 2025 5.2% (15.5%), 2026 2.2% (8.0%) |
| `r_usd` range | -0.05% .. 5.36% | -0.05% .. 5.36% |
| `y_coin` | 0.00 | 0.03 |

Notes:

* The unfiltered basis extremes come from the expiry tail: at dte = 1/24 day a few bp of premium
  annualizes to hundreds of percent (121 BTC / 127 ETH rows with abs(b) > 100%/yr, all with dte < 1 day).
* The dte >= 7 day extremes are real prints, not pipeline artifacts: BTC +80.6% on 2021-03-14 15:00
  (`BTCUSDT_210326` near the all-time high, F 61,573 vs S 60,002, dte 11.7); BTC -49.1% on 2021-09-07 15:00
  (crash bar); ETH +95.7% on 2021-02-20 14:00 (`ETHUSDT_210326`, two weeks after listing, 384 trades in
  the hour, 9% over spot, dte 33.8); ETH -37.8% on 2021-05-19 13:00 (crash).
* Listing regime (first bar of each contract): until mid-2023 the next quarterly was listed only 2-10
  days before the previous expiry, so nq is None for long spells (up to 91 days) in 2021-2023;
  `BTCUSDT_231229` was listed on 2023-08-18; from `BTCUSDT_240329` (listed 2023-09-29 08:00) each new
  contract lists exactly at the previous expiry, so cq and nq both exist at all times.
* Skipped symbols: `{ROOT}_270625` .. `{ROOT}_280929` (not listed yet; HTTP 400, code -1121). Cross-check
  with exchangeInfo: `261225` and `270326` are listed with deliveryDate equal to the generated expiry.
* Every quarterly has a bar in every hour it is listed (NaN share 0 when listed), and the perp has no
  missing hour since 2020.
* DTB3 for date d is published in the H.15 release on the next business day. Applying it from d 00:00
  UTC, as section 2 of the spec prescribes, is therefore about 1-1.5 days ahead of publication. That is
  negligible for a daily T-bill rate, but strictly speaking it is not "no look-ahead".
