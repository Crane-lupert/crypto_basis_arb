"""Data layer: Binance + FRED fetchers, a parquet cache, and the aligned 1h research panel.

Modules
-------
binance  keyless Binance REST client and fetchers (klines, funding, premium index, quarterly calendar)
rates    FRED DTB3 financing rate r and the coin-yield constant r_prime
cache    parquet cache under data/raw/ with incremental (tail) updates
panel    build / validate / save / load the panel; CLI ``python -m basis_arb.data.panel``

Submodules are deliberately not imported here, so ``python -m basis_arb.data.panel`` runs cleanly.
"""
