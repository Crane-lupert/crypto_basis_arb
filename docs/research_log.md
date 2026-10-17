# Research log

Dated, append-only. Decisions are recorded before results whenever possible.

## 2026-10-18 — project start, pre-registration

* Scope fixed: Binance spot / USDⓈ-M perpetual / USDⓈ-M quarterly, BTC and ETH, 1h UTC grid, 2020-01-01 to date.
* Theory anchors checked against publisher pages (see `docs/theory.md`): He-Manela-Ross-von Wachter
  (arXiv:2212.06888), Ackerer-Hugonnier-Jermann (arXiv:2310.11771 / Mathematical Finance),
  Angeris-Chitra-Evans-Lorig (arXiv:2209.03307 / SIAM J. Fin. Math.), Schmeling-Schrimpf-Todorov
  (BIS WP 1087). Binance funding and delivery specifications read from the support articles.
* Data endpoints verified keyless and reachable; funding history available from 2019-09-10; quarterly
  contracts from `BTCUSDT_210326`; expired-contract klines still served (`docs/data_sources.md`).
* Strategy rules, costs, hurdles and the sensitivity grid frozen in `docs/strategy_spec.md` v1.0
  **before** any full-sample backtest. Thresholds are cost-derived (round trip amortized over the
  holding horizon + 3%/yr buffer), not fitted.
* Fair-value module and signal state machines written with unit tests (23 tests) before the data layer,
  engine and daemon were implemented.
* Honesty policy adopted (`docs/strategy_spec.md` §10): backtest, paper and testnet results are labelled as
  such; no real-money results are claimed anywhere in this repository.

## 2026-10-18 — model review fixes (before any full-sample run)

A first-pass code review of `model/` produced 16 findings; the behaviour-relevant ones were fixed and
the spec updated in the same commit (`31d7787`), still before the data layer or engine existed:

* Trailing funding is now a 30-day **calendar** window of per-print annualized rates (each print annualized
  by its actual interval), so Binance's 4h/1h funding regimes no longer understate `f̂`; a full window is
  required (the first 30 days of the sample carry no S2/S3 decision) and a stalled feed yields NaN.
* NaN excess = no decision in both state machines (previously the simple loop exited immediately).
* `min_hold` counts from entry; a roll no longer resets it. ROLL rows report the next contract's values.
* S1/S2/S4 cannot reverse regardless of the flag (no borrow-cost model); S4's lower band edge is kept
  asymmetric for documentation only.
* `validate_panel` now rejects non-UTC indexes, non-positive futures closes, percent-unit rates and
  `dte_days` that disagree with `expiry − ts`.
* Synthetic panel fixture now promotes the next quarter to current at expiry, like the real builder.

Next entries: panel build stats, first full-sample results (pre-registered cell), sensitivity grid,
paper-live start date.
