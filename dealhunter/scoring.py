"""
The scoring engine: 0-100 for every listing that survives the hard filters.

Seven base components sum to exactly 100 (enforced at config load):

    VRAM tier            30   what model size fits on the card at all
    Memory bandwidth     10   how fast tokens come out of it
    Panel                15
    System RAM           15
    Storage               8
    GPU power (TGP)      10   wattage beats the model name
    Condition / trust    12

On top of that sit modifiers that can push outside 0-100 before clamping:

    Price vs. known floor  +/-10   the deal-quality signal
    Free M.2 slot            +2
    Single-channel RAM       -3
    Junk title              -15
    ISO / bilingual keyboard -4

Everything is driven from `config.yaml`. Nothing in this file has a magic
number in it — if you want to care more about VRAM and less about the panel,
edit the config, and the load-time check will tell you if the weights no
longer add up.
"""

from __future__ import annotations

import re

from .config import Config, KnownModel
from .models import (
    Condition,
    Flag,
    KeyboardLayout,
    LandedCost,
    Listing,
    PanelType,
    ParsedSpecs,
    ScoreComponent,
    ScoreResult,
)


def score_listing(
    listing: Listing,
    specs: ParsedSpecs,
    landed: LandedCost,
    config: Config,
    keyboard_layout: KeyboardLayout,
    floor_override_usd: float | None = None,
) -> ScoreResult:
    """Score one listing that has already passed the hard filters.

    `floor_override_usd` lets the database supply a floor that has been driven
    lower than the config seed by a verified sighting. When omitted we fall
    back to the seeded figure in `known_models`.
    """
    rules = config.scoring
    result = ScoreResult(total=0.0)

    # Carry parsing flags through, so the alert shows UNVERIFIED_VRAM etc.
    for flag in specs.flags:
        result.add_flag(flag)

    result.components.append(_score_vram(specs, rules))
    result.components.append(_score_bandwidth(specs, rules, result))
    result.components.append(_score_panel(specs, rules))
    result.components.append(_score_system_ram(specs, rules))
    result.components.append(_score_storage(specs, rules))
    result.components.append(_score_tgp(specs, rules, result))
    result.components.append(_score_condition(listing, specs, landed, config, result))
    result.components.append(
        _score_price_vs_floor(specs, landed, config, result, floor_override_usd)
    )
    result.components.append(_score_keyboard(keyboard_layout, rules, result))

    raw_total = sum(component.points for component in result.components)
    result.total = round(
        max(rules["clamp_min"], min(rules["clamp_max"], raw_total)), 1
    )

    _apply_priority_rules(specs, landed, config, result, floor_override_usd)
    return result


# ---------------------------------------------------------------------------
# Individual components
# ---------------------------------------------------------------------------


def _tiered(value: float | None, tiers: dict, default: float = 0.0) -> float:
    """Score against descending "at least this much" thresholds.

    `tiers` is ``{threshold: points}`` from config; we take the points for the
    highest threshold the value meets or exceeds.
    """
    if value is None:
        return default
    for threshold in sorted((int(k) for k in tiers), reverse=True):
        if value >= threshold:
            return float(tiers[threshold])
    return default


def _score_vram(specs: ParsedSpecs, rules: dict) -> ScoreComponent:
    """VRAM tier, 30 pts — the single heaviest component.

    This is the binding constraint on local inference: 8 GB caps you at small
    quantised models, 12 GB opens up 14B-class at 4-bit, 16 GB is comfortable.
    """
    cfg = rules["vram"]
    points = _tiered(specs.vram_gb, cfg["tiers"])

    if specs.vram_gb is None:
        detail = "unknown"
    elif specs.vram_verified:
        detail = f"{specs.vram_gb} GB"
    else:
        # A bare "RTX 5070" is scored at the 8 GB tier until someone confirms.
        detail = f"{specs.vram_gb} GB assumed, UNVERIFIED"

    return ScoreComponent("VRAM", points, cfg["max_points"], detail)


def _score_bandwidth(specs: ParsedSpecs, rules: dict, result: ScoreResult) -> ScoreComponent:
    """Memory bandwidth, 10 pts, linear between the two config anchors.

    Token generation on a local LLM is bandwidth-bound, not capacity-bound —
    which is why a 5070 Ti (672 GB/s) generates roughly 1.75x faster than a
    5060 (384 GB/s) on a model that fits on both.
    """
    cfg = rules["bandwidth"]
    bandwidth = specs.memory_bandwidth_gbs

    if bandwidth is None:
        result.add_flag(Flag.UNVERIFIED_BANDWIDTH)
        return ScoreComponent(
            "Bandwidth", float(cfg["unknown_points"]), cfg["max_points"], "unknown"
        )

    floor, ceiling = float(cfg["floor_gbs"]), float(cfg["ceiling_gbs"])
    fraction = (bandwidth - floor) / (ceiling - floor)
    points = max(0.0, min(1.0, fraction)) * cfg["max_points"]

    return ScoreComponent(
        "Bandwidth", round(points, 1), cfg["max_points"], f"{bandwidth} GB/s"
    )


def _score_panel(specs: ParsedSpecs, rules: dict) -> ScoreComponent:
    """Panel, 15 pts. OLED at 2560x1600 / 2560x1440 is the target.

    An UNVERIFIED panel scores as IPS. We never infer OLED from a model family
    — see `KNOWN_NO_OLED_MODELS` in parsing.py for why.
    """
    cfg = rules["panel"]
    resolution = (specs.resolution_w, specs.resolution_h)
    preferred = [tuple(r) for r in cfg["oled_preferred_resolutions"]]

    if specs.panel_type == PanelType.OLED:
        if resolution in preferred:
            return ScoreComponent(
                "Panel", float(cfg["oled_preferred_points"]), cfg["max_points"],
                f"OLED {specs.resolution_w}x{specs.resolution_h}",
            )
        return ScoreComponent(
            "Panel", float(cfg["oled_other_points"]), cfg["max_points"],
            f"OLED {specs.resolution_w}x{specs.resolution_h}",
        )

    label = "IPS/VA" if specs.panel_type != PanelType.UNVERIFIED else "panel UNVERIFIED"

    if specs.nits is not None and specs.nits >= cfg["ips_bright_nits"]:
        return ScoreComponent(
            "Panel", float(cfg["ips_bright_points"]), cfg["max_points"],
            f"{label} {specs.nits} nit",
        )

    brightness = f"{specs.nits} nit" if specs.nits else "brightness unknown"
    return ScoreComponent(
        "Panel", float(cfg["ips_dim_or_unknown_points"]), cfg["max_points"],
        f"{label}, {brightness}",
    )


def _score_system_ram(specs: ParsedSpecs, rules: dict) -> ScoreComponent:
    """System RAM, 15 pts, with a single-channel penalty.

    One SODIMM halves memory bandwidth on these machines. It is cheap to fix,
    but you should know before you buy, so it costs 3 points and gets flagged.
    """
    cfg = rules["system_ram"]
    points = _tiered(specs.system_ram_gb, cfg["tiers"])
    detail = f"{specs.system_ram_gb} GB" if specs.system_ram_gb else "unknown"

    if specs.single_channel:
        points += cfg["single_channel_penalty"]
        detail += ", SINGLE-CHANNEL"

    return ScoreComponent("RAM", points, cfg["max_points"], detail)


def _score_storage(specs: ParsedSpecs, rules: dict) -> ScoreComponent:
    """Storage, 8 pts + 2 bonus for a confirmed free M.2 slot.

    `specs.storage_gb` is internal M.2 only — dock and external capacity has
    already been stripped out by the parser.
    """
    cfg = rules["storage"]
    points = _tiered(specs.storage_gb, cfg["tiers"])
    detail = f"{specs.storage_gb} GB" if specs.storage_gb else "unknown"

    if specs.free_m2_slot:
        points += cfg["free_m2_slot_bonus"]
        detail += " + free M.2"

    if specs.dock_storage_gb:
        detail += f" ({specs.dock_storage_gb} GB dock storage ignored)"

    return ScoreComponent("Storage", points, cfg["max_points"], detail)


def _score_tgp(specs: ParsedSpecs, rules: dict, result: ScoreResult) -> ScoreComponent:
    """GPU total graphics power, 10 pts.

    A 115 W xx60 sustains higher clocks than an 85 W xx70, so this is scored
    independently of the model name. Unknown wattage takes the middle value and
    a flag rather than an assumption, because it swings 8 points.
    """
    cfg = rules["tgp"]

    if specs.tgp_watts is None:
        result.add_flag(Flag.UNVERIFIED_TGP)
        return ScoreComponent(
            "TGP", float(cfg["unknown_points"]), cfg["max_points"], "unknown"
        )

    points = _tiered(specs.tgp_watts, cfg["tiers"])
    return ScoreComponent("TGP", points, cfg["max_points"], f"{specs.tgp_watts} W")


def _score_condition(
    listing: Listing,
    specs: ParsedSpecs,
    landed: LandedCost,
    config: Config,
    result: ScoreResult,
) -> ScoreComponent:
    """Condition and seller trust, 12 pts, plus the junk-title penalty.

    Two adjustments sit on top of the base tier:

      * A junk title ("READ", "AS IS", "cracked", "no battery") costs 15 points
        and marks the listing HIGH RISK. That is enough to push almost anything
        below the notification threshold, which is the intent.
      * Below the suspiciously-cheap threshold, trust is weighted harder: the
        listing is penalised by a fraction of the trust points it failed to
        earn. A cheap machine from Best Buy is unaffected; a cheap machine from
        a 12-feedback seller goes sharply negative.
    """
    cfg = config.scoring["condition"]
    max_points = float(cfg["max_points"])
    points, detail = _base_condition_points(listing, config, cfg, result)

    # -- junk title ---------------------------------------------------------
    junk = _junk_markers_in(listing.title, cfg["junk_title_markers"])
    if junk:
        points += cfg["junk_title_penalty"]
        result.add_flag(Flag.HIGH_RISK)
        detail += f", JUNK TITLE ({'/'.join(junk)})"

    # -- suspiciously cheap -------------------------------------------------
    threshold = config.budget["suspicious_below_usd"]
    if landed.landed_usd < threshold:
        result.add_flag(Flag.SUSPICIOUSLY_CHEAP)
        multiplier = float(config.budget["suspicious_trust_penalty_multiplier"])
        shortfall = max(0.0, max_points - points)
        penalty = shortfall * multiplier
        if penalty:
            points -= penalty
            detail += f", cheap-listing trust penalty -{penalty:.1f}"

    return ScoreComponent("Condition/Trust", round(points, 1), max_points, detail)


def _base_condition_points(
    listing: Listing, config: Config, cfg: dict, result: ScoreResult
) -> tuple[float, str]:
    """The condition tier before junk-title and cheapness adjustments."""
    feedback = listing.seller_feedback_count
    percent = listing.seller_feedback_percent
    is_major = listing.is_major_retailer or _is_major_retailer(listing, config)

    if listing.condition == Condition.NEW:
        if is_major:
            return float(cfg["new_major_retailer"]), f"new @ {listing.seller_name or 'major retailer'}"
        return float(cfg["new_other"]), "new, non-major seller"

    if listing.condition == Condition.MFR_CERTIFIED_REFURB:
        return float(cfg["mfr_certified_refurb"]), "manufacturer-certified refurb"

    open_box = {
        Condition.OPEN_BOX_EXCELLENT: ("open_box_excellent", "open box Excellent"),
        Condition.OPEN_BOX_GOOD: ("open_box_good", "open box Good"),
        Condition.OPEN_BOX_FAIR: ("open_box_fair", "open box Fair"),
    }
    if listing.condition in open_box:
        key, label = open_box[listing.condition]
        return float(cfg[key]), label

    if listing.condition == Condition.EBAY_REFURBISHED:
        if (
            feedback is not None
            and percent is not None
            and feedback > cfg["ebay_refurb_trusted_min_feedback"]
            and percent > cfg["ebay_refurb_trusted_min_percent"]
        ):
            return (
                float(cfg["ebay_refurb_trusted"]),
                f"eBay Refurbished, {feedback:,} fb / {percent:g}%",
            )
        # A refurb from a seller without that track record is scored as used.
        return _used_points(feedback, percent, cfg, result, prefix="eBay Refurbished")

    if listing.condition == Condition.USED:
        return _used_points(feedback, percent, cfg, result, prefix="used")

    return float(cfg["used_default"]), "condition unknown"


def _used_points(
    feedback: int | None,
    percent: float | None,
    cfg: dict,
    result: ScoreResult,
    prefix: str,
) -> tuple[float, str]:
    """Trust tiers for used stock, driven entirely by seller history."""
    if feedback is not None and feedback < cfg["used_untrusted_max_feedback"]:
        result.add_flag(Flag.HIGH_RISK)
        return float(cfg["used_untrusted"]), f"{prefix}, only {feedback} feedback - HIGH RISK"

    if (
        feedback is not None
        and percent is not None
        and feedback > cfg["used_trusted_min_feedback"]
        and percent > cfg["used_trusted_min_percent"]
    ):
        return float(cfg["used_trusted"]), f"{prefix}, {feedback:,} fb / {percent:g}%"

    if feedback is None:
        return float(cfg["used_default"]), f"{prefix}, seller history unknown"

    return float(cfg["used_default"]), f"{prefix}, {feedback:,} fb"


def _is_major_retailer(listing: Listing, config: Config) -> bool:
    """Match the seller/source against the configured major-retailer list."""
    haystack = f"{listing.seller_name} {listing.source}".lower()
    return any(name in haystack for name in config.scoring["major_retailers"])


def _junk_markers_in(title: str, markers: list[str]) -> list[str]:
    """Junk markers present in a title, matched on word boundaries.

    Word boundaries matter: a naive substring test for "read" fires on
    "already", "thread" and "spreadsheet". The multi-word markers ("as is",
    "for parts") are matched with flexible whitespace.
    """
    found = []
    for marker in markers:
        pattern = r"\b" + r"\s*[- ]\s*".join(re.escape(w) for w in marker.split()) + r"\b"
        if re.search(pattern, title, re.IGNORECASE):
            found.append(marker)
    return found


def _score_price_vs_floor(
    specs: ParsedSpecs,
    landed: LandedCost,
    config: Config,
    result: ScoreResult,
    floor_override_usd: float | None,
) -> ScoreComponent:
    """Price against the known historical floor, +10 to -10.

    +10 at or below the floor, sliding linearly to -10 at `penalty_at_ratio`
    (25%) above it. A model we have no floor for scores 0 and gets flagged —
    absence of history is not evidence of a good price.
    """
    cfg = config.scoring["price_vs_floor"]
    max_bonus = float(cfg["max_bonus"])
    max_penalty = float(cfg["max_penalty"])

    floor = floor_override_usd
    if floor is None and specs.model_key:
        model = config.model_by_key(specs.model_key)
        floor = model.floor_usd if model else None

    if not floor:
        result.add_flag(Flag.NO_KNOWN_FLOOR)
        return ScoreComponent(
            "Price vs floor", float(cfg["unknown_floor_points"]), max_bonus,
            "no floor on record",
        )

    ratio = landed.landed_usd / floor
    if ratio <= 1.0:
        points = max_bonus
        if landed.landed_usd < floor:
            result.add_flag(Flag.BELOW_KNOWN_FLOOR)
    else:
        # Straight line from (1.00, +10) to (penalty_at_ratio, -10).
        span = float(cfg["penalty_at_ratio"]) - 1.0
        slope = (max_bonus - max_penalty) / span
        points = max(max_penalty, max_bonus - (ratio - 1.0) * slope)

    delta = landed.landed_usd - floor
    detail = f"${landed.landed_usd:,.0f} vs ${floor:,.0f} floor ({delta:+,.0f})"
    return ScoreComponent("Price vs floor", round(points, 1), max_bonus, detail)


def _score_keyboard(
    layout: KeyboardLayout, rules: dict, result: ScoreResult
) -> ScoreComponent:
    """Keyboard layout modifier.

    Rejected layouts never reach this function — they were filtered out. What
    remains is ANSI (free), UK ISO (-4), a confirmed bilingual Canadian board
    (-4), or UNVERIFIED (no penalty, but always flagged in the alert so you can
    check the photos before committing).
    """
    cfg = rules["keyboard"]

    if layout == KeyboardLayout.ISO_UK:
        result.add_flag(Flag.ISO_KEYBOARD_PENALTY)
        return ScoreComponent("Keyboard", float(cfg["iso_uk_penalty"]), 0, "UK ISO")

    if layout == KeyboardLayout.CANADIAN_MULTILINGUAL:
        result.add_flag(Flag.ISO_KEYBOARD_PENALTY)
        return ScoreComponent(
            "Keyboard", float(cfg["canadian_multilingual_penalty"]), 0,
            "Canadian Multilingual - not ANSI",
        )

    if layout == KeyboardLayout.UNVERIFIED:
        result.add_flag(Flag.UNVERIFIED_KEYBOARD)
        return ScoreComponent(
            "Keyboard", float(cfg["unverified_penalty"]), 0, "layout UNVERIFIED",
        )

    return ScoreComponent("Keyboard", 0.0, 0, "ANSI")


# ---------------------------------------------------------------------------
# Priority overrides
# ---------------------------------------------------------------------------


def _apply_priority_rules(
    specs: ParsedSpecs,
    landed: LandedCost,
    config: Config,
    result: ScoreResult,
    floor_override_usd: float | None,
) -> None:
    """Force an immediate alert for finds that matter regardless of score.

    Two standing rules:

      1. The Acer Predator Helios Neo 16S AI at or under its configured trigger
         price. It is the only machine that satisfies every preferred spec, so
         a sighting inside budget is worth waking up for even if some other
         component drags the computed score down.
      2. Any confirmed RTX 5070 **12 GB**. That variant is new enough that
         open-box pricing has no history, so every sighting is information.
    """
    if specs.model_key:
        model: KnownModel | None = config.model_by_key(specs.model_key)
        if model and model.priority_alert_at_or_below_usd is not None:
            if landed.landed_usd <= model.priority_alert_at_or_below_usd:
                result.priority = True
                result.priority_reason = (
                    f"{model.display} at ${landed.landed_usd:,.0f} landed, at or under "
                    f"the ${model.priority_alert_at_or_below_usd:,.0f} standing trigger"
                )
                result.add_flag(Flag.PRIORITY_TARGET)

    if config.priority_rules.get("rtx_5070_12gb_is_priority") and Flag.RTX_5070_12GB in result.flags:
        result.priority = True
        if not result.priority_reason:
            result.priority_reason = (
                "Confirmed RTX 5070 12 GB - new variant with no open-box price history"
            )


def in_target_zone(landed: LandedCost, config: Config) -> bool:
    """True when the landed price sits inside the $1,000-1,200 sweet spot.

    Not a scoring component (the seven base weights already total 100), but the
    digest uses it to label picks and to break ties between equal scores.
    """
    return (
        config.budget["target_low_usd"]
        <= landed.landed_usd
        <= config.budget["target_high_usd"]
    )
