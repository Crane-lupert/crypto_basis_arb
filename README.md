# crypto_basis_arb

Theory-anchored basis trading on Binance: a **theoretical futures price** from the perpetual-futures
pricing literature and the cost-of-carry relation, compared with the **market basis**, to enter and exit
spot-vs-futures positions. Deterministic backtest engine, pre-registered rules, and a paper-live daemon
with Binance testnet order routing.

> Status: backtest + paper-live. No real capital has been traded through this code, and the code cannot
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
Pending: the first pre-registered run is written to `reports/` by `python -m basis_arb.backtest.run` and
summarized here with a link to the report and the commit hash.
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

## License

MIT.
