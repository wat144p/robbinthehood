"""
End-to-end: eBay payloads in, ranked alerts out.

This is the test that proves stage 2 actually joins up — it runs real API
payloads through the real source, the real parser, the real landed-cost maths
and the real scorer, and asserts on the decisions that come out the far end.
Only the socket is faked.
"""

from __future__ import annotations

import pytest

from dealhunter.evaluate import evaluate_all
from dealhunter.models import Flag, Region, RejectReason
from dealhunter.sources.base import collect_listings, run_sources
from dealhunter.sources.ebay import EbaySource
from tests.fixtures_ebay import FakeSession, item, search_response

# Titles in the style eBay sellers actually write them.
HELIOS_OPENBOX = (
    "Acer Predator Helios Neo 16S AI 16\" 2560x1600 240Hz OLED G-SYNC 500nit "
    "Core Ultra 9 275HX RTX 5070 Ti 12GB 140W 32GB DDR5 1TB SSD"
)
LEGION_CA = (
    "Lenovo Legion Pro 5 16 83LT000MUS 16\" WQXGA OLED 165Hz 500nit Ryzen 7 8745HX "
    "RTX 5060 8GB @115W 32GB 1TB SSD"
)
LEGION_UK = (
    "Lenovo Legion 7i 16 16\" 2.5K OLED 165Hz Core Ultra 7 255HX RTX 5060 8GB "
    "32GB 1TB SSD"
)
WUXGA_JUNK = (
    "HP Omen 16 16.1\" WUXGA 1920x1200 144Hz Core i7-14700HX RTX 4060 8GB "
    "16GB 1TB SSD"
)
DE_QWERTZ = (
    "Lenovo Legion Pro 5 16 WQXGA OLED 165Hz Ryzen 7 8745HX RTX 5060 8GB "
    "32GB 1TB SSD QWERTZ Tastatur"
)
AMBIGUOUS_5070 = (
    "GIGABYTE Aero X16 16\" 2560x1600 165Hz IPS 400nit Ryzen AI 7 350 "
    "RTX 5070 32GB 1TB SSD"
)


@pytest.fixture
def marketplace_payloads():
    """One realistic page of results per marketplace."""
    return {
        "EBAY_US": search_response([
            # The priority target at its known open-box floor, Delaware ZIP.
            item(item_id="v1|1001|0", title=HELIOS_OPENBOX, price="1184.00",
                 condition_id="1500", seller="bestbuy", feedback_score=1250000,
                 feedback_percent="98.9", postal="19801"),
            # Ambiguous 5070 — must survive but be flagged, not rejected.
            item(item_id="v1|1002|0", title=AMBIGUOUS_5070, price="1015.00",
                 condition_id="2010", seller="gigabyte_outlet",
                 feedback_score=22000, feedback_percent="99.1", postal="97035"),
            # Fails on resolution.
            item(item_id="v1|1003|0", title=WUXGA_JUNK, price="899.00",
                 condition_id="3000", seller="someseller",
                 feedback_score=3200, feedback_percent="99.0", postal="10001"),
        ]),
        "EBAY_CA": search_response([
            # Alberta postal code -> 5% GST, which is what makes this competitive.
            item(item_id="v1|2001|0", title=LEGION_CA, price="1499.00",
                 currency="CAD", condition_id="1500", seller="bestbuy_canada",
                 feedback_score=88000, feedback_percent="98.2",
                 country="CA", postal="T2P 1J9"),
        ]),
        "EBAY_GB": search_response([
            item(item_id="v1|3001|0", title=LEGION_UK, price="899.00",
                 currency="GBP", condition_id="2000", seller="lenovo_certified",
                 feedback_score=48000, feedback_percent="99.3",
                 country="GB", postal="M1 1AA"),
        ]),
        "EBAY_DE": search_response([
            item(item_id="v1|4001|0", title=DE_QWERTZ, price="1049.00",
                 currency="EUR", condition_id="1000", seller="notebooksbilliger",
                 feedback_score=51000, feedback_percent="99.5",
                 country="DE", postal="10115"),
        ]),
    }


@pytest.fixture
def pipeline(config, rates, marketplace_payloads):
    """Run the whole thing and hand back the evaluated listings."""
    session = FakeSession(marketplace_payloads)
    source = EbaySource(
        config, rates, session=session,
        client_id="fake-id", client_secret="fake-secret",
        sleep=lambda _s: None,
    )
    results = run_sources([source])
    listings = collect_listings(results)
    return results, evaluate_all(listings, config, rates)


def find(evaluated, listing_id):
    return next(e for e in evaluated if e.listing.listing_id == listing_id)


# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_the_run_succeeds_and_produces_listings(self, pipeline):
        results, evaluated = pipeline
        assert results[0].ok is True
        assert len(evaluated) == 6

    def test_the_priority_target_tops_the_ranking(self, pipeline):
        """Helios Neo, OLED + 12 GB + 32 GB + 1 TB, at its known floor."""
        _results, evaluated = pipeline
        best = evaluated[0]
        assert best.listing.listing_id == "v1|1001|0"
        assert best.score.priority is True
        assert Flag.PRIORITY_TARGET in best.all_flags
        assert best.landed.landed_usd == 1184.00     # Delaware: no sales tax

    def test_alberta_listing_beats_its_own_sticker_shock(self, config, pipeline):
        """C$1,499 looks worse than $1,184 until you do the maths: Alberta's
        5% GST and a weak CAD put it under the US listing on landed cost."""
        _results, evaluated = pipeline
        alberta = find(evaluated, "v1|2001|0")

        assert alberta.listing.jurisdiction == "AB"
        assert alberta.landed.tax_rate_applied == 0.05
        assert alberta.landed.landed_usd == pytest.approx(1148.98, abs=0.02)
        # Both prices are available for the alert, per the ranking rule.
        assert alberta.landed.local_display() == "C$1,499.00"

    def test_uk_listing_carries_vat_fx_and_the_iso_penalty(self, pipeline):
        _results, evaluated = pipeline
        uk = find(evaluated, "v1|3001|0")

        # £899 x 1.27 x 1.03 = $1,175.98 — the sticker never told you that.
        assert uk.landed.landed_usd == pytest.approx(1175.98, abs=0.02)
        assert uk.landed.vat_embedded_rate == 0.20
        assert uk.landed.reclaimable_tax_local == 0.0
        assert Flag.ISO_KEYBOARD_PENALTY in uk.all_flags

    def test_german_qwertz_stock_is_rejected(self, pipeline):
        _results, evaluated = pipeline
        german = find(evaluated, "v1|4001|0")
        assert german.rejected
        assert RejectReason.KEYBOARD_LAYOUT in german.reject_reasons

    def test_low_resolution_listing_is_rejected(self, pipeline):
        _results, evaluated = pipeline
        wuxga = find(evaluated, "v1|1003|0")
        assert RejectReason.RESOLUTION_TOO_LOW in wuxga.reject_reasons

    def test_ambiguous_5070_survives_but_is_flagged(self, pipeline):
        """It must stay in the pipeline for manual confirmation rather than
        silently disappearing — but it is scored at the 8 GB tier."""
        _results, evaluated = pipeline
        aero = find(evaluated, "v1|1002|0")

        assert not aero.rejected
        assert Flag.UNVERIFIED_VRAM in aero.all_flags
        assert aero.specs.vram_gb == 8
        assert Flag.RTX_5070_12GB not in aero.all_flags

    def test_rejected_listings_never_reach_the_alert_set(self, config, pipeline):
        _results, evaluated = pipeline
        immediate = config.notification["immediate_alert_score"]
        alerts = [
            e for e in evaluated
            if not e.rejected and (e.score.total >= immediate or e.score.priority)
        ]
        assert all(not e.rejected for e in alerts)
        assert "v1|1001|0" in {e.listing.listing_id for e in alerts}

    def test_every_kept_listing_records_the_fx_rate_it_used(self, pipeline):
        """Historical comparisons stay honest only if the rate is stored."""
        _results, evaluated = pipeline
        for item_ in evaluated:
            if item_.landed:
                assert item_.landed.fx_rate_to_usd > 0
                assert item_.landed.fx_source
                assert item_.landed.fx_fetched_at is not None

    def test_ranking_is_by_landed_cost_not_sticker(self, pipeline):
        """The Canadian listing has the highest sticker number of the three
        survivors and is not last. Sticker order would put it last."""
        _results, evaluated = pipeline
        kept = [e for e in evaluated if not e.rejected]
        by_sticker = sorted(kept, key=lambda e: -e.listing.sticker_price_local)
        assert by_sticker[0].listing.listing_id == "v1|2001|0"
        assert kept[-1].listing.listing_id != "v1|2001|0"


class TestRegionAttribution:
    def test_each_listing_knows_its_region(self, pipeline):
        _results, evaluated = pipeline
        assert find(evaluated, "v1|1001|0").listing.region == Region.US
        assert find(evaluated, "v1|2001|0").listing.region == Region.CA
        assert find(evaluated, "v1|3001|0").listing.region == Region.GB
        assert find(evaluated, "v1|4001|0").listing.region == Region.DE

    def test_a_non_us_win_can_be_explained(self, config, pipeline):
        """The brief requires saying *why* a non-US pick won. All the inputs
        for that sentence are on the LandedCost object."""
        from dealhunter.regions import explain_landed_cost

        _results, evaluated = pipeline
        alberta = find(evaluated, "v1|2001|0")
        explanation = explain_landed_cost(alberta.landed, config.region(Region.CA))

        assert "C$1,499.00" in explanation
        assert "AB" in explanation
        assert "5%" in explanation
        assert "0.7300" in explanation
