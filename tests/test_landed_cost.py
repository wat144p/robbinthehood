"""
Landed-cost normalisation across all seven regions.

This is the file to look at first if a ranking ever surprises you. The central
claim it defends: **a VAT-inclusive European sticker is not comparable to a
US sticker of the same number**, and a raw FX conversion will lie about it.
"""

from __future__ import annotations

import pytest

from dealhunter.models import Region
from dealhunter.regions import compute_landed_cost
from tests.conftest import TEST_FX
from tests.fixtures import make_listing


def landed(config, rates, **kwargs) -> float:
    listing = make_listing("legion_pro_5_oled", **kwargs)
    return compute_landed_cost(listing, config, rates).landed_usd


# ---------------------------------------------------------------------------
# The headline assertion
# ---------------------------------------------------------------------------


class TestVATInclusiveComparisons:
    def test_uk_1150_does_not_outrank_us_1150(self, config, rates):
        """A £1,150 UK listing and a $1,150 US listing are not the same deal.

        The UK sticker already contains 20% VAT that is not reclaimable, the
        pound is worth more than the dollar, and the UK carries a 3% risk
        premium. All three push the same number materially higher in landed
        terms — and none of them show on the price tag.
        """
        uk = landed(config, rates, region=Region.GB, price=1150.0)
        us = landed(config, rates, region=Region.US, price=1150.0, jurisdiction="DE")

        assert uk > us, "VAT-inclusive UK sticker must not look cheaper than a US one"

        # £1,150 x 1.27 x 1.03 = $1,504.36
        assert uk == pytest.approx(1504.36, abs=0.05)
        # $1,150 in Delaware: no sales tax, no risk premium.
        assert us == pytest.approx(1150.00, abs=0.01)

        # And the gap is large enough that it changes the decision entirely:
        # the UK listing is over the $1,400 ceiling, the US one is not.
        assert uk > config.budget["hard_ceiling_usd"]
        assert us < config.budget["hard_ceiling_usd"]

    def test_vat_is_never_stripped_from_the_sticker(self, config, rates):
        """We do not divide European prices by 1.20 to make them competitive.

        The contact receives the goods as an ordinary domestic consumer, so the
        VAT is simply part of the price. Reclaimable tax is zero everywhere.
        """
        cost = compute_landed_cost(
            make_listing("legion_pro_5_oled", region=Region.DE, price=1299.0),
            config,
            rates,
        )
        assert cost.reclaimable_tax_local == 0.0
        # Total before FX equals the sticker exactly — nothing added, nothing
        # taken away, because EU tax is already inside it.
        assert cost.total_local == 1299.0
        # The embedded rate is recorded for the alert, but never subtracted.
        assert cost.vat_embedded_rate == 0.19

    def test_naive_fx_conversion_would_have_been_wrong(self, config, rates):
        """Sanity check on the thing we are guarding against: a plain FX
        conversion understates the German listing by the risk premium alone."""
        naive = 1299.0 * TEST_FX["EUR"]
        real = landed(config, rates, region=Region.DE, price=1299.0)
        assert real > naive
        assert real == pytest.approx(naive * 1.05, abs=0.05)


# ---------------------------------------------------------------------------
# Per-region arithmetic
# ---------------------------------------------------------------------------


class TestPerRegionLandedCost:
    def test_us_no_tax_state(self, config, rates):
        """Delaware, Montana, New Hampshire and Oregon add nothing."""
        assert landed(config, rates, region=Region.US, price=1200.0,
                      jurisdiction="OR") == pytest.approx(1200.00)

    def test_us_taxed_state(self, config, rates):
        # $1,200 x 1.0725 (California) x 1.00 risk
        assert landed(config, rates, region=Region.US, price=1200.0,
                      jurisdiction="CA") == pytest.approx(1287.00, abs=0.01)

    def test_us_unknown_state_uses_pessimistic_fallback(self, config, rates):
        cost = compute_landed_cost(
            make_listing("legion_pro_5_oled", region=Region.US, price=1200.0),
            config, rates,
        )
        assert cost.tax_rate_assumed is True
        assert cost.tax_rate_applied == pytest.approx(0.07)
        assert cost.landed_usd == pytest.approx(1284.00, abs=0.01)

    def test_canada_alberta_versus_ontario(self, config, rates):
        """The province is an 8-point swing, so it has to be surfaced.

        Same C$1,499 machine: 5% GST in Alberta, 13% HST in Ontario.
        """
        alberta = landed(config, rates, region=Region.CA, price=1499.0,
                         jurisdiction="AB")
        ontario = landed(config, rates, region=Region.CA, price=1499.0,
                         jurisdiction="ON")

        # C$1,499 x 1.05 x 0.73 = $1,148.98
        assert alberta == pytest.approx(1148.98, abs=0.02)
        # C$1,499 x 1.13 x 0.73 = $1,236.53
        assert ontario == pytest.approx(1236.53, abs=0.02)
        # ~$88 of pure tax jurisdiction. This is why the province is surfaced
        # explicitly in every Canadian alert.
        assert ontario - alberta == pytest.approx(87.55, abs=0.05)

    def test_canada_has_no_risk_premium(self, config, rates):
        """Canada is co-primary with the US, not a foreign-risk region."""
        assert config.region(Region.CA).risk_premium == 0.0

    def test_uk_risk_premium(self, config, rates):
        # £1,000 x 1.27 x 1.03
        assert landed(config, rates, region=Region.GB,
                      price=1000.0) == pytest.approx(1308.10, abs=0.02)

    def test_germany(self, config, rates):
        # €1,000 x 1.09 x 1.05
        assert landed(config, rates, region=Region.DE,
                      price=1000.0) == pytest.approx(1144.50, abs=0.02)

    def test_belgium_matches_germany_on_price_but_not_on_vat(self, config, rates):
        """Same currency, same risk premium — the 21% vs 19% VAT difference is
        inside the sticker, so identical stickers land identically. The VAT gap
        shows up as Belgian retailers quoting higher numbers, not as a
        correction we apply."""
        de = landed(config, rates, region=Region.DE, price=1000.0)
        be = landed(config, rates, region=Region.BE, price=1000.0)
        assert de == pytest.approx(be)
        assert config.region(Region.BE).vat_in_sticker > config.region(Region.DE).vat_in_sticker

    def test_sweden(self, config, rates):
        # 12,000 SEK x 0.095 x 1.05
        assert landed(config, rates, region=Region.SE,
                      price=12000.0) == pytest.approx(1197.00, abs=0.02)

    def test_australia(self, config, rates):
        # A$1,800 x 0.66 x 1.05
        assert landed(config, rates, region=Region.AU,
                      price=1800.0) == pytest.approx(1247.40, abs=0.02)

    @pytest.mark.parametrize("region", list(Region))
    def test_every_region_produces_a_landed_figure(self, config, rates, region):
        cost = compute_landed_cost(
            make_listing("legion_pro_5_oled", region=region, price=1000.0),
            config, rates,
        )
        assert cost.landed_usd > 0
        assert cost.reclaimable_tax_local == 0.0


# ---------------------------------------------------------------------------
# Shipping, provenance, and auditability
# ---------------------------------------------------------------------------


class TestLandedCostMechanics:
    def test_domestic_shipping_is_included_and_taxed(self, config, rates):
        """Both the US and Canada assess sales tax on shipping."""
        cost = compute_landed_cost(
            make_listing("legion_pro_5_oled", region=Region.US, price=1000.0,
                         shipping=50.0, jurisdiction="CA"),
            config, rates,
        )
        assert cost.total_local == pytest.approx(1050.0 * 1.0725, abs=0.01)

    def test_fx_rate_is_recorded_on_every_listing(self, config, rates):
        """Historical comparisons stay honest only if we know the rate used."""
        cost = compute_landed_cost(
            make_listing("legion_pro_5_oled", region=Region.GB, price=1000.0),
            config, rates,
        )
        assert cost.fx_rate_to_usd == TEST_FX["GBP"]
        assert cost.fx_source == "test-fixture"
        assert cost.fx_fetched_at is not None

    def test_both_prices_are_available_for_display(self, config, rates):
        """The ranking rule: never show a converted number without the original."""
        cost = compute_landed_cost(
            make_listing("legion_pro_5_oled", region=Region.GB, price=1149.99),
            config, rates,
        )
        assert cost.local_display() == "£1,149.99"
        assert cost.usd_display().startswith("$1,")

    def test_currency_region_mismatch_is_a_loud_error(self, config, rates):
        """A source module reporting a converted price would silently break the
        region's tax rules, so it fails rather than producing a wrong number."""
        listing = make_listing("legion_pro_5_oled", region=Region.GB,
                               currency="USD", price=1150.0)
        with pytest.raises(ValueError, match="configured for GBP"):
            compute_landed_cost(listing, config, rates)

    def test_risk_premiums_match_the_brief(self, config):
        expected = {
            Region.US: 0.00, Region.CA: 0.00, Region.GB: 0.03,
            Region.DE: 0.05, Region.BE: 0.05, Region.SE: 0.05, Region.AU: 0.05,
        }
        for region, premium in expected.items():
            assert config.region(region).risk_premium == premium
