# NEXT — resume checklist

Updated 2026-10-18 after the cost/latency diagnostics. All layers are complete and tested (197 offline tests);
the pre-registered results are in `reports/` (commit `36f7433`), the cost/latency diagnostics in
`reports/cost_latency/` (commit `454937d`), both summarized in `README.md` and `README_kr.md`.

## 0. First commands in a new session

```
cd D:/vscode/crypto_basis_arb
git log --oneline | head
.venv/Scripts/python.exe -m pytest -q -m "not network"
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m basis_arb.live.daemon --root BTCUSDT --mode paper --once
```

## 1. Operator decisions (not code)

1. Publish: create the public GitHub repository and push (done from meta-harness with `gh`; the local config
   denies push by design). Before pushing, re-read `README.md` and confirm the commit hashes it cites.
2. Start the paper daemon as a continuous service (planned on the cloud host, `docs/operations.md` §9); review
   after 14 days (`state/{ROOT}/paper/events.jsonl`, HEARTBEAT/SAFE/HALT counts) and add a research-log entry.
   Only then does README describe paper operation as done.
3. Testnet keys (`BINANCE_TESTNET_API_KEY/SECRET` in the environment only) → `--mode testnet --once` once,
   record the output in the research log.

## 2. Research (pre-register before any result)

1. Passive-quote S4 (research log, 2026-10-18 "cost and latency diagnostics"): quote around the HMRW fair
   premium, fills by trade-through on 1-minute data, adverse selection by post-fill markouts; evaluate on the
   paper daemon's forward bid/ask snapshots. Write the spec v1.1 section and the research-log entry first.

## 3. Small engineering items (optional, in priority order)

1. Apply the §6 resize rule in the paper daemon (currently fixed coin quantity per position).
2. Merge `live/history.py` into `data/` with a parity test (same panel from both paths).
3. Mark-price klines (`/fapi/v1/markPriceKlines`) for the liquidation monitor instead of the close proxy.
4. Replace the constant ETH staking diagnostic with a time series; convert DTB3 discount yield to a
   continuously-compounded rate (+8-11 bp).
5. Full sensitivity grid (`--grid full`) once, overnight.

## 4. Where the design lives

- Spec (frozen rules, v1.0.2): `docs/strategy_spec.md`; theory: `docs/theory.md`; data: `docs/data_sources.md`
- Decisions and results log: `docs/research_log.md`; runbook: `docs/operations.md`
- Meta-harness audit (design, gates, interview narrative, user questions):
  `D:/vscode/meta-harness/audits/2026-10-18-crypto_basis_arb-design-and-bootstrap.md`
