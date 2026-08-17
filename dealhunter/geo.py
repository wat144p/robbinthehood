"""
Postal code -> tax jurisdiction.

Only worth doing where it changes the landed price materially:

* **Canada** — the province determines GST/HST, a swing from 5% (Alberta) to
  15% (Atlantic provinces). Canadian postal codes encode the province in their
  first letter, so this mapping is exact and tiny.

* **United States** — we only try to identify the four zero-sales-tax states.
  A full ZIP-to-state table is thousands of entries and buys us nothing: every
  other state falls back to the pessimistic 7% rate anyway, and getting a
  taxed state slightly wrong doesn't change a decision. Correctly spotting a
  0% state does.

When we can't tell, we return `None` and the region config applies its
deliberately pessimistic fallback rate, flagged as assumed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canada — first letter of the postal code identifies the province exactly
# ---------------------------------------------------------------------------
CANADA_POSTAL_PREFIX_TO_PROVINCE: dict[str, str] = {
    "A": "NL",   # Newfoundland and Labrador
    "B": "NS",   # Nova Scotia
    "C": "PE",   # Prince Edward Island
    "E": "NB",   # New Brunswick
    "G": "QC",   # Quebec - eastern
    "H": "QC",   # Quebec - Montreal
    "J": "QC",   # Quebec - western
    "K": "ON",   # Ontario - eastern
    "L": "ON",   # Ontario - central
    "M": "ON",   # Ontario - Toronto
    "N": "ON",   # Ontario - southwestern
    "P": "ON",   # Ontario - northern
    "R": "MB",   # Manitoba
    "S": "SK",   # Saskatchewan
    "T": "AB",   # Alberta  <- the 5% GST-only province worth hunting for
    "V": "BC",   # British Columbia
    "X": "NT",   # Northwest Territories / Nunavut (both 5%, so NT is safe)
    "Y": "YT",   # Yukon
}

# ---------------------------------------------------------------------------
# United States — ZIP prefix ranges for the zero-sales-tax states only
# ---------------------------------------------------------------------------
# (inclusive_low, inclusive_high, state) on the first three ZIP digits.
US_ZERO_TAX_ZIP_RANGES: list[tuple[int, int, str]] = [
    (30, 38, "NH"),      # New Hampshire: 030xx-038xx
    (197, 199, "DE"),    # Delaware
    (590, 599, "MT"),    # Montana
    (970, 979, "OR"),    # Oregon
]


def jurisdiction_from_postal(country: str, postal_code: str | None) -> str | None:
    """Best-effort state/province from a postal code.

    Returns an uppercase two-letter code, or ``None`` when we can't tell (which
    is most of the time in the US, by design).
    """
    if not postal_code:
        return None

    country = (country or "").upper()
    postal = postal_code.strip().upper().replace(" ", "")

    if country == "CA":
        if postal and postal[0] in CANADA_POSTAL_PREFIX_TO_PROVINCE:
            return CANADA_POSTAL_PREFIX_TO_PROVINCE[postal[0]]
        return None

    if country == "US":
        digits = "".join(ch for ch in postal if ch.isdigit())[:5]
        if len(digits) < 3:
            return None
        prefix = int(digits[:3])
        for low, high, state in US_ZERO_TAX_ZIP_RANGES:
            if low <= prefix <= high:
                return state
        # Somewhere taxed. Which state doesn't change the decision, so we let
        # the config's pessimistic fallback handle it rather than guessing.
        return None

    # Everywhere else, tax is inside the sticker and jurisdiction is irrelevant.
    return None
