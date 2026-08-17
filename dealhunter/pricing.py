"""
Extracting prices from free text.

Tier 0 and Tier 1 sources don't hand us structured prices — they hand us deal
post titles written by humans:

    "[$1,199] Lenovo Legion Pro 5 16 OLED RTX 5060 32GB 1TB @ Best Buy"
    "Legion 7i 16 2.5K OLED — was £1,399, now £1,150 (18% off)"
    "MSI Vector 16 HX für 1.299,00 € bei notebooksbilliger"
    "[AMAZON] Aero X16 — 12 995 kr (spara 2 000 kr)"

Three things here will bite you if you're careless:

1.  **European decimal notation.** "1.299,00 €" is one thousand two hundred and
    ninety-nine euros, not one euro thirty. Reading it naively understates a
    German listing by three orders of magnitude and it sails past every filter.

2.  **"$" is not USD.** In a RedFlagDeals feed it's CAD; in OzBargain it's AUD.
    The feed's region decides, unless the text says otherwise.

3.  **Discount claims are not prices.** "25% off" and "was $1,499" must never
    be mistaken for the current price — and per the affiliate-bias rule, the
    claimed discount is recorded but never trusted. We verify against our own
    price history instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Currency detection
# ---------------------------------------------------------------------------

# Explicit markers that override the feed's default currency. Order matters:
# the longer, more specific ones have to be tried first, or "C$" gets read as
# "$" and a Canadian price is treated as US dollars.
CURRENCY_MARKERS: list[tuple[str, str]] = [
    (r"CAD|C\$|CA\$", "CAD"),
    (r"AUD|A\$|AU\$", "AUD"),
    (r"NZD|NZ\$", "NZD"),          # recognised so it can be *rejected*, not used
    (r"USD|US\$", "USD"),
    (r"GBP|£", "GBP"),
    (r"EUR|€", "EUR"),
    (r"SEK|kr\b|:-", "SEK"),
    (r"\$", None),                 # bare $ — defer to the feed's own currency
]

# Phrases that introduce a *former* price. Anything they introduce is a
# reference price, never the thing we would actually pay.
_WAS_PRICE = re.compile(
    r"\b(?:was|list|rrp|msrp|orig(?:inally)?|before|reg(?:ular)?|"
    r"statt|uvp|ord\.?pris)\b",
    re.IGNORECASE,
)

# Phrases that introduce the current price, which wins over anything else.
_NOW_PRICE = re.compile(
    r"\b(?:now|deal|sale|price|only|just|jetzt|für|nu|nur)\b[:\s]*",
    re.IGNORECASE,
)

_PERCENT_OFF = re.compile(r"(\d{1,2}(?:\.\d)?)\s*%\s*(?:off|rabatt|reduziert|réduction)?",
                          re.IGNORECASE)

# A number that could be money: 1299, 1,299, 1.299,00, 1 299, 1299.99
#
# The `\s?` before the decimal digits is not cosmetic: Scan UK renders prices
# as "£ 3,799. 99", with a space after the decimal point. Without it the pence
# are silently dropped, understating every price on that site.
_NUMBER = r"\d{1,3}(?:[.,\s ]\d{3})*(?:[.,]\s?\d{1,2})?|\d+(?:[.,]\s?\d{1,2})?"


@dataclass
class PriceQuote:
    """A price found in free text, with everything we know about it."""

    amount: float
    currency: str
    matched_text: str
    #: A discount the source *claimed*. Recorded, never trusted, never scored —
    #: inflated list prices are the most common form of fake discount.
    claimed_discount_percent: float | None = None
    claimed_was_price: float | None = None
    #: True when the text gave a range ("$999-$1,299") and we took the low end.
    is_range_low: bool = False


def parse_number(raw: str) -> float | None:
    """Parse a money-shaped string into a float, handling both notations.

    The decisive rule: look at how many digits follow the **last** separator.
    Exactly three means it was a thousands separator; one or two means it was
    the decimal point. That single rule handles every format we see:

        "1,299"    -> 1299.00   (English thousands)
        "1.299"    -> 1299.00   (German thousands)
        "1299,00"  -> 1299.00   (German decimal)
        "1299.99"  -> 1299.99   (English decimal)
        "12 995"   -> 12995.00  (Swedish thousands)
        "1.299,00" -> 1299.00   (German, both separators)
    """
    cleaned = raw.strip().replace(" ", "").replace(" ", "")
    if not cleaned:
        return None

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        # Whichever comes last is the decimal separator.
        if cleaned.rindex(".") > cleaned.rindex(","):
            cleaned = cleaned.replace(",", "")            # 1,299.00
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")   # 1.299,00
    elif has_comma or has_dot:
        separator = "," if has_comma else "."
        tail = cleaned.rsplit(separator, 1)[1]
        if len(tail) == 3:
            cleaned = cleaned.replace(separator, "")       # thousands
        else:
            cleaned = cleaned.replace(separator, ".")      # decimal

    try:
        return float(cleaned)
    except ValueError:
        return None


def detect_currency(text: str, default: str) -> str:
    """Work out which currency a price is in.

    A bare "$" means whatever the feed's region uses — CAD on RedFlagDeals,
    AUD on OzBargain, USD on Slickdeals — which is why `default` is required
    rather than optional.
    """
    for pattern, currency in CURRENCY_MARKERS:
        if re.search(pattern, text, re.IGNORECASE):
            return currency or default
    return default


def extract_price(
    text: str,
    default_currency: str,
    *,
    min_plausible: float = 100.0,
    max_plausible: float = 100_000.0,
) -> PriceQuote | None:
    """Pull the current price out of a deal post title or body.

    Returns `None` rather than guessing when nothing money-shaped is present.
    The plausibility bounds are wide on purpose: they exist to reject "16GB"
    and "2024", not to enforce the budget — that is the landed-cost filter's
    job, and doing it here would hide listings we want to log.
    """
    currency = detect_currency(text, default_currency)
    candidates = _find_price_candidates(text, currency)
    if not candidates:
        return None

    plausible = [
        c for c in candidates if min_plausible <= c["amount"] <= max_plausible
    ]
    if not plausible:
        return None

    # A "was" price is a reference, never what we'd pay. Drop them unless that
    # would leave us with nothing.
    current = [c for c in plausible if not c["is_was"]] or plausible

    # Prefer a price explicitly marked as the current one; otherwise take the
    # lowest, which is both the conservative reading of a range and the right
    # answer for "was X, now Y".
    marked_now = [c for c in current if c["is_now"]]
    chosen = min(marked_now or current, key=lambda c: c["amount"])

    was_prices = [c["amount"] for c in plausible if c["is_was"]]
    claimed_was = max(was_prices) if was_prices else None

    discount_match = _PERCENT_OFF.search(text)
    claimed_discount = float(discount_match.group(1)) if discount_match else None

    return PriceQuote(
        amount=chosen["amount"],
        currency=currency,
        matched_text=chosen["text"],
        claimed_discount_percent=claimed_discount,
        claimed_was_price=claimed_was,
        is_range_low=len(current) > 1 and not marked_now,
    )


def _find_price_candidates(text: str, currency: str) -> list[dict]:
    """Every money-shaped number in the text, with its surrounding context."""
    # A number only counts as money if a currency marker sits beside it. This
    # is what stops "RTX 5070 Ti 12GB 1TB 240Hz" producing four fake prices.
    symbols = r"\$|£|€|kr|:-|USD|CAD|GBP|EUR|SEK|AUD|C\$|A\$|US\$|AU\$|CA\$"
    pattern = re.compile(
        rf"(?:(?P<pre>{symbols})\s*(?P<amount1>{_NUMBER}))"
        rf"|(?:(?P<amount2>{_NUMBER})\s*(?P<post>{symbols}))",
        re.IGNORECASE,
    )

    candidates = []
    for match in pattern.finditer(text):
        raw = match.group("amount1") or match.group("amount2")
        amount = parse_number(raw)
        if amount is None:
            continue

        # Look back a short way to see whether this is a former price, and
        # whether it is explicitly flagged as the current one.
        before = text[max(0, match.start() - 30): match.start()]

        candidates.append({
            "amount": amount,
            "text": match.group(0).strip(),
            "is_was": bool(_WAS_PRICE.search(before)),
            "is_now": bool(_NOW_PRICE.search(before)),
            "position": match.start(),
        })

    return candidates


def looks_like_discount_claim(text: str) -> bool:
    """True when the text advertises a percentage off.

    Per the affiliate-bias rule these claims are never treated as truth:
    inflated list prices are the commonest form of fake discount, and most of
    these sites earn commission on the outbound click.
    """
    return bool(_PERCENT_OFF.search(text))
