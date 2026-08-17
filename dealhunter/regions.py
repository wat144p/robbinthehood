"""
Landed-cost normalisation and keyboard-layout resolution.

**This is the most important logic in the system.** A £1,150 UK listing and a
$1,150 US listing are not the same deal, and a naive FX conversion ranks the UK
one far too highly. Everything downstream compares landed USD, never stickers.

    landed_usd = (sticker_local
                  - reclaimable_tax
                  + domestic_shipping
                  + destination_tax_added_at_checkout)
                 * fx_rate_to_usd
                 * (1 + regional_risk_premium)

Two things about that formula deserve emphasis:

1.  ``reclaimable_tax`` is **always zero**. UK and EU sticker prices are
    VAT-inclusive by law, and because the receiving contact takes delivery as a
    normal domestic consumer, none of that VAT is reclaimable. We deliberately
    do NOT strip it out. A €1,299 German sticker really does cost €1,299, and
    the fact that €207 of it is VAT is our problem, not a discount.

2.  ``destination_tax`` only applies where tax is added *at checkout* - the US
    and Canada. Everywhere else it is zero because the tax is already inside
    the sticker. Applying both would double-count.

The taxable base for checkout tax is sticker + shipping. That is how most US
states and all Canadian provinces actually assess it.
"""

from __future__ import annotations

import re

from .config import Config, RegionConfig
from .fx import FxRates
from .models import Flag, KeyboardLayout, LandedCost, Listing, ParsedSpecs

# ---------------------------------------------------------------------------
# Keyboard layout
# ---------------------------------------------------------------------------

# Phrases that constitute an *explicit* statement of a US or UK layout. Only
# these override a region's default - this is what lets a US-layout SKU sold in
# Germany survive the QWERTZ filter, while ordinary German stock does not.
US_LAYOUT_MARKERS = (
    "us layout", "us keyboard", "u.s. keyboard", "ansi", "qwerty us",
    "us english keyboard", "english (us) keyboard", "us international",
    "american keyboard", "us-layout", "us qwerty",
)

UK_LAYOUT_MARKERS = (
    "uk layout", "uk keyboard", "united kingdom keyboard", "gb keyboard",
    "uk english keyboard", "iso uk", "uk qwerty", "uk-layout",
)

# Phrases that positively identify a layout we reject, wherever they appear.
# A QWERTZ machine listed on eBay US is still a QWERTZ machine.
NEGATIVE_LAYOUT_MARKERS: dict[str, KeyboardLayout] = {
    "qwertz": KeyboardLayout.QWERTZ,
    "german keyboard": KeyboardLayout.QWERTZ,
    "deutsch tastatur": KeyboardLayout.QWERTZ,
    "tastatur": KeyboardLayout.QWERTZ,
    "azerty": KeyboardLayout.AZERTY,
    "belgian keyboard": KeyboardLayout.AZERTY,
    "french keyboard": KeyboardLayout.AZERTY,
    "nordic": KeyboardLayout.NORDIC,
    "swedish keyboard": KeyboardLayout.NORDIC,
    "scandinavian keyboard": KeyboardLayout.NORDIC,
    "canadian multilingual": KeyboardLayout.CANADIAN_MULTILINGUAL,
    "multilingual standard": KeyboardLayout.CANADIAN_MULTILINGUAL,
    "bilingual keyboard": KeyboardLayout.CANADIAN_MULTILINGUAL,
    "clavier": KeyboardLayout.CANADIAN_MULTILINGUAL,
}

# Canadian SKU suffixes that indicate a plain ANSI keyboard rather than the
# bilingual Multilingual Standard layout. Lenovo uses a trailing "US"/"UUS"
# (83LT000MUS), HP a "#ABA" localisation code (8L2P3UA#ABA).
#
# This MUST be anchored as a genuine SKU suffix, not tested as a bare
# substring: "US" appears inside ASUS, USED, PLUS and STATUS, and a substring
# test would mark every ASUS listing in Canada as ANSI-confirmed. The {4,}
# prefix requires a real SKU-shaped token in front of the suffix.
#
# Deliberately conservative: codes we are not confident about are left out, so
# an unrecognised SKU falls through to UNVERIFIED. Wrongly claiming ANSI is
# much more expensive than asking you to check the photos.
CANADIAN_ANSI_SKU = re.compile(r"\b[A-Z0-9]{4,}#?(?:UUS|ABA|US)\b")


def resolve_keyboard_layout(listing: Listing) -> tuple[KeyboardLayout, bool]:
    """Work out the physical layout of a listing's keyboard.

    Returns ``(layout, explicitly_stated)``. The second value matters: a
    German listing that *explicitly* says "US layout" is acceptable, whereas
    one that merely fails to mention a layout is assumed QWERTZ and rejected.

    Resolution order:
      1. Whatever the source module set on the listing (highest confidence).
      2. An explicit US/UK statement in the text.
      3. An explicit negative marker (QWERTZ/AZERTY/Nordic/bilingual).
      4. A Canadian SKU suffix that identifies English-Canada stock.
      5. The region default from config.yaml.
    """
    if listing.stated_keyboard_layout is not None:
        return listing.stated_keyboard_layout, True

    text = listing.searchable_text

    # 2. Positive statements win over everything else, including region default.
    if any(marker in text for marker in US_LAYOUT_MARKERS):
        return KeyboardLayout.ANSI, True
    if any(marker in text for marker in UK_LAYOUT_MARKERS):
        return KeyboardLayout.ISO_UK, True

    # 3. Negative markers are equally explicit, just in the other direction.
    for marker, layout in NEGATIVE_LAYOUT_MARKERS.items():
        if marker in text:
            return layout, True

    # 4. Canada is a trap: bilingual Multilingual Standard keyboards are common
    #    and are NOT ANSI. Only an English-Canada SKU suffix rescues it;
    #    otherwise it stays UNVERIFIED and gets flagged for manual checking.
    if listing.region.value == "CA":
        if CANADIAN_ANSI_SKU.search(listing.title.upper()):
            return KeyboardLayout.ANSI, False
        return KeyboardLayout.UNVERIFIED, False

    # 5. Fall back to the region's default.
    return _DEFAULTS_BY_REGION.get(listing.region.value, KeyboardLayout.UNVERIFIED), False


# Populated lazily from config on first use so this module has no import-time
# dependency on a config file being present.
_DEFAULTS_BY_REGION: dict[str, KeyboardLayout] = {}


def prime_keyboard_defaults(config: Config) -> None:
    """Load per-region default layouts out of config. Called by the pipeline."""
    _DEFAULTS_BY_REGION.clear()
    for code, region_cfg in config.regions.items():
        _DEFAULTS_BY_REGION[code.value] = region_cfg.default_keyboard


# ---------------------------------------------------------------------------
# Landed cost
# ---------------------------------------------------------------------------


def compute_landed_cost(
    listing: Listing,
    config: Config,
    rates: FxRates,
    specs: ParsedSpecs | None = None,
) -> LandedCost:
    """Normalise a listing's price into landed USD.

    Every input is preserved on the returned `LandedCost` - including the FX
    rate and the timestamp it was fetched - so a stored listing can be
    re-audited later without guessing what the numbers were on the day.
    """
    region_cfg: RegionConfig = config.region(listing.region)

    if listing.currency.upper() != region_cfg.currency.upper():
        # Not fatal - eBay occasionally reports a converted price - but the
        # region's tax rules were written for its own currency, so say so.
        raise ValueError(
            f"Listing {listing.fingerprint()} is in {listing.currency} but region "
            f"{listing.region.value} is configured for {region_cfg.currency}. "
            f"The source module should report prices in the region's own currency."
        )

    sticker = float(listing.sticker_price_local)
    shipping = float(listing.domestic_shipping_local or 0.0)

    # (1) Reclaimable tax is zero everywhere. Kept as an explicit named term
    #     rather than dropped, so the formula in the code matches the spec and
    #     so a future change of circumstances has an obvious place to live.
    reclaimable_tax = 0.0

    # (2) Checkout tax - US states and Canadian provinces only. Assessed on
    #     goods plus shipping, which is the common treatment in both countries.
    tax_rate, jurisdiction, assumed = region_cfg.tax_rate_for(listing.jurisdiction)
    taxable_base = sticker - reclaimable_tax + shipping
    destination_tax = taxable_base * tax_rate

    total_local = taxable_base + destination_tax

    # (3) FX, then (4) the regional risk premium for costs that never show on
    #     the price tag: forwarding friction, return shipping, unserviceable RMA.
    fx_rate = rates.to_usd(listing.currency)
    landed = total_local * fx_rate * (1.0 + region_cfg.risk_premium)

    cost = LandedCost(
        sticker_local=round(sticker, 2),
        currency=listing.currency.upper(),
        reclaimable_tax_local=reclaimable_tax,
        domestic_shipping_local=round(shipping, 2),
        destination_tax_local=round(destination_tax, 2),
        total_local=round(total_local, 2),
        fx_rate_to_usd=fx_rate,
        fx_source=rates.source,
        fx_fetched_at=rates.fetched_at,
        risk_premium=region_cfg.risk_premium,
        landed_usd=round(landed, 2),
        tax_jurisdiction=jurisdiction,
        tax_rate_applied=tax_rate,
        tax_rate_assumed=assumed,
        vat_embedded_rate=region_cfg.vat_in_sticker,
    )

    if specs is not None:
        if assumed and region_cfg.tax_added_at_checkout:
            specs.add_flag(Flag.UNVERIFIED_TAX_JURISDICTION)
        if rates.is_stale:
            specs.add_flag(Flag.FX_STALE)

    return cost


def explain_landed_cost(cost: LandedCost, region_cfg: RegionConfig) -> str:
    """Human-readable derivation, for alerts and for debugging a surprising rank.

    Example output::

        C$1,499.00 + C$0.00 shipping + 5.0% AB tax = C$1,573.95
        x 0.7300 FX x 1.00 risk = $1,148.98 landed
    """
    parts = [f"{cost.local_display()}"]

    if cost.domestic_shipping_local:
        parts.append(f"+ {cost.currency} {cost.domestic_shipping_local:,.2f} shipping")

    if cost.tax_rate_applied:
        label = f"{cost.tax_rate_applied * 100:.3g}%"
        where = cost.tax_jurisdiction or region_cfg.display
        assumed = " assumed" if cost.tax_rate_assumed else ""
        parts.append(f"+ {label} {where}{assumed} tax")
    elif cost.vat_embedded_rate:
        parts.append(
            f"(incl. {cost.vat_embedded_rate * 100:.3g}% VAT already in the sticker, "
            f"not reclaimable)"
        )

    line1 = " ".join(parts) + f" = {cost.currency} {cost.total_local:,.2f}"
    line2 = (
        f"x {cost.fx_rate_to_usd:.4f} FX x {1 + cost.risk_premium:.2f} risk "
        f"= {cost.usd_display()} landed"
    )
    return f"{line1}\n{line2}"
