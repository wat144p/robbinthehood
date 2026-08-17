"""
Turning an evaluated listing into the content of an alert.

Every channel — Discord, ntfy, stdout — renders from the same `AlertContent`
object, so they can never disagree about the facts. Formatting differs;
substance does not.

The brief specifies exactly what an alert has to carry, and `AlertContent` has
a field for each one:

    model, region + flag, local sticker AND landed USD side by side,
    delta vs. known floor, full parsed spec line, keyboard layout,
    condition and seller trust, score with a one-line breakdown,
    UNVERIFIED / HIGH RISK flags, warranty note, direct URL

The ranking rule is enforced structurally: `price_line` always contains both
numbers. There is no code path that renders a converted figure on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..models import EvaluatedListing, Flag, KeyboardLayout, Region
from ..regions import explain_landed_cost, resolve_keyboard_layout
from ..scoring import in_target_zone

# Flags that deserve a prominent warning line rather than a quiet mention.
LOUD_FLAGS = {
    Flag.HIGH_RISK: "HIGH RISK — read the listing carefully before bidding",
    Flag.UNVERIFIED_VRAM: "VRAM UNVERIFIED — scored at the lower tier, confirm capacity",
    Flag.MULTI_VARIATION_LISTING: (
        "MULTI-VARIATION LISTING — the price shown is the cheapest variant, "
        "which may not be the config in the title"
    ),
    Flag.DOCK_STORAGE_TRAP: "DOCK STORAGE — accessory capacity excluded from the spec",
    Flag.UNVERIFIED_KEYBOARD: "KEYBOARD UNVERIFIED — check the photos for the layout",
    Flag.SINGLE_CHANNEL_RAM: "SINGLE-CHANNEL RAM — one SODIMM, halves memory bandwidth",
    Flag.DESKTOP_GPU_SUSPECTED: "DESKTOP SPECS QUOTED — the seller may not know the part",
    Flag.SUSPICIOUSLY_CHEAP: "SUSPICIOUSLY CHEAP — seller trust weighted harder",
    Flag.FX_STALE: "FX RATES STALE — the landed figure is approximate",
}


@dataclass
class AlertContent:
    """Everything an alert needs, channel-agnostic."""

    title: str
    url: str
    region_display: str
    region_flag: str

    price_line: str          # local sticker AND landed USD, always together
    landed_usd: float
    landed_breakdown: str    # the full derivation, for the curious

    floor_line: str          # delta vs. the known floor
    spec_line: str
    keyboard_line: str
    condition_line: str
    score: float
    score_line: str          # "72.5/100 — VRAM +26/30 | Panel +15/15 | ..."
    warranty_line: str

    warnings: list[str] = field(default_factory=list)
    headline_tag: str = ""   # "NEW RECORD LOW", "PRIORITY TARGET", ...
    is_priority: bool = False
    regional_advantage: str = ""   # why a non-US pick won, when it did
    in_target_zone: bool = False

    def plain_text(self) -> str:
        """The stdout / ntfy rendering."""
        lines = []
        if self.headline_tag:
            lines.append(self.headline_tag)
        lines.append(f"{self.region_flag} [{self.region_display}] {self.title}")
        lines.append(f"  {self.price_line}")
        if self.regional_advantage:
            lines.append(f"  {self.regional_advantage}")
        lines.append(f"  {self.floor_line}")
        lines.append(f"  {self.spec_line}")
        lines.append(f"  keyboard: {self.keyboard_line}")
        lines.append(f"  condition: {self.condition_line}")
        lines.append(f"  score {self.score:g}/100 — {self.score_line}")
        for warning in self.warnings:
            lines.append(f"  ⚠ {warning}")
        if self.warranty_line:
            lines.append(f"  warranty: {self.warranty_line}")
        lines.append(f"  {self.url}")
        return "\n".join(lines)


def build_alert(
    evaluated: EvaluatedListing,
    config: Config,
    peers: list[EvaluatedListing] | None = None,
) -> AlertContent:
    """Assemble the alert content for one scored listing.

    `peers` is the rest of this run's results. It is used only to explain why a
    non-US pick beat the US field — the brief asks for that sentence explicitly,
    and it can't be produced from one listing in isolation.
    """
    listing = evaluated.listing
    specs = evaluated.specs
    landed = evaluated.landed
    score = evaluated.score
    region_cfg = config.region(listing.region)
    flags = evaluated.all_flags

    layout, layout_explicit = resolve_keyboard_layout(listing)

    return AlertContent(
        title=listing.title,
        url=listing.url,
        region_display=region_cfg.display,
        region_flag=region_cfg.flag,
        price_line=(
            f"{landed.local_display()} sticker  →  {landed.usd_display()} landed"
        ),
        landed_usd=landed.landed_usd,
        landed_breakdown=explain_landed_cost(landed, region_cfg),
        floor_line=_floor_line(evaluated, config),
        spec_line=spec_line(evaluated),
        keyboard_line=_keyboard_line(layout, layout_explicit, listing.region),
        condition_line=_condition_line(evaluated),
        score=score.total,
        score_line=score.breakdown_line(),
        warranty_line=_warranty_line(evaluated),
        warnings=[LOUD_FLAGS[f] for f in flags if f in LOUD_FLAGS],
        headline_tag=_headline_tag(evaluated),
        is_priority=score.priority,
        regional_advantage=explain_regional_advantage(evaluated, peers or [], config),
        in_target_zone=in_target_zone(landed, config),
    )


# ---------------------------------------------------------------------------
# Line builders
# ---------------------------------------------------------------------------


def _headline_tag(evaluated: EvaluatedListing) -> str:
    """The loud banner. Beating the known floor is the loudest thing we say."""
    tags = []
    if Flag.BELOW_KNOWN_FLOOR in evaluated.all_flags:
        tags.append("🏆 NEW RECORD LOW — below the known floor for this model")
    if evaluated.score and evaluated.score.priority:
        reason = evaluated.score.priority_reason or "standing priority rule"
        tags.append(f"🚨 PRIORITY — {reason}")
    if Flag.RTX_5070_12GB in evaluated.all_flags:
        tags.append("⭐ RTX 5070 12 GB — the untracked new variant")
    return "\n".join(tags)


def _floor_line(evaluated: EvaluatedListing, config: Config) -> str:
    """Delta against the known historical low for this model."""
    specs = evaluated.specs
    landed = evaluated.landed

    if not specs.model_key:
        return "no price floor on record for this model — treat with caution"

    model = config.model_by_key(specs.model_key)
    if not model:
        return "no price floor on record for this model"

    delta = landed.landed_usd - model.floor_usd
    if delta < 0:
        verdict = f"${abs(delta):,.0f} BELOW the known floor"
    elif delta == 0:
        verdict = "exactly at the known floor"
    else:
        verdict = f"${delta:,.0f} above the known floor"

    return f"{model.display}: {verdict} (${model.floor_usd:,.0f})"


def spec_line(evaluated: EvaluatedListing) -> str:
    """The full parsed spec, with unknowns shown as unknown rather than hidden."""
    specs = evaluated.specs

    def unknown(value, suffix: str = "") -> str:
        return f"{value}{suffix}" if value is not None else "?"

    vram = unknown(specs.vram_gb, " GB")
    if specs.vram_gb is not None and not specs.vram_verified:
        vram += " (UNVERIFIED)"

    resolution = (
        f"{specs.resolution_w}x{specs.resolution_h}"
        if specs.resolution_h else "?"
    )
    panel = specs.panel_type.value
    ram = unknown(specs.system_ram_gb, " GB")
    if specs.single_channel:
        ram += " single-channel"

    storage = unknown(specs.storage_gb, " GB")
    if specs.free_m2_slot:
        storage += " + free M.2"

    parts = [
        f"{specs.gpu_model or '?'} {vram}",
        f"{unknown(specs.tgp_watts, ' W')} TGP",
        f"{ram} RAM",
        storage,
        f"{resolution} {panel}",
        unknown(specs.refresh_hz, " Hz"),
    ]
    if specs.nits:
        parts.append(f"{specs.nits} nit")
    if specs.cpu:
        parts.append(specs.cpu)

    return " / ".join(parts)


def _keyboard_line(
    layout: KeyboardLayout, explicit: bool, region: Region
) -> str:
    """Layout, and how confident we are about it."""
    descriptions = {
        KeyboardLayout.ANSI: "US ANSI — what you want",
        KeyboardLayout.ISO_UK: "UK ISO — usable, -4 points (Enter shape, @ and \" swapped)",
        KeyboardLayout.CANADIAN_MULTILINGUAL: (
            "Canadian Multilingual — QWERTY but NOT ANSI, -4 points"
        ),
        KeyboardLayout.UNVERIFIED: (
            "UNVERIFIED — check the photos. Canadian stock is frequently bilingual"
        ),
    }
    text = descriptions.get(layout, layout.value)
    if not explicit and layout != KeyboardLayout.UNVERIFIED:
        text += f" (assumed from {region.value} stock, not stated in the listing)"
    return text


def _warranty_line(evaluated: EvaluatedListing) -> str:
    """Warranty context. Never scored — surfaced so you know what you're giving up.

    A source that says nothing gets an explicit "not stated" rather than a
    blank, because an empty line reads as "no warranty concerns" when it
    actually means "we don't know". Lenovo and Acer honour international
    coverage in some regions and not others, and none of it is reliably
    serviceable in Pakistan either way.
    """
    if evaluated.listing.warranty_note:
        return evaluated.listing.warranty_note

    region = evaluated.listing.region
    return (
        f"not stated — assume {region.value}-only coverage, "
        f"not serviceable in Pakistan"
    )


def _condition_line(evaluated: EvaluatedListing) -> str:
    """Condition plus the seller-trust detail that justified its score."""
    listing = evaluated.listing
    trust = next(
        (c for c in evaluated.score.components if c.name == "Condition/Trust"), None
    )
    detail = trust.detail if trust else listing.condition.value

    if listing.seller_name:
        detail += f"  [{listing.seller_name}]"
    return detail


# ---------------------------------------------------------------------------
# Why did a non-US pick win?
# ---------------------------------------------------------------------------


def explain_regional_advantage(
    pick: EvaluatedListing,
    peers: list[EvaluatedListing],
    config: Config,
) -> str:
    """Explain a non-US win in terms of the actual numbers.

    The brief asks for exactly this, e.g. "CAD weakness plus Alberta 5% GST
    puts this $90 under the best US listing." We build it from the real inputs
    rather than a template, so it is always true of this specific listing.
    """
    if pick.listing.region == Region.US or not pick.landed:
        return ""

    us_peers = [
        peer for peer in peers
        if peer.listing.region == Region.US and not peer.rejected and peer.landed
    ]
    if not us_peers:
        return ""

    best_us = min(us_peers, key=lambda peer: peer.landed.landed_usd)
    delta = best_us.landed.landed_usd - pick.landed.landed_usd
    if delta <= 0:
        return ""   # it didn't actually beat the US field; say nothing

    region_cfg = config.region(pick.listing.region)
    landed = pick.landed
    reasons = []

    # Currency: a rate below 1.0 means the local currency buys fewer dollars,
    # which is exactly what makes a scary-looking sticker land cheaply.
    if landed.fx_rate_to_usd < 0.95:
        reasons.append(
            f"{landed.currency} weakness (1 {landed.currency} = "
            f"${landed.fx_rate_to_usd:.3f})"
        )

    # Tax jurisdiction, when it beat the region's own baseline.
    if (
        landed.tax_jurisdiction
        and not landed.tax_rate_assumed
        and landed.tax_rate_applied < region_cfg.checkout_tax_fallback
    ):
        reasons.append(
            f"{landed.tax_jurisdiction} at {landed.tax_rate_applied * 100:.3g}% tax "
            f"vs the {region_cfg.checkout_tax_fallback * 100:.3g}% baseline"
        )

    if region_cfg.risk_premium == 0:
        reasons.append("no forwarding risk premium")

    if not reasons:
        reasons.append("a lower sticker price after normalisation")

    return (
        f"Why this beat the US field: {' plus '.join(reasons)} puts it "
        f"${delta:,.0f} under the best US listing "
        f"({best_us.landed.usd_display()})."
    )


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Trim to a hard character limit. Discord enforces several of these."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix
