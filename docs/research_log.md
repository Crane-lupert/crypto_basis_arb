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

Next entries: panel build stats, first full-sample results (pre-registered cell), sensitivity grid,
paper-live start date.
