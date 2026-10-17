# crypto_basis_arb

Theory-anchored basis trading on Binance: a **theoretical futures price** from the perpetual-futures
pricing literature and the cost-of-carry relation, compared with the **market basis**, to enter and exit
spot-vs-futures positions. Deterministic backtest engine, pre-registered rules, and a paper-live daemon
with Binance testnet order routing.

> Status: in progress — backtest engine and paper-live daemon implemented; the first pre-registered run
> (spec v1.0.2) is being produced. No real capital has been traded through this code, and the code cannot
> reach a real-money endpoint (see *Honesty and limits*).

## What it does

```
          theory                                   market                          action
 ┌─────────────────────────┐          ┌────────────────────────────┐
 │ F* = S·e^{(r−y)τ}       │  b − b*  │ quarterly basis b = ln(F/S)/τ │  ──▶  S1 long spot / short quarterly
 │ P* = κ/(κ−(r−r′))·S     │  f̂ − f* │ trailing funding f̂           │  ──▶  S2 long spot / short perpetual
 │ fair funding f* ≈ r − r′│  b − f̂  │ quarterly basis vs funding   │  ──▶  S3 long perpetual / short quarterly
 │ band κ·ln(1 ± C)        │  ρ − ρ* │ perp premium ρ = κ·ln(P/S)   │  ──▶  S4 HMRW random-maturity arbitrage
 └─────────────────────────┘          └────────────────────────────┘
```

* **Fair values** (`src/basis_arb/model/fair_value.py`): cost of carry for dated futures; the no-arbitrage
  perpetual price and equilibrium funding of He, Manela, Ross and von Wachter (arXiv:2212.06888); the
  random-maturity representation of Ackerer, Hugonnier and Jermann (arXiv:2310.11771, *Mathematical Finance*);
  Binance's funding formula. Reading notes with URLs: [`docs/theory.md`](docs/theory.md).
* **Signals** (`model/signals.py`): four strategies with one rule shape (enter above an amortized-cost hurdle,
  exit at fair value), frozen in [`docs/strategy_spec.md`](docs/strategy_spec.md) before the first full-sample run.
* **Backtest** (`backtest/`): 1h event loop with funding accrual at the actual stamps, fees and slippage per leg,
  quarterly expiry settlement and rolls, an isolated-margin liquidation monitor, and a P&L decomposition
  (basis convergence / funding / costs).
* **Paper-live** (`live/`): the same signal functions on live quotes, simulated fills at bid/ask, a JSON book
  and an append-only event ledger, fail-secure kill switches, and a testnet broker. Runbook:
  [`docs/operations.md`](docs/operations.md).
* **Data** (`data/`): keyless public endpoints only (Binance spot / USDⓈ-M / quarterly klines, funding history
  since 2019-09, FRED T-bill). [`docs/data_sources.md`](docs/data_sources.md).

## Results

<!-- RESULTS:BEGIN -->
Pre-registered cell of spec v1.0.2 (`git tag prereg-v1.0.2`): Binance VIP0 taker fees (10 / 5 bp) plus 2 bp
slippage per leg, 10,000 USDT per leg, 2× leverage on the futures leg (isolated for the spot-leg strategies,
cross for the futures pair), 2020-01-01 to 2026-10-17, hourly bars, same-bar fills. **Backtest, not live.**
Headline figures are *excess of the 3-month T-bill*, charged on the capital committed to the strategy even on
flat days (a rule that is rarely in position is penalised for idling its capital); raw figures assume idle cash
earns nothing. Full report, year-by-year and stress-episode tables, P&L decomposition and the sensitivity grid:
[`reports/BTCUSDT/summary.md`](reports/BTCUSDT/summary.md), [`reports/ETHUSDT/summary.md`](reports/ETHUSDT/summary.md)
(commit `36f7433`).

| | Strategy | Excess ann. return | Excess Sharpe | Raw Sharpe | Max DD | Round trips | Time in position |
|---|---|---:|---:|---:|---:|---:|---:|
| BTC | S1 cash-and-carry | 0.91% | 0.26 | 1.06 | 6.45% | 12 | 33% |
| BTC | S2 funding carry | **2.77%** | **3.14** | 6.68 | 2.00% | 5 | 45% |
| BTC | S3 calendar | −1.55% | −0.37 | 0.32 | 12.54% | 36 | 30% |
| BTC | S4 random-maturity | −1.36% | −2.24 | 2.59 | 1.32% | 8 | 2% |
| ETH | S1 cash-and-carry | 0.24% | 0.07 | 0.90 | 6.52% | 14 | 32% |
| ETH | S2 funding carry | **4.63%** | **4.03** | 6.73 | 1.25% | 4 | 52% |
| ETH | S3 calendar | −1.67% | −0.35 | 0.25 | 9.85% | 26 | 27% |
| ETH | S4 random-maturity | −0.40% | −0.44 | 2.81 | 0.78% | 10 | 3% |

What the numbers say:

* **Funding carry (S2)** is the only rule that beats the T-bill after costs, and its edge sits in 2020-21
  (BTC excess return 9.3% and 19.7%) and 2024 (0.7%); 2022-23 and 2025-26 are flat to negative in excess
  terms. The share of BTC funding prints pinned at the 0.01% contract rate fell from 50.5% (2020) to 6.7%
  (2026): the funding compression documented by Borri-Liu-Tsyvinski-Wu is visible in the raw prints.
* **Cash-and-carry (S1)** captured the 2021 and 2024 basis spikes (BTC raw return 13.8% and 8.1% in those
  years) and sat flat otherwise; its excess figure is small because committed capital idles for two thirds of
  the sample.
* **The calendar spread (S3)** loses after costs: trailing realized funding is a poor forecast of the
  quarterly basis, and 36 round trips at 28 bp each consume the term premium.
* **HMRW's random-maturity rule (S4)** barely trades at retail taker fees. At their fee levels it trades
  39-157 times with +1.4% to +3.3% excess per year (replication cells in the report), reproducing their point
  that fees decide this trade.
* No liquidation at 2× in-sample on close prices (peak margin utilization 0.56-0.68). One-bar-lag fills, three
  cost tiers, four buffers and three windows are in the sensitivity grid (`N_configs` = 12 per strategy plus
  3 replication cells); the pre-registered cell is not the best cell.
<!-- RESULTS:END -->

## Quick start

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"     # Windows; use .venv/bin/python elsewhere
.venv/Scripts/python.exe -m pytest -q -m "not network"
.venv/Scripts/python.exe -m basis_arb.data.panel --root BTCUSDT --start 2020-01-01
.venv/Scripts/python.exe -m basis_arb.backtest.run --root BTCUSDT --panel data/panel_BTCUSDT_1h.parquet --out reports/BTCUSDT
.venv/Scripts/python.exe -m basis_arb.live.daemon --root BTCUSDT --mode paper --once
```

## Honesty and limits

* Every number is labelled backtest, paper or testnet. Nothing here is a real-money track record.
* `src/basis_arb/live/broker.py` contains no mainnet trading host (a unit test asserts it); testnet keys are
  read from the environment only.
* Known simplifications: single venue; taker fees at VIP0; 1h bars with close-based fills; settlement proxied
  by the spot close at expiry; constant 3% ETH staking yield; T-bill as the stablecoin financing rate;
  isolated-margin liquidation as a proxy for the exchange's margin engine; no exchange-failure risk model.
* The carry documented by Schmeling-Schrimpf-Todorov (BIS WP 1087) is a risk premium that has compressed
  since 2024 (Borri-Liu-Tsyvinski-Wu, arXiv:2510.14435); results are reported by year and by stress episode
  for that reason.

## How this was built

The author directed an AI coding agent (Claude Code) and every commit carries the co-author trailer. The
author set the scope, froze the strategy rules before any full-sample run (`git tag prereg-v1.0.2`),
checked the theory mapping against the papers, commissioned two independent reviews (code-level and
engineering-level) and applied their findings before the cited run. The spec and the tests are the contract.

## License

MIT.
