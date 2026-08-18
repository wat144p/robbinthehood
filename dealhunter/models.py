"""
Core data types for the deal hunter.

Everything that flows through the system is one of four things:

    Listing        - raw facts a source gave us (price, seller, title, region)
    ParsedSpecs    - what we managed to extract from the title/description
    LandedCost     - the normalised price, with every input preserved
    ScoreResult    - the 0-100 score plus a component-by-component breakdown

They are deliberately kept separate. A source module only has to produce a
`Listing`; parsing, normalisation and scoring are all downstream and testable
without touching the network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Region(str, Enum):
    """The seven regions where a forwarding contact can receive a shipment.

    A listing is viable if it ships *domestically* within one of these. We
    never need the retailer to ship internationally.
    """

    US = "US"
    CA = "CA"
    GB = "GB"
    DE = "DE"
    BE = "BE"
    SE = "SE"
    AU = "AU"


class KeyboardLayout(str, Enum):
    """Physical keyboard layout.

    This is a hard filter, not a preference. QWERTZ/AZERTY/Nordic are rejected
    outright; ISO_UK is accepted with a penalty; CANADIAN_MULTILINGUAL is the
    trap case (looks like ANSI in a spec sheet, isn't).
    """

    ANSI = "ANSI"                                  # US layout, what we want
    ISO_UK = "ISO_UK"                              # acceptable, -4 points
    QWERTZ = "QWERTZ"                              # DE - reject
    AZERTY = "AZERTY"                              # BE - reject
    NORDIC = "NORDIC"                              # SE - reject
    CANADIAN_MULTILINGUAL = "CANADIAN_MULTILINGUAL"  # not ANSI - reject
    UNVERIFIED = "UNVERIFIED"                      # unclear, needs eyes on it


class PanelType(str, Enum):
    OLED = "OLED"
    IPS = "IPS"
    VA = "VA"
    UNVERIFIED = "UNVERIFIED"   # scored as IPS, never inferred from the model


class Condition(str, Enum):
    """Condition tiers that map onto the seller-trust scoring rubric."""

    NEW = "NEW"
    MFR_CERTIFIED_REFURB = "MFR_CERTIFIED_REFURB"   # Acer/Lenovo eBay stores
    OPEN_BOX_EXCELLENT = "OPEN_BOX_EXCELLENT"
    OPEN_BOX_GOOD = "OPEN_BOX_GOOD"
    OPEN_BOX_FAIR = "OPEN_BOX_FAIR"
    EBAY_REFURBISHED = "EBAY_REFURBISHED"
    USED = "USED"
    UNKNOWN = "UNKNOWN"


class Flag(str, Enum):
    """Non-fatal warnings attached to a listing.

    Flags never reject a listing on their own - they change how it is scored
    and they all get surfaced in the alert so you can eyeball the risky ones.
    """

    UNVERIFIED_VRAM = "UNVERIFIED_VRAM"                 # e.g. bare "RTX 5070"
    UNVERIFIED_PANEL = "UNVERIFIED_PANEL"               # panel type not stated
    UNVERIFIED_KEYBOARD = "UNVERIFIED_KEYBOARD"         # esp. Canadian stock
    UNVERIFIED_TGP = "UNVERIFIED_TGP"                   # wattage not stated
    UNVERIFIED_BANDWIDTH = "UNVERIFIED_BANDWIDTH"       # unknown GPU model
    UNVERIFIED_TAX_JURISDICTION = "UNVERIFIED_TAX_JURISDICTION"
    UNVERIFIED_SOURCE = "UNVERIFIED_SOURCE"             # only one site reports it
    SINGLE_CHANNEL_RAM = "SINGLE_CHANNEL_RAM"
    DOCK_STORAGE_TRAP = "DOCK_STORAGE_TRAP"
    # Multi-variation listing: the advertised price is the cheapest variant,
    # which is usually a lower-spec config than the one in the title.
    MULTI_VARIATION_LISTING = "MULTI_VARIATION_LISTING"             # "2TB (1TB SSD + dock)"
    HIGH_RISK = "HIGH_RISK"
    SUSPICIOUSLY_CHEAP = "SUSPICIOUSLY_CHEAP"
    BELOW_KNOWN_FLOOR = "BELOW_KNOWN_FLOOR"             # new record low
    NO_KNOWN_FLOOR = "NO_KNOWN_FLOOR"
    PRIORITY_TARGET = "PRIORITY_TARGET"                 # Helios Neo 16S AI
    RTX_5070_12GB = "RTX_5070_12GB"                     # untracked new variant
    ISO_KEYBOARD_PENALTY = "ISO_KEYBOARD_PENALTY"
    FX_STALE = "FX_STALE"                               # fallback rates used
    DESKTOP_GPU_SUSPECTED = "DESKTOP_GPU_SUSPECTED"     # desktop specs quoted


class RejectReason(str, Enum):
    """Why a listing was thrown away. Logged, never notified."""

    RAM_TOO_LOW = "RAM_TOO_LOW"
    VRAM_TOO_LOW = "VRAM_TOO_LOW"
    STORAGE_TOO_LOW = "STORAGE_TOO_LOW"
    RESOLUTION_TOO_LOW = "RESOLUTION_TOO_LOW"
    OVER_BUDGET = "OVER_BUDGET"
    GPU_TOO_OLD = "GPU_TOO_OLD"
    NO_DOMESTIC_SHIPPING = "NO_DOMESTIC_SHIPPING"
    KEYBOARD_LAYOUT = "KEYBOARD_LAYOUT"
    REGION_DISABLED = "REGION_DISABLED"
    UNPARSEABLE = "UNPARSEABLE"
    ACCESSORY_OR_PART = "ACCESSORY_OR_PART"


# ---------------------------------------------------------------------------
# Raw listing
# ---------------------------------------------------------------------------


@dataclass
class Listing:
    """One item as a source reported it, before any of our own reasoning.

    Source modules fill this in and hand it off. Only `source`, `listing_id`,
    `title`, `url`, `region`, `currency` and `sticker_price_local` are
    mandatory; everything else improves scoring accuracy when available.
    """

    source: str                       # e.g. "ebay", "bestbuy", "reddit"
    listing_id: str                   # stable within the source
    title: str
    url: str
    region: Region
    currency: str                     # ISO 4217, must match the region config
    sticker_price_local: float        # advertised price, in local currency

    # -- optional, all improve accuracy -------------------------------------
    description: str = ""
    domestic_shipping_local: float = 0.0
    ships_domestically: bool = True   # false -> hard reject
    condition: Condition = Condition.UNKNOWN
    condition_raw: str = ""           # source's own wording, kept for auditing

    seller_name: str = ""
    seller_feedback_count: int | None = None
    seller_feedback_percent: float | None = None
    is_major_retailer: bool = False

    # Sub-national jurisdiction, when the source reveals it. Drives checkout
    # tax: "AB" (Alberta, 5%) vs "ON" (Ontario, 13%) is an 8-point swing.
    jurisdiction: str | None = None

    # Set only when the listing *explicitly* states a layout. Leaving this
    # None means "fall back to the region default", which is what makes the
    # German/Belgian/Swedish rejections work.
    stated_keyboard_layout: KeyboardLayout | None = None

    # Manufacturer international warranty note. Surfaced, never scored.
    warranty_note: str = ""

    # Flags the source itself established, which the text parsers could never
    # infer — e.g. eBay reporting that a listing has multiple variations, so
    # the advertised price belongs to the cheapest one rather than the config
    # in the title. Merged into the parsed spec flags by `evaluate()`.
    source_flags: list["Flag"] = field(default_factory=list)

    seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = field(default_factory=dict)   # untouched source payload

    # -- identity ------------------------------------------------------------

    def fingerprint(self) -> str:
        """Stable identity used for dedup and price history.

        Prefer `source:listing_id`. When a source has no usable ID (RSS feeds,
        forum posts), fall back to a hash of the normalised title + seller +
        price so the same post doesn't re-notify on every run.
        """
        if self.listing_id:
            return f"{self.source}:{self.listing_id}"

        basis = "|".join(
            [
                self.source,
                " ".join(self.title.lower().split()),
                self.seller_name.lower(),
                f"{self.sticker_price_local:.2f}",
            ]
        )
        return f"{self.source}:h:{hashlib.sha256(basis.encode()).hexdigest()[:16]}"

    @property
    def searchable_text(self) -> str:
        """Title + description, lowercased. What the parsers actually read."""
        return f"{self.title} {self.description}".lower()


# ---------------------------------------------------------------------------
# Parsed specifications
# ---------------------------------------------------------------------------


@dataclass
class ParsedSpecs:
    """What we could extract from a listing's text.

    Every field is optional. `None` means "we could not determine this", which
    is materially different from a low value - unknowns get flagged and scored
    conservatively rather than assumed away.
    """

    gpu_model: str | None = None          # canonical, e.g. "RTX 5070 Ti"
    gpu_generation: int | None = None     # 30, 40, 50
    vram_gb: int | None = None
    vram_verified: bool = True            # False for a bare "RTX 5070"
    memory_bandwidth_gbs: int | None = None
    tgp_watts: int | None = None

    system_ram_gb: int | None = None
    single_channel: bool = False

    storage_gb: int | None = None         # INTERNAL M.2 only
    dock_storage_gb: int | None = None    # accessory storage we discarded
    free_m2_slot: bool = False

    panel_type: PanelType = PanelType.UNVERIFIED
    resolution_w: int | None = None
    resolution_h: int | None = None
    refresh_hz: int | None = None
    nits: int | None = None

    screen_inches: float | None = None
    cpu: str | None = None
    model_key: str | None = None          # matched entry in config.known_models
    model_display: str | None = None

    flags: list[Flag] = field(default_factory=list)

    def add_flag(self, flag: Flag) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


# ---------------------------------------------------------------------------
# Landed cost
# ---------------------------------------------------------------------------


@dataclass
class LandedCost:
    """The normalised price, with every input kept for auditing.

        landed_usd = (sticker_local
                      - reclaimable_tax          (always 0 - see regions.py)
                      + domestic_shipping
                      + destination_tax_at_checkout)
                     * fx_rate_to_usd
                     * (1 + regional_risk_premium)

    We store the FX rate and the moment it was fetched on every listing so that
    a comparison made six weeks from now against today's record is still honest.
    """

    sticker_local: float
    currency: str
    reclaimable_tax_local: float
    domestic_shipping_local: float
    destination_tax_local: float
    total_local: float                    # the sum before FX and risk premium

    fx_rate_to_usd: float
    fx_source: str
    fx_fetched_at: datetime

    risk_premium: float
    landed_usd: float

    # Provenance for the tax number, so an alert can say "assumed Ontario 13%".
    tax_jurisdiction: str | None = None
    tax_rate_applied: float = 0.0
    tax_rate_assumed: bool = False
    vat_embedded_rate: float = 0.0        # informational: VAT already in sticker

    def local_display(self) -> str:
        """e.g. '£1,150.00' — always shown next to the landed USD figure."""
        symbols = {"USD": "$", "CAD": "C$", "GBP": "£",
                   "EUR": "€", "SEK": "kr", "AUD": "A$"}
        symbol = symbols.get(self.currency, self.currency + " ")
        return f"{symbol}{self.sticker_local:,.2f}"

    def usd_display(self) -> str:
        return f"${self.landed_usd:,.2f}"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class ScoreComponent:
    """One line of the score breakdown, e.g. 'VRAM 26/30 - 12 GB'."""

    name: str
    points: float
    max_points: float
    detail: str

    def __str__(self) -> str:
        return f"{self.name} {self.points:+g}/{self.max_points:g} ({self.detail})"


@dataclass
class ScoreResult:
    total: float
    components: list[ScoreComponent] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    priority: bool = False           # force an immediate alert regardless of score
    priority_reason: str = ""

    def add_flag(self, flag: Flag) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def breakdown_line(self, top_n: int = 3) -> str:
        """One-line summary of what actually drove the score.

        Sorted by absolute contribution so the biggest movers show up first -
        a -15 junk-title penalty is more informative than a +5 storage tier.
        """
        ranked = sorted(self.components, key=lambda c: -abs(c.points))
        return " | ".join(str(c) for c in ranked[:top_n])


@dataclass
class EvaluatedListing:
    """A listing that has been through the whole pipeline.

    `rejected` listings keep their reasons and are written to the database for
    debugging, but are never notified.
    """

    listing: Listing
    specs: ParsedSpecs
    landed: LandedCost | None = None
    score: ScoreResult | None = None
    reject_reasons: list[RejectReason] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.reject_reasons)

    @property
    def fingerprint(self) -> str:
        return self.listing.fingerprint()

    @property
    def all_flags(self) -> list[Flag]:
        """Spec-parsing flags and scoring flags, merged and de-duplicated."""
        merged: list[Flag] = []
        for flag in list(self.specs.flags) + list(self.score.flags if self.score else []):
            if flag not in merged:
                merged.append(flag)
        return merged
