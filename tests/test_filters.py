"""
Hard filters and keyboard-layout resolution.

Filters are where good listings go to die, so each rejection path is tested
in both directions: the thing that should be rejected, and a near-miss that
should survive.
"""

from __future__ import annotations

import pytest

from dealhunter.evaluate import evaluate
from dealhunter.filters import looks_like_accessory_or_part
from dealhunter.models import KeyboardLayout, Region, RejectReason
from dealhunter.regions import resolve_keyboard_layout
from tests.fixtures import make_listing


def reasons(config, rates, **kwargs) -> list[RejectReason]:
    return evaluate(make_listing(**kwargs), config, rates).reject_reasons


# ---------------------------------------------------------------------------
# Spec filters
# ---------------------------------------------------------------------------


class TestSpecFilters:
    def test_resolution_below_1440_is_rejected(self, config, rates):
        assert RejectReason.RESOLUTION_TOO_LOW in reasons(
            config, rates, title_key="wuxga_trap", price=1100.0
        )
        assert RejectReason.RESOLUTION_TOO_LOW in reasons(
            config, rates, title_key="fhd_trap", price=1100.0
        )

    def test_qualifying_resolution_survives(self, config, rates):
        assert RejectReason.RESOLUTION_TOO_LOW not in reasons(
            config, rates, title_key="helios_neo_16s_openbox", price=1100.0
        )

    def test_over_budget_is_rejected(self, config, rates):
        assert RejectReason.OVER_BUDGET in reasons(
            config, rates, title_key="helios_neo_16s_openbox",
            price=1500.0, jurisdiction="OR",
        )

    def test_at_the_ceiling_survives(self, config, rates):
        """$1,400 exactly is inside the ceiling; a cent more is not."""
        assert RejectReason.OVER_BUDGET not in reasons(
            config, rates, title_key="helios_neo_16s_openbox",
            price=1400.0, jurisdiction="OR",
        )
        assert RejectReason.OVER_BUDGET in reasons(
            config, rates, title_key="helios_neo_16s_openbox",
            price=1400.01, jurisdiction="OR",
        )

    def test_budget_is_applied_to_landed_not_sticker(self, config, rates):
        """A £1,150 UK sticker is under 1,400 as a number and over it as a
        landed cost. This is the whole point of the normalisation."""
        assert RejectReason.OVER_BUDGET in reasons(
            config, rates, title_key="uk_listing", region=Region.GB, price=1150.0
        )

    def test_non_domestic_shipping_is_rejected(self, config, rates):
        assert RejectReason.NO_DOMESTIC_SHIPPING in reasons(
            config, rates, title_key="helios_neo_16s_openbox",
            price=1100.0, ships_domestically=False,
        )

    def test_unparseable_listing_is_rejected(self, config, rates):
        assert RejectReason.UNPARSEABLE in reasons(
            config, rates, title="Gaming Laptop 16 inch great condition",
            price=900.0,
        )

    def test_pre_30_series_gpu_is_rejected(self, config, rates):
        # The 2070 isn't in the reference table at all, so it fails as
        # unparseable — which is the correct outcome either way.
        result = reasons(config, rates, title_key="rtx_2070", price=800.0)
        assert RejectReason.UNPARSEABLE in result

    def test_ram_below_16gb_is_rejected(self, config, rates):
        assert RejectReason.RAM_TOO_LOW in reasons(
            config, rates,
            title="Lenovo Legion Pro 5 16 WQXGA OLED 165Hz RTX 5060 8GB RAM 1TB SSD",
            price=1000.0,
        )

    def test_storage_below_1tb_is_rejected(self, config, rates):
        assert RejectReason.STORAGE_TOO_LOW in reasons(
            config, rates,
            title="Lenovo Legion Pro 5 16 WQXGA OLED RTX 5060 32GB 512GB SSD",
            price=1000.0,
        )


# ---------------------------------------------------------------------------
# Keyboard layout
# ---------------------------------------------------------------------------


class TestKeyboardLayout:
    def test_german_qwertz_is_rejected(self, config, rates):
        assert RejectReason.KEYBOARD_LAYOUT in reasons(
            config, rates, title_key="german_qwertz", region=Region.DE, price=1000.0
        )

    def test_german_stock_without_a_stated_layout_is_rejected(self, config, rates):
        """Silence means QWERTZ in Germany. Only an explicit US/UK statement
        rescues German stock."""
        assert RejectReason.KEYBOARD_LAYOUT in reasons(
            config, rates, title_key="legion_pro_5_oled", region=Region.DE, price=900.0
        )

    def test_german_listing_stating_us_layout_survives(self, config, rates):
        assert RejectReason.KEYBOARD_LAYOUT not in reasons(
            config, rates, title_key="german_us_layout", region=Region.DE, price=900.0
        )

    def test_belgian_azerty_is_rejected(self, config, rates):
        assert RejectReason.KEYBOARD_LAYOUT in reasons(
            config, rates, title_key="belgian_azerty", region=Region.BE, price=1000.0
        )

    def test_swedish_stock_defaults_to_nordic_and_is_rejected(self, config, rates):
        assert RejectReason.KEYBOARD_LAYOUT in reasons(
            config, rates, title_key="legion_pro_5_oled", region=Region.SE,
            price=11000.0,
        )

    def test_uk_iso_is_accepted(self, config, rates):
        """Acceptable, not preferred — the penalty is applied in scoring."""
        assert RejectReason.KEYBOARD_LAYOUT not in reasons(
            config, rates, title_key="uk_listing", region=Region.GB, price=800.0
        )

    def test_us_listing_defaults_to_ansi(self, config):
        listing = make_listing("legion_pro_5_oled", region=Region.US)
        layout, explicit = resolve_keyboard_layout(listing)
        assert layout == KeyboardLayout.ANSI

    def test_uk_listing_defaults_to_iso(self, config):
        listing = make_listing("uk_listing", region=Region.GB)
        layout, _ = resolve_keyboard_layout(listing)
        assert layout == KeyboardLayout.ISO_UK

    def test_canadian_listing_is_unverified_by_default(self, config):
        """The Canada trap: bilingual Multilingual Standard boards are common
        and are not ANSI, so silence means 'check the photos', not 'fine'."""
        listing = make_listing("legion_5i_15", region=Region.CA)
        layout, _ = resolve_keyboard_layout(listing)
        assert layout == KeyboardLayout.UNVERIFIED

    def test_canadian_bilingual_is_detected_when_stated(self, config):
        listing = make_listing("canadian_bilingual", region=Region.CA)
        layout, explicit = resolve_keyboard_layout(listing)
        assert layout == KeyboardLayout.CANADIAN_MULTILINGUAL
        assert explicit is True

    def test_canadian_us_sku_suffix_resolves_to_ansi(self, config):
        listing = make_listing(
            title="Lenovo Legion Pro 5 16 83LT000MUS WQXGA OLED RTX 5060 32GB 1TB",
            region=Region.CA,
        )
        layout, _ = resolve_keyboard_layout(listing)
        assert layout == KeyboardLayout.ANSI

    @pytest.mark.parametrize(
        "title",
        [
            "ASUS ROG Strix G16 2560x1600 240Hz RTX 5070 Ti 32GB 1TB",
            "Lenovo Legion Pro 5 16 QHD+ RTX 5060 32GB 1TB USED good condition",
            "MSI Vector 16 HX PLUS bundle 2560x1600 RTX 5070 Ti 32GB 1TB",
        ],
    )
    def test_the_letters_us_inside_a_word_do_not_confirm_ansi(self, config, title):
        """A bare substring test for "US" fires on ASUS, USED and PLUS, which
        would silently mark most Canadian stock as ANSI-confirmed. The suffix
        has to be anchored to a real SKU-shaped token."""
        listing = make_listing(title=title, region=Region.CA)
        layout, _ = resolve_keyboard_layout(listing)
        assert layout == KeyboardLayout.UNVERIFIED

    def test_hp_localisation_code_resolves_to_ansi(self, config):
        listing = make_listing(
            title="HP OMEN MAX 16 8L2P3UA#ABA 2560x1600 240Hz RTX 5070 Ti 32GB 1TB",
            region=Region.CA,
        )
        layout, _ = resolve_keyboard_layout(listing)
        assert layout == KeyboardLayout.ANSI

    def test_qwertz_is_rejected_even_on_a_us_marketplace(self, config, rates):
        """A QWERTZ machine listed on eBay US is still a QWERTZ machine."""
        assert RejectReason.KEYBOARD_LAYOUT in reasons(
            config, rates, title_key="german_qwertz", region=Region.US, price=1000.0
        )


# ---------------------------------------------------------------------------
# Multiple reasons
# ---------------------------------------------------------------------------


def test_all_failing_reasons_are_collected(config, rates):
    """Debugging a vanished listing is much easier with the full list."""
    result = reasons(
        config, rates,
        title="HP Omen 16 WUXGA 1920x1200 144Hz RTX 4060 8GB RAM 512GB SSD",
        price=2000.0,
    )
    assert RejectReason.RESOLUTION_TOO_LOW in result
    assert RejectReason.STORAGE_TOO_LOW in result
    assert RejectReason.OVER_BUDGET in result
    assert len(result) >= 3


# ---------------------------------------------------------------------------
# Accessories and repair parts sold under a full laptop's model name
# ---------------------------------------------------------------------------
#
# A search for "Acer Predator Helios Neo 16S AI" also returns a replacement
# motherboard, a RAM kit, an SSD "compatible with" that laptop. Most fail
# UNPARSEABLE on their own. The dangerous case is the one that doesn't: a
# seller writes "RAM Kit Compatible with [full model + full spec sheet]" for
# search visibility, and that title recites every field the parser looks for
# — which would otherwise pass every hard filter and get scored as if the
# $40 RAM stick WERE the $1,200 laptop it merely fits.
# ---------------------------------------------------------------------------


class TestAccessoryDetection:
    @pytest.mark.parametrize(
        "title",
        [
            # Real titles observed in a live eBay run during this project's
            # build, before this filter existed.
            "Acer Predator Helios Neo 16S AI OLED PHN16S-71 Mainboard Laptop "
            "Reparatur Repair",
            "M.2 Gen4 SSD passend für Acer Predator Helios Neo 16S AI OLED "
            "PHN16S-71-90EF",
            "32GB 16GB RAM Speicher für Acer Predator Helios Neo 16S AI OLED "
            "PHN16S-71-90EF",
            "32GB 2x 16GB SODIMM DDR5-5600 RAM Compatible for Acer Predator "
            "Helios Neo 16S AI",
            "Refurbished Razer Core X V2 Enclosure, Thunderbolt 5, PCIe 4.0 "
            "x16, eGPU External Graphics",
            "Razer Laptop Cooling Pad, Black, Adaptive Smart Cooling",
            "Razer Laptop Stand Chroma V2, RGB Lighting",
            "MSI Gaming 1 Year Warranty Extension for MSI G Series Gaming "
            "Laptops",
            "ASUS ProArt Backpack, Black, Water Repellent",
            "Replacement Battery for Acer Predator Helios Neo 16S AI",
            "New Keyboard for Lenovo Legion Pro 5 16 83LT000MUS",
        ],
    )
    def test_real_accessory_titles_are_caught(self, title):
        assert looks_like_accessory_or_part(title) is not None

    @pytest.mark.parametrize(
        "title",
        [
            # A genuine laptop legitimately says "32GB RAM", "1TB SSD", "OLED
            # display", "backlit keyboard", "long battery life" — none of
            # that should trip the accessory detector.
            "Acer Predator Helios Neo 16S AI 16\" 2560x1600 240Hz OLED Core "
            "Ultra 9 275HX RTX 5070 Ti 12GB 140W 32GB 1TB SSD",
            "Lenovo Legion Pro 5 16 83LT000MUS 16\" WQXGA OLED 165Hz Ryzen 7 "
            "8745HX RTX 5060 8GB @115W 32GB 1TB SSD",
            "HP OMEN 16 with backlit keyboard, long battery life, 32GB RAM "
            "1TB SSD RTX 5060",
            "MSI Vector 16 HX 16\" QHD+ 240Hz IPS Ryzen 9 7945HX RTX 5070 Ti "
            "12GB 140W TGP 32GB DDR5 1TB NVMe",
        ],
    )
    def test_genuine_laptops_are_not_falsely_caught(self, title):
        assert looks_like_accessory_or_part(title) is None

    def test_a_part_that_recites_the_full_host_laptops_spec_sheet_is_rejected(
        self, config, rates
    ):
        """The dangerous case: an accessory's title includes every field the
        parser looks for, priced at a fraction of the real laptop. Without
        this filter it would pass every spec check and score as a genuine,
        absurdly-cheap deal."""
        listing_title = (
            "32GB RAM Kit Compatible with Acer Predator Helios Neo 16S AI "
            "OLED 2560x1600 240Hz RTX 5070 Ti 12GB 1TB SSD"
        )
        result = reasons(config, rates, title=listing_title, price=45.0)
        assert RejectReason.ACCESSORY_OR_PART in result

    def test_accessory_check_runs_even_when_parsing_otherwise_succeeds(
        self, config, rates
    ):
        """Confirms the filter is checked independent of parse success, not
        just as a fallback for titles that fail to parse."""
        from dealhunter.parsing import parse_listing

        listing_title = (
            "32GB RAM Kit Compatible with Acer Predator Helios Neo 16S AI "
            "OLED 2560x1600 240Hz RTX 5070 Ti 12GB 1TB SSD"
        )
        specs = parse_listing(listing_title)
        # The title DOES parse cleanly — that is exactly what makes it
        # dangerous, and exactly why this cannot rely on UNPARSEABLE alone.
        assert specs.gpu_model is not None
        assert specs.resolution_h is not None
        assert specs.storage_gb is not None

        result = reasons(config, rates, title=listing_title, price=45.0)
        assert RejectReason.UNPARSEABLE not in result
        assert RejectReason.ACCESSORY_OR_PART in result
