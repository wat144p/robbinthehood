"""
Price extraction from human-written deal titles.

The European decimal cases are the ones that matter most: misreading
"1.299,00 €" as €1.30 understates a German listing by three orders of
magnitude, and it sails straight through every budget filter.
"""

from __future__ import annotations

import pytest

from dealhunter.pricing import (
    detect_currency,
    extract_price,
    looks_like_discount_claim,
    parse_number,
)


class TestNumberParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # English notation
            ("1,299", 1299.0),
            ("1,299.99", 1299.99),
            ("1299.99", 1299.99),
            ("999", 999.0),
            ("12,995", 12995.0),
            # German / European notation — the dangerous ones
            ("1.299", 1299.0),
            ("1.299,00", 1299.0),
            ("1299,00", 1299.0),
            ("1.049,99", 1049.99),
            # Swedish: space as the thousands separator
            ("12 995", 12995.0),
            ("1 299", 1299.0),
        ],
    )
    def test_both_notations(self, raw, expected):
        assert parse_number(raw) == pytest.approx(expected)

    def test_the_decisive_rule_is_digits_after_the_last_separator(self):
        """Three digits means thousands; one or two means decimal. That single
        rule disambiguates every format we see."""
        assert parse_number("1.299") == 1299.0     # 3 digits -> thousands
        assert parse_number("1,299") == 1299.0     # 3 digits -> thousands
        assert parse_number("1.29") == 1.29        # 2 digits -> decimal
        assert parse_number("1,29") == 1.29        # 2 digits -> decimal


class TestCurrencyDetection:
    @pytest.mark.parametrize(
        "text,default,expected",
        [
            ("$1,299", "USD", "USD"),
            ("£1,150", "USD", "GBP"),
            ("€1.299", "USD", "EUR"),
            ("C$1,499", "USD", "CAD"),
            ("A$1,899", "USD", "AUD"),
            ("12 995 kr", "USD", "SEK"),
            ("CAD 1499", "USD", "CAD"),
        ],
    )
    def test_explicit_markers_win(self, text, default, expected):
        assert detect_currency(text, default) == expected

    def test_a_bare_dollar_sign_means_the_feeds_own_currency(self):
        """"$" is CAD on RedFlagDeals and AUD on OzBargain."""
        assert detect_currency("$1,499", "CAD") == "CAD"
        assert detect_currency("$1,899", "AUD") == "AUD"
        assert detect_currency("$1,299", "USD") == "USD"

    def test_c_dollar_is_not_read_as_a_bare_dollar(self):
        """Marker order matters: "C$" has to be tried before "$"."""
        assert detect_currency("C$1,499", "USD") == "CAD"


class TestPriceExtraction:
    def test_typical_reddit_title(self):
        quote = extract_price(
            "[$1,199] Lenovo Legion Pro 5 16 OLED RTX 5060 32GB 1TB @ Best Buy", "USD"
        )
        assert quote.amount == 1199.0
        assert quote.currency == "USD"

    def test_german_price_is_not_read_as_one_euro(self):
        quote = extract_price(
            "MSI Vector 16 HX für 1.299,00 € bei notebooksbilliger", "EUR"
        )
        assert quote.amount == pytest.approx(1299.0)
        assert quote.currency == "EUR"

    def test_was_now_takes_the_current_price(self):
        quote = extract_price(
            "Legion 7i 16 2.5K OLED — was £1,399, now £1,150 (18% off)", "GBP"
        )
        assert quote.amount == 1150.0
        assert quote.claimed_was_price == 1399.0
        assert quote.claimed_discount_percent == 18.0

    def test_percentage_off_is_never_read_as_a_price(self):
        quote = extract_price("Gaming laptop 25% off — now $1,199", "USD")
        assert quote.amount == 1199.0

    def test_a_range_takes_the_low_end(self):
        quote = extract_price("Legion Pro 5 from $999 to $1,299", "USD")
        assert quote.amount == 999.0
        assert quote.is_range_low is True

    def test_specs_are_not_mistaken_for_prices(self):
        """"RTX 5070 Ti 12GB 1TB 240Hz 32GB" has no currency marker anywhere,
        so it must produce nothing rather than four fake prices."""
        assert extract_price(
            "Acer Predator Helios Neo 16S AI RTX 5070 Ti 12GB 32GB 1TB 240Hz", "USD"
        ) is None

    def test_hotukdeals_temperature_is_not_a_price(self):
        """HotUKDeals titles start with a deal temperature: "108° - ...". """
        quote = extract_price("108° - Gaming Laptop RTX 5060 - £1,150 - Currys", "GBP")
        assert quote.amount == 1150.0

    def test_swedish_krona(self):
        quote = extract_price("Aero X16 — 12 995 kr hos Webhallen", "SEK")
        assert quote.amount == pytest.approx(12995.0)
        assert quote.currency == "SEK"

    def test_implausible_values_are_rejected(self):
        assert extract_price("Laptop sleeve $12", "USD") is None

    def test_no_price_returns_none_rather_than_guessing(self):
        assert extract_price("Anyone know if the Legion Pro 5 is any good?", "USD") is None

    def test_pepper_merchant_price_attribute(self):
        """HotUKDeals gives us the price already separated out."""
        quote = extract_price("£1,150", "GBP")
        assert quote.amount == 1150.0
        assert quote.currency == "GBP"


class TestDiscountClaims:
    def test_percentage_claims_are_detected(self):
        assert looks_like_discount_claim("Save 30% off RRP") is True
        assert looks_like_discount_claim("Legion Pro 5 at $1,049") is False

    def test_claimed_discount_is_recorded_but_separate_from_the_price(self):
        """Per the affiliate-bias rule these are recorded and never trusted —
        inflated list prices are the commonest form of fake discount."""
        quote = extract_price("RTX 5070 Ti laptop, 40% off! Now $1,299", "USD")
        assert quote.amount == 1299.0
        assert quote.claimed_discount_percent == 40.0
