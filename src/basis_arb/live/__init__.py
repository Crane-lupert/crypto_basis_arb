"""Paper-live layer (strategy_spec.md section 9).

Modules
-------
history   minimal read-only recent 1h panel from keyless mainnet public endpoints (to merge with data/)
feed      live quote snapshot (bookTicker, premiumIndex, exchangeInfo) and the live panel row
state     Book (positions, cash, P&L components, counters), atomic book.json, append-only events.jsonl
broker    PaperBroker (deterministic fills on live quotes) and TestnetBroker (signed REST, testnet hosts only)
daemon    the loop: history -> feed -> funding -> expiry -> model.signals.build_targets -> reconcile -> MTM

Nothing here is imported eagerly, so `python -m basis_arb.live.daemon` runs without import side effects.
No module in this package can send an order to a real-money endpoint.
"""

__all__ = ["broker", "daemon", "feed", "history", "state"]
