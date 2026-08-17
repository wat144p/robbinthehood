"""
Tests for spec extraction and every spec-trap validator.

There is one test class per trap from the brief, so when a listing gets
misparsed in the wild you can add a fixture to the matching class and know
exactly which rule is being exercised.
"""

from __future__ import annotations

import pytest

from dealhunter import hardware
from dealhunter.models import Flag, PanelType
from dealhunter.parsing import parse_listing
from tests.fixtures import TITLES


def parse(title_key: str, config=None):
    return parse_listing(TITLES[title_key], config)


# ---------------------------------------------------------------------------
# Trap 1 — "RTX 5070" is ambiguous
# ---------------------------------------------------------------------------


class TestAmbiguousRTX5070:
    def test_bare_5070_is_unverified_and_scored_at_8gb(self):
        """Same die, same cores, same bus — only module density differs, so a
        bare 'RTX 5070' could be either. Assume the smaller one, and flag."""
        specs = parse("ambiguous_5070")
        assert specs.gpu_model == "RTX 5070"
        assert specs.vram_gb == 8
        assert specs.vram_verified is False
        assert Flag.UNVERIFIED_VRAM in specs.flags

    def test_explicit_12gb_is_accepted_and_marked_priority(self):
        specs = parse("explicit_5070_12gb")
        assert specs.vram_gb == 12
        assert specs.vram_verified is True
        assert Flag.UNVERIFIED_VRAM not in specs.flags
        # New variant, no open-box price history — worth surfacing on its own.
        assert Flag.RTX_5070_12GB in specs.flags

    def test_bare_5070_does_not_borrow_system_ram_as_vram(self):
        """'RTX 5070 32GB 1TB' must not read 32 GB as video memory."""
        specs = parse("ambiguous_5070")
        assert specs.vram_gb == 8
        assert specs.system_ram_gb == 32


# ---------------------------------------------------------------------------
# Trap 2 — the laptop VRAM reference table
# ---------------------------------------------------------------------------


class TestVRAMReferenceTable:
    @pytest.mark.parametrize(
        "gpu,expected",
        [
            ("RTX 3060", 6),
            ("RTX 3070", 8),
            ("RTX 3070 Ti", 8),
            ("RTX 3080 Ti", 16),
            ("RTX 4060", 8),
            ("RTX 4070", 8),
            ("RTX 4080", 12),
            ("RTX 4090", 16),
            ("RTX 5050", 8),
            ("RTX 5060", 8),
            ("RTX 5070 Ti", 12),
            ("RTX 5080", 16),
            ("RTX 5090", 24),
        ],
    )
    def test_reference_vram(self, gpu, expected):
        assert hardware.vram_for(gpu) == expected

    @pytest.mark.parametrize("gpu", ["RTX 3080", "RTX 5070"])
    def test_ambiguous_models_have_no_fixed_capacity(self, gpu):
        assert hardware.vram_for(gpu) is None
        assert hardware.is_vram_ambiguous(gpu) is True

    def test_table_beats_a_number_in_the_title(self):
        """'RTX 4060 8GB 16GB 1TB' — the 4060 is always 8 GB. The 16 is RAM."""
        specs = parse("wuxga_trap")
        assert specs.vram_gb == 8
        assert specs.system_ram_gb == 16


# ---------------------------------------------------------------------------
# Trap 3 — never apply desktop specs to laptop parts
# ---------------------------------------------------------------------------


class TestDesktopSpecConfusion:
    def test_laptop_5070_ti_is_12gb_not_the_desktop_16gb(self):
        specs = parse("desktop_specs_quoted")
        assert specs.vram_gb == 12                          # laptop part
        assert Flag.DESKTOP_GPU_SUSPECTED in specs.flags    # listing said 16

    def test_laptop_3080_ti_is_16gb_desktop_is_12gb(self):
        assert hardware.LAPTOP_GPU_VRAM_GB["RTX 3080 Ti"] == 16
        assert hardware.DESKTOP_GPU_VRAM_GB["RTX 3080 Ti"] == 12

    def test_desktop_detector_ignores_matching_capacities(self):
        # The 4090's laptop and desktop parts differ (16 vs 24), so a stated
        # 16 GB is correct for the laptop and must not be flagged.
        assert hardware.looks_like_desktop_specs("RTX 4090", 16) is False
        assert hardware.looks_like_desktop_specs("RTX 4090", 24) is True


# ---------------------------------------------------------------------------
# Trap 4 — bandwidth table
# ---------------------------------------------------------------------------


class TestBandwidth:
    @pytest.mark.parametrize(
        "gpu,expected",
        [
            ("RTX 5060", 384),
            ("RTX 5070", 384),      # identical for both the 8 and 12 GB variants
            ("RTX 5070 Ti", 672),
            ("RTX 3070", 448),
            ("RTX 3080 Ti", 512),
        ],
    )
    def test_reference_bandwidth(self, gpu, expected):
        assert hardware.bandwidth_for(gpu) == expected

    def test_bandwidth_is_attached_during_parsing(self):
        specs = parse("helios_neo_16s_openbox")
        assert specs.memory_bandwidth_gbs == 672


# ---------------------------------------------------------------------------
# Trap 5 — the docking-station storage trap
# ---------------------------------------------------------------------------


class TestDockStorageTrap:
    def test_aggregate_headline_is_discarded(self):
        """'2TB Storage (1TB SSD & 1TB Docking Station)' is a 1 TB machine."""
        specs = parse("dock_storage_aggregate")
        assert specs.storage_gb == 1000
        assert specs.dock_storage_gb == 1000
        assert Flag.DOCK_STORAGE_TRAP in specs.flags

    def test_additive_form_is_discarded(self):
        """'1TB SSD + 1TB Dock Set' is also a 1 TB machine."""
        specs = parse("dock_storage_additive")
        assert specs.storage_gb == 1000
        assert Flag.DOCK_STORAGE_TRAP in specs.flags

    def test_genuine_2tb_is_not_penalised(self):
        specs = parse("desktop_specs_quoted")     # "... 2TB SSD", no dock
        assert specs.storage_gb == 2000
        assert Flag.DOCK_STORAGE_TRAP not in specs.flags

    def test_ram_is_not_mistaken_for_storage(self):
        specs = parse("legion_pro_5_oled")
        assert specs.storage_gb == 1000
        assert specs.system_ram_gb == 32


# ---------------------------------------------------------------------------
# Trap 6 — resolution naming
# ---------------------------------------------------------------------------


class TestResolutionNaming:
    @pytest.mark.parametrize(
        "title_key,expected_h",
        [
            ("helios_neo_16s_openbox", 1600),   # explicit 2560x1600
            ("legion_pro_5_oled", 1600),        # WQXGA
            ("msi_vector_16hx", 1600),          # QHD+
            ("legion_5_pro_16ach6h_used", 1600),
            ("panel_unstated", 1600),           # 2.5K
        ],
    )
    def test_passing_resolutions(self, title_key, expected_h):
        specs = parse(title_key)
        assert specs.resolution_h == expected_h
        assert specs.resolution_h >= 1440

    @pytest.mark.parametrize(
        "title_key,expected", [("wuxga_trap", 1200), ("fhd_trap", 1080)]
    )
    def test_failing_resolutions(self, title_key, expected):
        """WUXGA and FHD read as premium next to '16-inch gaming laptop'.
        They are below the 1440 vertical minimum and must not sneak through."""
        specs = parse(title_key)
        assert specs.resolution_h == expected
        assert specs.resolution_h < 1440

    def test_explicit_pixels_beat_a_marketing_alias(self):
        specs = parse("wuxga_trap")             # "WUXGA 1920x1200"
        assert (specs.resolution_w, specs.resolution_h) == (1920, 1200)


# ---------------------------------------------------------------------------
# Trap 7 — panel type is never inferred
# ---------------------------------------------------------------------------


class TestPanelType:
    def test_unstated_panel_is_unverified(self):
        specs = parse("panel_unstated")
        assert specs.panel_type == PanelType.UNVERIFIED
        assert Flag.UNVERIFIED_PANEL in specs.flags

    def test_legion_pro_5_16irx9_never_shipped_oled(self):
        """Gen 9 was IPS in every configuration. A listing claiming OLED is
        wrong, so we score it as IPS and flag it rather than believing it."""
        specs = parse("legion_pro_5_16irx9_fake_oled")
        assert specs.panel_type == PanelType.IPS
        assert Flag.UNVERIFIED_PANEL in specs.flags

    def test_genuine_oled_is_recognised(self):
        specs = parse("helios_neo_16s_openbox")
        assert specs.panel_type == PanelType.OLED

    def test_ips_is_recognised(self):
        specs = parse("msi_vector_16hx")
        assert specs.panel_type == PanelType.IPS

    def test_nits_are_extracted(self):
        assert parse("helios_neo_16s_openbox").nits == 500
        assert parse("legion_pro_5_oled").nits == 500
        assert parse("ambiguous_5070").nits == 400


# ---------------------------------------------------------------------------
# Trap 8 — single-channel RAM
# ---------------------------------------------------------------------------


class TestSingleChannel:
    def test_1x16gb_notation_is_detected(self):
        specs = parse("single_channel")
        assert specs.system_ram_gb == 16
        assert specs.single_channel is True
        assert Flag.SINGLE_CHANNEL_RAM in specs.flags

    def test_dual_channel_is_not_flagged(self):
        specs = parse("legion_pro_5_oled")
        assert specs.single_channel is False

    @pytest.mark.parametrize(
        "text",
        [
            "16GB single channel",
            "16GB (1x16GB) DDR5",
            "32GB RAM, one SODIMM occupied",
            "16GB single-channel memory",
        ],
    )
    def test_single_channel_phrasings(self, text):
        from dealhunter.parsing import detect_single_channel

        assert detect_single_channel(text) is True


# ---------------------------------------------------------------------------
# Other extraction
# ---------------------------------------------------------------------------


class TestGeneralExtraction:
    def test_tgp_from_at_notation(self):
        assert parse("legion_pro_5_oled").tgp_watts == 115       # "@115W"

    def test_tgp_from_explicit_label(self):
        assert parse("msi_vector_16hx").tgp_watts == 140         # "140W TGP"

    def test_tgp_adjacent_to_gpu(self):
        assert parse("helios_neo_16s_openbox").tgp_watts == 140  # "RTX 5070 Ti 12GB 140W"

    def test_missing_tgp_is_flagged_not_guessed(self):
        specs = parse("panel_unstated")
        assert specs.tgp_watts is None
        assert Flag.UNVERIFIED_TGP in specs.flags

    def test_free_m2_slot(self):
        assert parse("free_m2").free_m2_slot is True
        assert parse("legion_pro_5_oled").free_m2_slot is False

    def test_gpu_generation(self):
        assert parse("helios_neo_16s_openbox").gpu_generation == 50
        assert parse("wuxga_trap").gpu_generation == 40
        assert parse("legion_5_pro_16ach6h_used").gpu_generation == 30

    def test_pre_30_series_gpu_is_not_recognised(self):
        """We have no reference data for a 2070, so it must not be accepted
        into the pipeline with invented specs."""
        specs = parse("rtx_2070")
        assert specs.gpu_model is None

    def test_refresh_rate(self):
        assert parse("helios_neo_16s_openbox").refresh_hz == 240

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("MSI Vector 16 HX Ryzen 9 7945HX RTX 5070 Ti 12GB", "Ryzen 9 7945Hx"),
            ("Acer 16 Core Ultra 9 275HX RTX 5070 Ti 32GB", "Core Ultra 9 275Hx"),
            ("Dell G16 i7-13650HX RTX 4060 16GB", "I7-13650Hx"),
            # Names the GPU but not the CPU model — must not invent one.
            ("HP OMEN MAX 16 OLED Ryzen 9 RTX 5070 Ti 12GB 32GB", None),
        ],
    )
    def test_cpu_extraction(self, title, expected):
        assert parse_listing(title).cpu == expected

    def test_screen_size(self):
        assert parse("helios_neo_16s_openbox").screen_inches == 16.0
        assert parse("legion_5i_15").screen_inches == 15.1


class TestModelMatching:
    def test_priority_model_is_matched_and_flagged(self, config):
        specs = parse_listing(TITLES["helios_neo_16s_openbox"], config)
        assert specs.model_key == "acer_predator_helios_neo_16s_ai"
        assert Flag.PRIORITY_TARGET in specs.flags

    def test_sku_match(self, config):
        specs = parse_listing(TITLES["legion_pro_5_oled"], config)
        assert specs.model_key == "lenovo_legion_pro_5_16_83lt000mus"

    def test_unknown_model_matches_nothing(self, config):
        specs = parse_listing(TITLES["explicit_5070_12gb"], config)
        assert specs.model_key is None
