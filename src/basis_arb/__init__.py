"""crypto_basis_arb — theory-anchored crypto basis trading research + paper-live stack.

Layers
------
data/      public exchange + FRED fetchers, parquet cache, aligned 1h panel
model/     fair values (cost-of-carry, perpetual no-arbitrage pricing) and signal state machines
backtest/  event-driven 1h engine with funding accrual, fees, slippage, margin monitor; metrics; reports
live/      paper-live daemon (simulated fills on live quotes) and Binance *testnet* order routing
"""

__version__ = "0.1.0"
