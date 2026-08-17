"""
robbin-the-hood — a self-hosted gaming-laptop deal hunter.

Stage 1 (this package as it stands) is the reasoning core: data models,
spec parsing with the trap validators, landed-cost normalisation across seven
regions, hard filters, and the 0-100 scoring engine. It has no network
dependencies and is fully unit-tested.

Later stages add sources (eBay first), notification, and scheduling on top,
without changing anything here.
"""

__version__ = "0.1.0"

from .config import Config, load_config
from .evaluate import evaluate, evaluate_all
from .fx import FxRates, FxService, static_rates
from .models import (
    Condition,
    EvaluatedListing,
    Flag,
    KeyboardLayout,
    LandedCost,
    Listing,
    PanelType,
    ParsedSpecs,
    Region,
    RejectReason,
    ScoreResult,
)

__all__ = [
    "Config",
    "load_config",
    "evaluate",
    "evaluate_all",
    "FxRates",
    "FxService",
    "static_rates",
    "Condition",
    "EvaluatedListing",
    "Flag",
    "KeyboardLayout",
    "LandedCost",
    "Listing",
    "PanelType",
    "ParsedSpecs",
    "Region",
    "RejectReason",
    "ScoreResult",
]
