"""
The scoring engine.

Each rubric component is tested at its tier boundaries, then the whole thing is
exercised end-to-end on realistic listings to check that the ordering it
produces matches the ordering a human would produce.
"""

from __future__ import annotations

import pytest

from dealhunter.evaluate import evaluate
from dealhunter.models import (
    Condition,
    Flag,
    KeyboardLayout,
    PanelType,
    ParsedSpecs,
    Region,
)
from dealhunter.regions import compute_landed_cost
from dealhunter.scoring import score_listing
from tests.fixtures import make_listing


def component(result, name: str):
    return next(c for c in result.components if c.name == name)


def score_of(config, rates, **kwargs):
    """Evaluate a listing and return its ScoreResult (asserting it passed)."""
    evaluated = evaluate(make_listing(**kwargs), config, rates)
    assert not evaluated.rejected, f"unexpectedly rejected: {evaluated.reject_reasons}"
    return evaluated.score


def score_specs(config, rates, specs: ParsedSpecs, **listing_kwargs):
    """Score a hand-built ParsedSpecs, bypassing the parser.

    Used for the tier-boundary tests, where writing a title that parses to
    exactly 24 GB of RAM would obscure what is being tested.
    """
    listing = make_listing("legion_pro_5_oled", **listing_kwargs)
    landed = compute_landed_cost(listing, config, rates)
    return score_listing(listing, specs, landed, config, KeyboardLayout.ANSI)


# ---------------------------------------------------------------------------
# Component tiers
# ---------------------------------------------------------------------------


class TestVRAMComponent:
    @pytest.mark.parametrize("vram,expected", [(16, 30), (12, 26), (8, 14), (24, 30)])
    def test_tiers(self, config, rates, vram, expected):
        specs = ParsedSpecs(gpu_model="RTX 5070 Ti", vram_gb=vram)
        assert component(score_specs(config, rates, specs), "VRAM").points == expected

    def test_vram_is_the_heaviest_component(self, config):
        assert config.scoring["vram"]["max_points"] == 30

    def test_unverified_vram_is_labelled(self, config, rates):
        specs = ParsedSpecs(gpu_model="RTX 5070", vram_gb=8, vram_verified=False)
        assert "UNVERIFIED" in component(score_specs(config, rates, specs), "VRAM").detail


class TestBandwidthComponent:
    @pytest.mark.parametrize(
        "bandwidth,expected",
        [
            (256, 0.0),      # floor anchor
            (672, 10.0),     # ceiling anchor
            (464, 5.0),      # midpoint
            (384, 3.1),      # RTX 5060 / 5070
            (200, 0.0),      # below the floor, clamped
            (900, 10.0),     # above the ceiling, clamped
        ],
    )
    def test_linear_scale(self, config, rates, bandwidth, expected):
        specs = ParsedSpecs(memory_bandwidth_gbs=bandwidth)
        points = component(score_specs(config, rates, specs), "Bandwidth").points
        assert points == pytest.approx(expected, abs=0.05)

    def test_5070ti_beats_5060_on_bandwidth(self, config, rates):
        fast = score_specs(config, rates, ParsedSpecs(memory_bandwidth_gbs=672))
        slow = score_specs(config, rates, ParsedSpecs(memory_bandwidth_gbs=384))
        assert component(fast, "Bandwidth").points > component(slow, "Bandwidth").points


class TestPanelComponent:
    @pytest.mark.parametrize("res", [(2560, 1600), (2560, 1440)])
    def test_oled_at_preferred_resolution(self, config, rates, res):
        specs = ParsedSpecs(panel_type=PanelType.OLED,
                            resolution_w=res[0], resolution_h=res[1])
        assert component(score_specs(config, rates, specs), "Panel").points == 15

    def test_oled_at_other_resolution(self, config, rates):
        specs = ParsedSpecs(panel_type=PanelType.OLED,
                            resolution_w=3840, resolution_h=2160)
        assert component(score_specs(config, rates, specs), "Panel").points == 12

    def test_bright_ips(self, config, rates):
        specs = ParsedSpecs(panel_type=PanelType.IPS,
                            resolution_w=2560, resolution_h=1600, nits=400)
        assert component(score_specs(config, rates, specs), "Panel").points == 7

    def test_dim_or_unknown_ips(self, config, rates):
        specs = ParsedSpecs(panel_type=PanelType.IPS,
                            resolution_w=2560, resolution_h=1600, nits=300)
        assert component(score_specs(config, rates, specs), "Panel").points == 5

    def test_unverified_panel_scores_as_ips(self, config, rates):
        specs = ParsedSpecs(panel_type=PanelType.UNVERIFIED,
                            resolution_w=2560, resolution_h=1600)
        result = score_specs(config, rates, specs)
        assert component(result, "Panel").points == 5
        assert "UNVERIFIED" in component(result, "Panel").detail


class TestSystemRAMComponent:
    @pytest.mark.parametrize("ram,expected", [(32, 15), (24, 10), (16, 6), (64, 15)])
    def test_tiers(self, config, rates, ram, expected):
        specs = ParsedSpecs(system_ram_gb=ram)
        assert component(score_specs(config, rates, specs), "RAM").points == expected

    def test_single_channel_penalty(self, config, rates):
        specs = ParsedSpecs(system_ram_gb=16, single_channel=True)
        result = score_specs(config, rates, specs)
        assert component(result, "RAM").points == 3        # 6 - 3
        assert "SINGLE-CHANNEL" in component(result, "RAM").detail


class TestStorageComponent:
    @pytest.mark.parametrize("gb,expected", [(2000, 8), (1000, 5), (4000, 8)])
    def test_tiers(self, config, rates, gb, expected):
        specs = ParsedSpecs(storage_gb=gb)
        assert component(score_specs(config, rates, specs), "Storage").points == expected

    def test_free_m2_bonus(self, config, rates):
        specs = ParsedSpecs(storage_gb=1000, free_m2_slot=True)
        assert component(score_specs(config, rates, specs), "Storage").points == 7

    def test_dock_storage_is_noted_in_the_detail(self, config, rates):
        specs = ParsedSpecs(storage_gb=1000, dock_storage_gb=1000)
        detail = component(score_specs(config, rates, specs), "Storage").detail
        assert "dock storage ignored" in detail


class TestTGPComponent:
    @pytest.mark.parametrize(
        "watts,expected", [(140, 10), (175, 10), (115, 8), (139, 8), (100, 5),
                           (114, 5), (85, 2), (60, 2)]
    )
    def test_tiers(self, config, rates, watts, expected):
        specs = ParsedSpecs(tgp_watts=watts)
        assert component(score_specs(config, rates, specs), "TGP").points == expected

    def test_unknown_tgp_is_midrange_and_flagged(self, config, rates):
        result = score_specs(config, rates, ParsedSpecs(tgp_watts=None))
        assert component(result, "TGP").points == 4
        assert Flag.UNVERIFIED_TGP in result.flags

    def test_a_115w_xx60_beats_an_85w_xx70(self, config, rates):
        """TGP matters more than the model name."""
        xx60 = score_specs(config, rates,
                           ParsedSpecs(gpu_model="RTX 5060", vram_gb=8, tgp_watts=115))
        xx70 = score_specs(config, rates,
                           ParsedSpecs(gpu_model="RTX 5070", vram_gb=8, tgp_watts=85))
        assert component(xx60, "TGP").points > component(xx70, "TGP").points


class TestConditionAndTrust:
    def test_new_from_major_retailer(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1300.0, jurisdiction="OR",
                          condition=Condition.NEW, seller_name="Best Buy")
        assert component(result, "Condition/Trust").points == 12

    def test_manufacturer_certified_refurb(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1300.0, jurisdiction="OR",
                          condition=Condition.MFR_CERTIFIED_REFURB,
                          seller_name="Acer Recertified")
        assert component(result, "Condition/Trust").points == 11

    @pytest.mark.parametrize(
        "condition,expected",
        [
            (Condition.OPEN_BOX_EXCELLENT, 10),
            (Condition.OPEN_BOX_GOOD, 8),
            (Condition.OPEN_BOX_FAIR, 6),
        ],
    )
    def test_open_box_tiers(self, config, rates, condition, expected):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1300.0, jurisdiction="OR", condition=condition)
        assert component(result, "Condition/Trust").points == expected

    def test_ebay_refurb_from_a_big_seller(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1300.0, jurisdiction="OR",
                          condition=Condition.EBAY_REFURBISHED,
                          seller_name="acer", feedback=25000, percent=99.4)
        assert component(result, "Condition/Trust").points == 9

    def test_ebay_refurb_from_a_small_seller_falls_back_to_used(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1300.0, jurisdiction="OR",
                          condition=Condition.EBAY_REFURBISHED,
                          seller_name="somebody", feedback=500, percent=99.4)
        assert component(result, "Condition/Trust").points < 9

    def test_trusted_used_seller(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1300.0, jurisdiction="OR",
                          condition=Condition.USED, seller_name="someone",
                          feedback=5000, percent=98.5)
        assert component(result, "Condition/Trust").points == 6

    def test_low_feedback_seller_is_high_risk(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1300.0, jurisdiction="OR",
                          condition=Condition.USED, seller_name="newbie",
                          feedback=12, percent=100.0)
        assert component(result, "Condition/Trust").points == 2
        assert Flag.HIGH_RISK in result.flags


class TestJunkTitles:
    @pytest.mark.parametrize(
        "title_key", ["junk_read_as_is", "junk_no_battery"]
    )
    def test_junk_titles_are_penalised_and_flagged(self, config, rates, title_key):
        evaluated = evaluate(
            make_listing(title_key, price=700.0, jurisdiction="OR",
                         condition=Condition.USED, feedback=3000, percent=99.0),
            config, rates,
        )
        if evaluated.rejected:
            pytest.skip(f"rejected by hard filters: {evaluated.reject_reasons}")
        assert Flag.HIGH_RISK in evaluated.score.flags
        assert component(evaluated.score, "Condition/Trust").points < 0

    def test_junk_penalty_is_exactly_15_on_an_otherwise_identical_listing(
        self, config, rates
    ):
        """Controlled comparison: same machine, same price, same seller — the
        only difference is the junk marker in the title."""
        from tests.fixtures import TITLES

        base = TITLES["msi_vector_16hx"]
        common = dict(price=1250.0, jurisdiction="OR", condition=Condition.USED,
                      seller_name="someone", feedback=3000, percent=99.0)

        clean = score_of(config, rates, title=base, **common)
        junk = score_of(config, rates, title=base + " - AS IS", **common)

        assert clean.total - junk.total == pytest.approx(15.0)
        assert Flag.HIGH_RISK in junk.flags
        assert junk.total < config.notification["immediate_alert_score"]

    def test_read_does_not_fire_on_already_or_thread(self, config):
        from dealhunter.scoring import _junk_markers_in

        markers = config.scoring["condition"]["junk_title_markers"]
        assert _junk_markers_in("Ready to ship, already tested, threaded", markers) == []
        assert "read" in _junk_markers_in("Please READ description", markers)


class TestSuspiciouslyCheap:
    def test_cheap_and_untrusted_is_punished(self, config, rates):
        result = score_of(config, rates, title_key="legion_5_pro_16ach6h_used",
                          price=800.0, jurisdiction="OR",
                          condition=Condition.USED, seller_name="newbie",
                          feedback=8, percent=100.0)
        assert Flag.SUSPICIOUSLY_CHEAP in result.flags
        assert Flag.HIGH_RISK in result.flags
        # 2 base, minus half of the 10 points it failed to earn.
        assert component(result, "Condition/Trust").points == pytest.approx(-3.0)

    def test_cheap_from_best_buy_is_not_punished(self, config, rates):
        """A cheap machine from a trusted seller is just a good deal."""
        result = score_of(config, rates, title_key="legion_pro_5_oled",
                          price=950.0, jurisdiction="OR",
                          condition=Condition.NEW, seller_name="Best Buy")
        assert Flag.SUSPICIOUSLY_CHEAP in result.flags
        assert component(result, "Condition/Trust").points == 12


class TestPriceVersusFloor:
    def test_at_the_floor_earns_the_full_bonus(self, config, rates):
        # Legion Pro 5 floor is $1,049.
        result = score_of(config, rates, title_key="legion_pro_5_oled",
                          price=1049.0, jurisdiction="OR")
        assert component(result, "Price vs floor").points == 10

    def test_below_the_floor_is_flagged_as_a_record(self, config, rates):
        result = score_of(config, rates, title_key="legion_pro_5_oled",
                          price=999.0, jurisdiction="OR")
        assert Flag.BELOW_KNOWN_FLOOR in result.flags
        assert component(result, "Price vs floor").points == 10

    def test_25_percent_above_the_floor_is_the_full_penalty(self, config, rates):
        result = score_of(config, rates, title_key="legion_pro_5_oled",
                          price=1049.0 * 1.25, jurisdiction="OR")
        assert component(result, "Price vs floor").points == pytest.approx(-10)

    def test_midway_is_linear(self, config, rates):
        # 12.5% above the floor should land halfway between +10 and -10.
        result = score_of(config, rates, title_key="legion_pro_5_oled",
                          price=1049.0 * 1.125, jurisdiction="OR")
        assert component(result, "Price vs floor").points == pytest.approx(0, abs=0.1)

    def test_unknown_model_is_neutral_and_flagged(self, config, rates):
        result = score_of(config, rates, title_key="explicit_5070_12gb",
                          price=1200.0, jurisdiction="OR")
        assert component(result, "Price vs floor").points == 0
        assert Flag.NO_KNOWN_FLOOR in result.flags

    def test_a_database_floor_overrides_the_config_seed(self, config, rates):
        """When a verified lower price has been seen, it becomes the baseline."""
        listing = make_listing("legion_pro_5_oled", price=1049.0, jurisdiction="OR")
        tighter = evaluate(listing, config, rates, floor_override_usd=900.0)
        assert component(tighter.score, "Price vs floor").points < 10


class TestKeyboardModifier:
    def test_ansi_is_free(self, config, rates):
        result = score_of(config, rates, title_key="legion_pro_5_oled",
                          price=1100.0, jurisdiction="OR")
        assert component(result, "Keyboard").points == 0

    def test_uk_iso_costs_four_points(self, config, rates):
        result = score_of(config, rates, title_key="uk_listing",
                          region=Region.GB, price=800.0)
        assert component(result, "Keyboard").points == -4
        assert Flag.ISO_KEYBOARD_PENALTY in result.flags

    def test_unverified_canadian_layout_is_flagged_not_penalised(self, config, rates):
        result = score_of(config, rates, title_key="legion_5i_15",
                          region=Region.CA, price=1500.0, jurisdiction="AB")
        assert component(result, "Keyboard").points == 0
        assert Flag.UNVERIFIED_KEYBOARD in result.flags


# ---------------------------------------------------------------------------
# Whole-listing behaviour
# ---------------------------------------------------------------------------


class TestPriorityOverrides:
    def test_helios_neo_under_1250_triggers_regardless_of_score(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1184.0, jurisdiction="OR",
                          condition=Condition.OPEN_BOX_EXCELLENT)
        assert result.priority is True
        assert "Helios Neo" in result.priority_reason

    def test_helios_neo_above_1250_does_not_force_an_alert(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1330.0, jurisdiction="OR",
                          condition=Condition.OPEN_BOX_GOOD)
        assert result.priority is False

    def test_confirmed_5070_12gb_is_always_priority(self, config, rates):
        result = score_of(config, rates, title_key="explicit_5070_12gb",
                          price=1350.0, jurisdiction="OR")
        assert Flag.RTX_5070_12GB in result.flags
        assert result.priority is True

    def test_ambiguous_5070_is_not_treated_as_the_12gb_variant(self, config, rates):
        result = score_of(config, rates, title_key="ambiguous_5070",
                          price=1015.0, jurisdiction="OR",
                          condition=Condition.OPEN_BOX_EXCELLENT)
        assert Flag.RTX_5070_12GB not in result.flags
        assert Flag.UNVERIFIED_VRAM in result.flags


class TestScoreBounds:
    def test_scores_stay_inside_0_to_100(self, config, rates):
        best = score_of(config, rates, title_key="helios_neo_16s_openbox",
                        price=1000.0, jurisdiction="OR",
                        condition=Condition.NEW, seller_name="Best Buy")
        assert 0 <= best.total <= 100

    def test_the_priority_target_scores_very_well_at_its_floor(self, config, rates):
        """The Helios Neo is the only machine hitting every preferred spec, so
        at its known floor it should clear the immediate-alert threshold."""
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1184.0, jurisdiction="OR",
                          condition=Condition.OPEN_BOX_EXCELLENT)
        assert result.total >= config.notification["immediate_alert_score"]

    def test_breakdown_line_names_the_biggest_drivers(self, config, rates):
        result = score_of(config, rates, title_key="helios_neo_16s_openbox",
                          price=1184.0, jurisdiction="OR",
                          condition=Condition.OPEN_BOX_EXCELLENT)
        line = result.breakdown_line()
        assert "VRAM" in line
        assert len(line.split("|")) == 3


class TestRelativeRanking:
    def test_oled_12gb_beats_ips_8gb_at_the_same_price(self, config, rates):
        good = score_of(config, rates, title_key="helios_neo_16s_openbox",
                        price=1250.0, jurisdiction="OR",
                        condition=Condition.OPEN_BOX_EXCELLENT)
        worse = score_of(config, rates, title_key="ambiguous_5070",
                         price=1250.0, jurisdiction="OR",
                         condition=Condition.OPEN_BOX_EXCELLENT)
        assert good.total > worse.total

    def test_identical_machine_scores_lower_in_the_uk(self, config, rates):
        """Same specs, same number on the tag: the UK one is worse because of
        embedded VAT, FX, the risk premium and the ISO keyboard."""
        us = score_of(config, rates, title_key="msi_vector_16hx",
                      region=Region.US, price=1299.0, jurisdiction="OR",
                      condition=Condition.NEW, seller_name="Newegg")
        uk = evaluate(
            make_listing("msi_vector_16hx", region=Region.GB, price=1000.0,
                         condition=Condition.NEW, seller_name="Scan"),
            config, rates,
        )
        assert not uk.rejected
        assert us.total > uk.score.total
