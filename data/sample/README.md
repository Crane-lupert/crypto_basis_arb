# Sample panel (offline demos and tests)

`panel_BTCUSDT_1h_2024.parquet`: 8784 hourly rows, 2024-01-01 00:00:00+00:00 to 2024-12-31 23:00:00+00:00 UTC, built by
`python -m basis_arb.data.panel` on 2026-10-18 from Binance public klines/funding and FRED DTB3, then sliced.
sha256: `ebff81b67c3256b00bbe9a5aac90f438f5996dc6f16898ec84fb900b745d98bb`

Binance returns HTTP 451 to some regions (e.g. US IPs); this slice lets the backtest CLI and tests
run without network access. It is not a citable sample: the research grid starts 2020-01-01.
