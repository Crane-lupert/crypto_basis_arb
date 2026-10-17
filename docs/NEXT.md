# NEXT — resume checklist (written 2026-10-18 late night, before a usage-limit cutoff)

State at the last checkpoint commit ("WIP checkpoint"): contract layer + data layer complete and tested;
backtest engine and live daemon were being written by two agents and may be partial on disk; an
engineering review produced fixes, some applied, some pending (list below). Real panels exist in
`data/panel_BTCUSDT_1h.parquet` and `data/panel_ETHUSDT_1h.parquet` (gitignored; rebuild with the CLI).

## 0. First commands in a new session

```
cd D:/vscode/crypto_basis_arb
git log --oneline | head
.venv/Scripts/python.exe -m pytest -q -m "not network"          # expect model+data tests green; engine/live may fail if partial
.venv/Scripts/python.exe -m ruff check src tests
ls src/basis_arb/backtest src/basis_arb/live                      # what the agents left
```

## 1. Pending from the engineering review (apply before the first full-sample run)

Done in code (commit "WIP checkpoint"): held-expiry keyed contract loop (no phantom EXPIRY/ROLL on data
holes); time-weighted trailing funding (sum of prints x 365/W); outage rows = no decision; calendar excess
on ln(F/P); validate_panel enforces a complete 1h grid and per-slot all-or-nothing symbol/expiry/dte;
ETH y = 0 in decisions (0.03 diagnostic only); `MarginModel.futures_pair_mode`; `StrategyParams.fill_lag_bars`.

Still to do:
1. Tests for the new contract logic (all should pass with the current code, add them):
   - mixed cadence: 20 days of 8h prints at 1e-4 then 10 days of 1h prints at 3.75e-4 -> f_hat = 0.096 x 365/30 = 1.168
   - data hole on a held contract (blank symbol, dte or close for one bar at ~40 dte) -> HOLD, contract unchanged;
     EXPIRY only at the first ts >= held expiry
   - validate_panel rejects a grid hole (drop 5 rows) and a row with expiry present but symbol NaN
   - signal causality: perturb every column after bar k -> (target, contract, reason) up to k unchanged, all 4 strategies
   - hold_to_expiry: no EXIT inside the roll window
2. Engine (if the agent did not finish): excess-of-r headline metrics (daily excess P&L = P&L - r_t/365 x capital),
   S3 under `futures_pair_mode`, `fill_lag_bars` axis, S4 HMRW maker-tier cells (2.25/0.18 bp and 6.75/1.44 bp, no slippage),
   funding-print-at-0.0001 share per year diagnostic, test "excess Sharpe = 0 when P&L == r/365 x capital".
3. Daemon (if the agent did not finish): decisions only on closed 1h bars; stale > 30 min -> HALT without flatten;
   drawdown -> SAFE (flatten only on margin breach); testnet delivery contracts -> UnsupportedOnTestnet; replay test.
4. Data: confirm the DTB3 shift (observation d applies from d+1 00:00 UTC) and the 5h gap-fill default landed in
   `data/rates.py` / `data/panel.py` (agent was applying them at cutoff); rebuild ETH panel so y_coin = 0:
   `python -m basis_arb.data.panel --root ETHUSDT --start 2020-01-01` (cached, fast). Add alignment tests:
   kline open_time -> bar end mapping (+1h), fundingTime floored to the hour.
5. Docs: spec section 3 (f_hat definition), section 6 (excess-of-r, S3 cross margin), section 7 (excess Sharpe headline),
   section 8 (fill-lag axis, S4 maker tiers), section 9 (closed-bar decisions, kill-switch changes), section 10 ("how this
   was built": AI coding agent directed by the author, every commit labelled, independent review commissioned).
   theory.md: 0.01% is a contract term (done in fair_value docstring; mirror it), label the AHJ exponential case as
   our own derivation, replace the unsupported "stablecoin spread near zero" sentence, say HMRW fee tiers are
   exchange fee levels in bp (2.25/0.18, 4.5/0.72, 6.75/1.44) without calling them maker or taker, cite the funding
   interval rules only as "support article 360033525031 as read on 2026-10-18".
6. Public hygiene: move the maintainer-only lines of CLAUDE.md (session model, token efficiency, D:/ paths, alerts,
   governance pointers) into `CLAUDE.local.md` (already gitignored); `.claude/hooks/asof-drift-check.py` is gitignored.
   README: status line "in progress" until results exist; add "How this was built".
7. Then: `git tag prereg-v1.0.2`, run `python -m basis_arb.backtest.run --root BTCUSDT --panel data/panel_BTCUSDT_1h.parquet --out reports/BTCUSDT --grid small`
   (and ETHUSDT), fill README results with report links + commit hash, append section 9 of the meta-harness audit.
8. Later: commit a small sample panel slice (e.g. 2024, with checksum) so tests/demos run where Binance returns HTTP 451.

## 2. Where the design lives

- Spec (frozen rules): `docs/strategy_spec.md`; theory: `docs/theory.md`; data: `docs/data_sources.md` (Build log at the end)
- Decisions log: `docs/research_log.md`
- Meta-harness audit (design, gates, interview narrative, user questions):
  `D:/vscode/meta-harness/audits/2026-10-18-crypto_basis_arb-design-and-bootstrap.md`
