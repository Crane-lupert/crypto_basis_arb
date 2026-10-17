# NEXT — resume checklist

Updated 2026-10-18 after the v1.0.2 run. All layers are complete and tested (197 offline tests); the
pre-registered results are in `reports/` (commit `36f7433`) and summarized in `README.md`.

## 0. First commands in a new session

```
cd D:/vscode/crypto_basis_arb
git log --oneline | head
.venv/Scripts/python.exe -m pytest -q -m "not network"
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m basis_arb.live.daemon --root BTCUSDT --mode paper --once
```

## 1. Operator decisions (not code)

1. Publish: `git push` to a new public GitHub repository (the local config denies push by design; do it from a
   normal shell). Before pushing, re-read `README.md` and confirm the commit hash in the results block.
2. Start the paper daemon as a scheduled task (`docs/operations.md`); review after 14 days
   (`state/BTCUSDT/paper/events.jsonl`, HEARTBEAT/SAFE/HALT counts) and add a research-log entry.
3. Testnet keys (`BINANCE_TESTNET_API_KEY/SECRET` in the environment only) → `--mode testnet --once` once,
   record the output in the research log.

## 2. Small engineering items (optional, in priority order)

1. Lint: make `ruff check src tests` clean in CI (fix remaining findings in `backtest/`, `live/`, `data/` or
   pin the rule set in `pyproject.toml`).
2. Apply the §6 resize rule in the paper daemon (currently fixed coin quantity per position).
3. Merge `live/history.py` into `data/` with a parity test (same panel from both paths).
4. Mark-price klines (`/fapi/v1/markPriceKlines`) for the liquidation monitor instead of the close proxy.
5. Replace the constant ETH staking diagnostic with a time series; convert DTB3 discount yield to a
   continuously-compounded rate (+8-11 bp).
6. Full sensitivity grid (`--grid full`) once, overnight.

## 3. Where the design lives

- Spec (frozen rules, v1.0.2): `docs/strategy_spec.md`; theory: `docs/theory.md`; data: `docs/data_sources.md`
- Decisions and results log: `docs/research_log.md`; runbook: `docs/operations.md`
- Meta-harness audit (design, gates, interview narrative, user questions):
  `D:/vscode/meta-harness/audits/2026-10-18-crypto_basis_arb-design-and-bootstrap.md`
