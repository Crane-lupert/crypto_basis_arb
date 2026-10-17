# NEXT — resume checklist

Updated 2026-10-18 (after the token reset). Spec **v1.0.2** is tagged (`prereg-v1.0.2`); the model, data,
engine and live layers are complete on disk; the engine and daemon were being updated to the v1.0.2 rules
(see below). Panels: `data/panel_{BTCUSDT,ETHUSDT}_1h.parquet` (gitignored; rebuild with the CLI, cached).

## 0. First commands in a new session

```
cd D:/vscode/crypto_basis_arb
git log --oneline | head
.venv/Scripts/python.exe -m pytest -q -m "not network"
.venv/Scripts/python.exe -m ruff check src tests
```

## 1. In flight at the last checkpoint

- **Engine (backtest/)**: v1.0.2 items (a)-(j): excess-of-r headline metrics, close-based liquidation + intrabar
  diagnostic, S3 `futures_pair_mode`, 25% resize rule at funding stamps, capital re-measured each bar,
  `fill_lag_bars`, cost tiers incl. HMRW replication cells, funding-print-at-0.01% diagnostic, test updates,
  then regenerate `reports/{BTCUSDT,ETHUSDT}` with `--grid small`. If `reports/*/summary.md` is older than the
  `prereg-v1.0.2` tag, the regeneration did not finish: run
  `python -m basis_arb.backtest.run --root BTCUSDT --panel data/panel_BTCUSDT_1h.parquet --out reports/BTCUSDT --grid small`
  (and ETHUSDT) after checking `src/basis_arb/backtest/engine.py` implements the items above.
- **Daemon (live/)**: closed-bar decision input, HALT without flatten on stale quotes, drawdown -> SAFE, per-mode
  state dirs + UNSUPPORTED action on testnet, replay test, operations.md update, paper `--once` smoke.

## 2. After both land

1. Full offline suite green; ruff clean; commit.
2. README results block: pre-registered cell headline (excess Sharpe, excess ann. return, max DD, round trips,
   time in position, liquidations/resizes) for both roots, year table pointer, link to `reports/<ROOT>/summary.md`
   and the commit hash; keep "superseded v1.0 run" out of the README.
3. Meta-harness audit §9.1/§9.2 (P1/P2 verification) in
   `D:/vscode/meta-harness/audits/2026-10-18-crypto_basis_arb-design-and-bootstrap.md`.
4. `docs/research_log.md`: results entry (pre-registered cell + grid N_configs + what surprised).
5. Optional: merge `live/history.py` duplication into `data/`; commit a 2024 sample slice (`data/sample/`) so tests
   and demos run where Binance returns HTTP 451 (done if `data/sample/` exists).
6. User decisions (audit §11): GitHub push (T3), paper daemon start, testnet keys, Vultr log sync, S03 attribution fix.
