"""The committed 2024 sample slice must satisfy the panel contract and run through the model offline."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from basis_arb.config import BacktestConfig
from basis_arb.model import signals as sg
from basis_arb.types import validate_panel

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "panel_BTCUSDT_1h_2024.parquet"


@pytest.fixture(scope="module")
def sample() -> pd.DataFrame:
    if not SAMPLE.exists():
        pytest.skip("sample panel not present")
    return pd.read_parquet(SAMPLE)


def test_sample_passes_contract_and_covers_2024(sample):
    validate_panel(sample)
    assert len(sample) == 8784  # leap year, complete 1h grid
    assert sample.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert sample["funding_rate"].notna().sum() == 3 * 366
    assert sample["fut_cq_symbol"].map(lambda s: isinstance(s, str) and s.startswith("BTCUSDT_")).all()


def test_sample_model_outputs_are_plausible(sample):
    cfg = BacktestConfig()
    p = sg.enrich(sample, cfg)
    basis = p["basis_cq_ann"].dropna()
    assert 0.0 < basis.median() < 0.30  # 2024 BTC quarterly basis: a few percent to ~20% annualized
    assert (p["r_usd"].between(0.03, 0.06)).all()  # 2024 T-bills
    targets = sg.build_targets(sample, cfg)
    assert set(targets) == {"cash_and_carry", "funding_carry", "calendar", "random_maturity"}
    # trailing funding is defined after the 30-day warm-up and never leaks before it
    assert p["funding_trailing_ann"].iloc[: 24 * 29].isna().all()
    assert np.isfinite(p["funding_trailing_ann"].iloc[24 * 31 :]).all()
