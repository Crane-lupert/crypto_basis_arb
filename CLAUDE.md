# CLAUDE.md — crypto_basis_arb

> Theory-anchored crypto basis trading: perpetual fair funding (He-Manela-Ross-von Wachter) and dated-futures
> cost of carry vs the market basis on Binance, with a deterministic backtest engine and a paper-live daemon.
> Public portfolio repository. **No real-money path exists here by construction.**

## 0. Project context

- **Instruments**: Binance `BTCUSDT`, `ETHUSDT` — spot, USDⓈ-M perpetual, USDⓈ-M quarterly delivery.
- **Horizon**: 1h research grid; positions held hours to months (basis convergence, funding accrual).
- **Capital mode**: none. Paper fills on live quotes, plus order routing to the Binance **testnet** only.
- **Standalone**: no imports from other local repositories; data, validation and risk code live here.
- **Source of truth**: `docs/strategy_spec.md` (v1.0.2, tag `prereg-v1.0.2`) outranks code. Provenance of every
  formula: `docs/theory.md`. Endpoints and contract specs: `docs/data_sources.md`. Decisions: `docs/research_log.md`.
  Resume checklist when work was interrupted: `docs/NEXT.md`.

## 1. Hard rules (verifiable)

1. **Spec over code.** A change to a threshold, cost or rule needs a dated `docs/research_log.md` entry and a
   re-run that reports old and new configurations side by side. Never edit the spec to match code silently.
2. **Pre-registration.** No parameter search on the full sample. The only sanctioned variation is the
   sensitivity grid in `docs/strategy_spec.md` §8, reported in full with `N_configs`.
3. **No mainnet trading host in `src/basis_arb/live/broker.py`.** The strings `api.binance.com` and
   `fapi.binance.com` must not appear there (`tests/test_live_broker.py` asserts it). Testnet keys come only
   from `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`; never write a key to any file.
4. **Fail-secure in `live/`.** Uncertainty resolves to SAFE (no new entries) or HALT (stop without trading
   blind), never to "allow". No permissive fallback for malformed data, timeouts or partial responses.
5. **Honest labels.** Every result is labelled backtest, paper or testnet. A number in `README.md` links to
   the report and commit that produced it. No real-money results are claimed anywhere.
6. **Sharpe = daily aggregation × √365 on excess-of-T-bill P&L.** Per-bar Sharpe is forbidden; raw (non-excess)
   figures are secondary.
7. **Research/live parity.** The live decision path imports `basis_arb.model.signals` and decides only on closed
   1h bars; no re-implementation.
8. **Determinism.** `run_backtest(panel, cfg)` is a pure function; two runs produce identical equity series and
   byte-identical trade CSVs (`tests/test_engine.py`).
9. **Panel contract.** Any frame handed to the model or engine passes `basis_arb.types.validate_panel`.
10. **LLM role boundary.** The assistant writes code, tests and docs and reviews results. No LLM output is part of
    the decision or order path; the daemon is deterministic code.
11. **Tests gate commits.** `pytest -m "not network"` must be green; network tests (`-m network`) run manually.
12. **Dependencies**: numpy (≥2), pandas (<3), pyarrow, requests, matplotlib only. Adding one needs a research-log entry.
13. **Runtime state is not edited by hand.** `state/**` and `.env*` are written only by the daemon and the
    operator; the `.claude/hooks/guard_runtime_paths.py` hook blocks editor writes there.

## 2. Layout

```
src/basis_arb/
  config.py          constants, CostModel, MarginModel, StrategyParams, BacktestConfig (frozen defaults)
  types.py           panel schema contract, STRATEGY_LEGS, Fill, LegPosition
  model/fair_value.py cost of carry, HMRW perpetual pricing, no-arbitrage band, Binance funding formula
  model/signals.py   enrich() + four pre-registered state machines -> target frames
  data/              Binance + FRED fetchers, parquet cache, panel builder (python -m basis_arb.data.panel)
  backtest/          engine, metrics, report, sensitivity, run (python -m basis_arb.backtest.run)
  live/              history, feed, state, broker (Paper / Testnet), daemon (python -m basis_arb.live.daemon)
tests/               offline unit tests; conftest.make_panel is the synthetic panel generator
docs/                strategy_spec, theory, data_sources, operations, research_log, NEXT
data/ state/         gitignored runtime artefacts; reports/ holds committed backtest output
```

## 3. Commands

```
.venv/Scripts/python.exe -m pytest -q -m "not network"          # unit tests
.venv/Scripts/python.exe -m basis_arb.data.panel --root BTCUSDT --start 2020-01-01
.venv/Scripts/python.exe -m basis_arb.backtest.run --root BTCUSDT --panel data/panel_BTCUSDT_1h.parquet --out reports/BTCUSDT --grid small
.venv/Scripts/python.exe -m basis_arb.live.daemon --root BTCUSDT --mode paper --once
```

Environment: `uv venv .venv --python 3.11 && uv pip install --python .venv/Scripts/python.exe -e ".[dev]"`.

## 4. Working in this repo with an AI coding assistant

- **Think before coding**: read `docs/strategy_spec.md` for the touched layer first; state assumptions in the
  research log; prefer the smallest change that satisfies the spec (think before coding / simplicity first /
  surgical changes / goal-driven execution).
- **TDD**: a failing test in `tests/` precedes any non-trivial change to `model/`, `backtest/` or `live/`.
- **Sub-agents**: one agent = one module; main session orchestrates. `data/`, `backtest/`, `live/` can be edited
  concurrently; `config.py`, `types.py`, `model/` are the shared contract and change only through the main
  session with tests updated in the same commit.
- **Long runs**: panel builds and grids write to `data/` and `reports/`; re-running is idempotent (cache +
  deterministic engine), so an interrupted run is simply restarted.
- Maintainer-only notes (session model, token policy, alerting, governance pointers) live in the gitignored
  `CLAUDE.local.md`.

## 5. Abandon criteria (declared per phase; ≥ 3 triggers)

| Category | Trigger | Action |
|---|---|---|
| data-validity | panel rows < 95% of the expected hourly grid, or `fut_cq_close` NaN share > 20% after 2021-03 | fix the fetcher or drop the instrument; log it |
| mechanism | pre-registered cell: excess Sharpe < 0 on ≥ 2 of the 3 spot-leg strategies with ≥ 10 round trips each | publish the negative result as the deliverable; no re-tuning |
| live | paper daemon HALTs > 3 times in 14 days | stop, root-cause, fix before any testnet session |
| resource | > 2 weeks of further work without a new committed deliverable | freeze the repo in its current state |

## 6. Not in scope

Real-money execution (any venue), cross-venue arbitrage, options, LLM-generated signals, parameter search,
reverse cash-and-carry with borrowed spot (flag reserved, off), staking of the spot leg, market making.
