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
