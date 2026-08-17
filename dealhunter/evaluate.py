"""
The evaluation pipeline: raw `Listing` in, `EvaluatedListing` out.

    parse -> resolve keyboard -> landed cost -> hard filters -> score

This is the single place where the stages are wired together, so a source
module never has to know about scoring and the scorer never has to know where
a listing came from. Stage 2 (eBay) will call `evaluate_all` and nothing else.
"""

from __future__ import annotations

import logging

from .config import Config
from .filters import apply_hard_filters
from .fx import FxRates
from .models import EvaluatedListing, Listing
from .parsing import parse_listing
from .regions import compute_landed_cost, prime_keyboard_defaults, resolve_keyboard_layout
from .scoring import score_listing

log = logging.getLogger(__name__)


def evaluate(
    listing: Listing,
    config: Config,
    rates: FxRates,
    floor_override_usd: float | None = None,
) -> EvaluatedListing:
    """Run one listing through the whole pipeline.

    A rejected listing keeps its parsed specs and landed cost so the database
    row is still useful for debugging; it just never gets a score and never
    gets notified.
    """
    prime_keyboard_defaults(config)

    specs = parse_listing(listing.searchable_text, config)
    layout, explicit = resolve_keyboard_layout(listing)

    # Facts only the source could know — no amount of title parsing reveals
    # that a listing has hidden price variations.
    for flag in listing.source_flags:
        specs.add_flag(flag)

    # Landed cost is computed before filtering because the budget filter needs
    # it. A currency/region mismatch is a source-module bug, so it surfaces as
    # an UNPARSEABLE rejection rather than taking the whole run down.
    try:
        landed = compute_landed_cost(listing, config, rates, specs)
    except (ValueError, KeyError) as exc:
        log.warning("Could not compute landed cost for %s: %s", listing.fingerprint(), exc)
        landed = None

    reasons = apply_hard_filters(listing, specs, landed, config, layout, explicit)

    evaluated = EvaluatedListing(
        listing=listing, specs=specs, landed=landed, reject_reasons=reasons
    )
    if reasons or landed is None:
        return evaluated

    evaluated.score = score_listing(
        listing, specs, landed, config, layout, floor_override_usd
    )
    return evaluated


def evaluate_all(
    listings: list[Listing],
    config: Config,
    rates: FxRates,
    floors: dict[str, float] | None = None,
) -> list[EvaluatedListing]:
    """Evaluate a batch, newest-best first.

    `floors` maps a model key to a floor that the database has driven below the
    config seed. Sorted by score descending, so the caller can take the top N
    straight off the front.
    """
    floors = floors or {}
    results = []

    for listing in listings:
        override = floors.get(model_key_for(listing, config))
        results.append(evaluate(listing, config, rates, override))

    results.sort(
        key=lambda e: (e.score.total if e.score else -1),
        reverse=True,
    )
    return results


def model_key_for(listing: Listing, config: Config) -> str:
    """Model key for a listing, or '' when no known model matches.

    Cheap enough to run twice (here and inside `evaluate`) and it keeps
    `evaluate_all`'s floor lookup readable.
    """
    model = config.model_for_title(listing.searchable_text)
    return model.key if model else ""
