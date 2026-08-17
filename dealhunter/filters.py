"""
Hard filters. A listing failing any of these is rejected outright and never
notified — it is still written to the database so you can audit what got
thrown away and why.

The filters are deliberately unforgiving. Every one of them encodes a spec
that makes the machine useless for its purpose:

  * < 16 GB RAM      - can't hold a quantised model plus a dev environment
  * < 8 GB VRAM      - can't fit anything worth running locally
  * < 1 TB storage   - model weights are enormous
  * < 1440 vertical  - the whole point of a 16" panel
  * > $1,400 landed  - out of budget, full stop
  * pre-30-series    - no useful tensor throughput
  * wrong keyboard   - unusable daily driver
"""

from __future__ import annotations

from .config import Config
from .models import (
    KeyboardLayout,
    LandedCost,
    Listing,
    ParsedSpecs,
    RejectReason,
)


def apply_hard_filters(
    listing: Listing,
    specs: ParsedSpecs,
    landed: LandedCost | None,
    config: Config,
    keyboard_layout: KeyboardLayout,
    keyboard_explicit: bool,
) -> list[RejectReason]:
    """Return every reason this listing should be rejected.

    An empty list means it passed. We collect *all* reasons rather than
    short-circuiting on the first, because when you're debugging why a
    promising listing vanished, "RAM_TOO_LOW + STORAGE_TOO_LOW" is a much more
    useful log line than "RAM_TOO_LOW".
    """
    limits = config.hard_filters
    reasons: list[RejectReason] = []

    # -- Region --------------------------------------------------------------
    if not config.region(listing.region).enabled:
        reasons.append(RejectReason.REGION_DISABLED)

    # A retailer that won't ship inside its own country is no use to us: the
    # forwarding contact has to be able to receive it domestically.
    if not listing.ships_domestically:
        reasons.append(RejectReason.NO_DOMESTIC_SHIPPING)

    # -- Parseability --------------------------------------------------------
    # We cannot honestly apply a spec filter to a spec we never extracted, so
    # anything missing a critical field is dropped rather than waved through.
    if limits.get("reject_unparseable", True):
        missing = [
            name
            for name, value in (
                ("gpu", specs.gpu_model),
                ("resolution", specs.resolution_h),
                ("ram", specs.system_ram_gb),
                ("storage", specs.storage_gb),
            )
            if value is None
        ]
        if missing:
            reasons.append(RejectReason.UNPARSEABLE)

    # -- Core specs ----------------------------------------------------------
    if specs.system_ram_gb is not None and specs.system_ram_gb < limits["min_system_ram_gb"]:
        reasons.append(RejectReason.RAM_TOO_LOW)

    # Note this uses the *resolved* VRAM, which for an ambiguous "RTX 5070" is
    # the pessimistic 8 GB. That is intentional: it keeps unverified listings
    # inside the pipeline (8 GB passes the filter) so they can be flagged for
    # manual confirmation rather than silently disappearing.
    if specs.vram_gb is not None and specs.vram_gb < limits["min_vram_gb"]:
        reasons.append(RejectReason.VRAM_TOO_LOW)

    if specs.storage_gb is not None and specs.storage_gb < limits["min_storage_gb"]:
        reasons.append(RejectReason.STORAGE_TOO_LOW)

    if specs.resolution_h is not None and specs.resolution_h < limits["min_vertical_resolution"]:
        reasons.append(RejectReason.RESOLUTION_TOO_LOW)

    if specs.gpu_generation is not None and specs.gpu_generation < limits["min_gpu_generation"]:
        reasons.append(RejectReason.GPU_TOO_OLD)

    # -- Budget --------------------------------------------------------------
    if landed is not None and landed.landed_usd > config.budget["hard_ceiling_usd"]:
        reasons.append(RejectReason.OVER_BUDGET)

    # -- Keyboard ------------------------------------------------------------
    if _keyboard_rejected(keyboard_layout, keyboard_explicit, config):
        reasons.append(RejectReason.KEYBOARD_LAYOUT)

    return reasons


def _keyboard_rejected(
    layout: KeyboardLayout, explicit: bool, config: Config
) -> bool:
    """True when the layout disqualifies the listing.

    The rule has two halves, and the `explicit` flag is what separates them:

      * A layout on the reject list (QWERTZ / AZERTY / Nordic) is always out.
        Stating it explicitly doesn't help — a QWERTZ board is a QWERTZ board.
      * German, Belgian and Swedish stock defaults to a rejected layout, so it
        only survives when the listing *explicitly* states a US or UK layout.
        That explicit statement is what sets `layout` to ANSI or ISO_UK in the
        first place, so no extra check is needed here.

    ANSI, ISO_UK, Canadian Multilingual and UNVERIFIED all pass; the last three
    carry score penalties and flags instead.
    """
    rejected = {
        KeyboardLayout(name) for name in config.hard_filters["rejected_keyboard_layouts"]
    }
    return layout in rejected


def describe_rejections(reasons: list[RejectReason]) -> str:
    """Compact log line, e.g. 'VRAM_TOO_LOW, OVER_BUDGET'."""
    return ", ".join(reason.value for reason in reasons)
