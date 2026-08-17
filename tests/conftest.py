"""
Shared pytest fixtures.

The FX rates here are **fixed**, not live. That is the point: a test that
depends on today's GBP/USD rate is a test that fails on a Tuesday. The rates
chosen are realistic enough that the landed-cost assertions read sensibly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dealhunter.config import load_config          # noqa: E402
from dealhunter.fx import static_rates             # noqa: E402
from dealhunter.regions import prime_keyboard_defaults  # noqa: E402

# Fixed local -> USD rates. Deliberately close to real mid-2026 levels.
TEST_FX = {
    "USD": 1.0,
    "CAD": 0.73,
    "GBP": 1.27,
    "EUR": 1.09,
    "SEK": 0.095,
    "AUD": 0.66,
}


@pytest.fixture(scope="session")
def config():
    """The real config.yaml. Tests assert against shipped values on purpose —
    if someone retunes a weight, the tests should notice."""
    cfg = load_config()
    prime_keyboard_defaults(cfg)
    return cfg


@pytest.fixture(scope="session")
def rates():
    return static_rates(TEST_FX)
