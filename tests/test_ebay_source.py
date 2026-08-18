"""
eBay source: query construction, response mapping, and failure handling.

No network. Every test drives the real `EbaySource` through a fake session, so
the OAuth flow, the filter string, the pagination logic and the mapping are all
genuinely exercised — only the socket is faked.
"""

from __future__ import annotations

import pytest

from dealhunter.models import Condition, Region
from dealhunter.sources.base import SourceAuthError, SourceBlocked, run_sources
from dealhunter.sources.ebay import EbaySource
from tests.fixtures_ebay import FakeSession, item, item_detail, search_response

HELIOS = (
    "Acer Predator Helios Neo 16S AI 16\" 2560x1600 240Hz OLED Core Ultra 9 275HX "
    "RTX 5070 Ti 12GB 140W 32GB 1TB SSD"
)
LEGION = (
    "Lenovo Legion Pro 5 16 83LT000MUS WQXGA OLED 165Hz Ryzen 7 8745HX "
    "RTX 5060 8GB @115W 32GB 1TB SSD"
)


def make_source(config, rates, session, **kwargs) -> EbaySource:
    """An EbaySource with credentials injected and sleeping disabled."""
    return EbaySource(
        config, rates, session=session,
        client_id="fake-id", client_secret="fake-secret",
        sleep=lambda _seconds: None,
        **kwargs,
    )


@pytest.fixture
def single_query_config(config, monkeypatch):
    """Trim to one query and no storefronts, so request-count assertions stay
    readable. The storefront pass has its own tests below."""
    ebay = config.raw_sources["ebay"]
    original_queries = ebay.get("queries")
    original_storefronts = ebay.get("storefronts")
    ebay["queries"] = ["gaming laptop RTX 5070 Ti 32GB"]
    ebay["storefronts"] = {"enabled": False}
    yield config
    ebay["queries"] = original_queries
    ebay["storefronts"] = original_storefronts


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_credentials_raise_a_clear_error(self, config, rates, monkeypatch):
        monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
        monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
        source = EbaySource(config, rates, session=FakeSession())

        with pytest.raises(SourceAuthError, match="EBAY_CLIENT_ID"):
            source.fetch()

    def test_credentials_come_from_the_environment(self, config, rates, monkeypatch):
        monkeypatch.setenv("EBAY_CLIENT_ID", "env-id")
        monkeypatch.setenv("EBAY_CLIENT_SECRET", "env-secret")
        source = EbaySource(config, rates, session=FakeSession())
        assert source.client_id == "env-id"
        assert source.client_secret == "env-secret"

    def test_rejected_credentials_surface_as_an_auth_error(
        self, single_query_config, rates
    ):
        session = FakeSession(oauth_status=401)
        source = make_source(single_query_config, rates, session)

        with pytest.raises(SourceAuthError, match="rejected the credentials"):
            source.fetch()

    def test_token_is_fetched_once_and_reused(self, single_query_config, rates):
        session = FakeSession({"EBAY_US": search_response([item()])})
        source = make_source(single_query_config, rates, session)
        source.fetch()

        oauth_calls = [c for c in session.calls if c["method"] == "POST"]
        assert len(oauth_calls) == 1, "the application token should be cached"


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


class TestQueryConstruction:
    def test_domestic_shipping_is_enforced_in_the_filter(
        self, single_query_config, rates
    ):
        """We never want a seller who has to ship internationally — the
        forwarding contact receives the machine domestically."""
        session = FakeSession()
        make_source(single_query_config, rates, session).fetch()

        us_filter = session.filters_for("EBAY_US")[0]
        assert "itemLocationCountry:US" in us_filter
        assert "deliveryCountry:US" in us_filter

        gb_filter = session.filters_for("EBAY_GB")[0]
        assert "itemLocationCountry:GB" in gb_filter
        assert "deliveryCountry:GB" in gb_filter

    def test_each_marketplace_uses_its_own_currency(self, single_query_config, rates):
        session = FakeSession()
        make_source(single_query_config, rates, session).fetch()

        assert "priceCurrency:USD" in session.filters_for("EBAY_US")[0]
        assert "priceCurrency:CAD" in session.filters_for("EBAY_CA")[0]
        assert "priceCurrency:GBP" in session.filters_for("EBAY_GB")[0]
        assert "priceCurrency:EUR" in session.filters_for("EBAY_DE")[0]
        assert "priceCurrency:AUD" in session.filters_for("EBAY_AU")[0]

    def test_price_ceiling_is_derived_from_the_landed_budget(
        self, single_query_config, rates
    ):
        """The search ceiling is worked backwards from $1,400 landed, so it
        moves with FX instead of being a hardcoded per-currency guess.

        GBP: 1400 * 1.05 / (1.27 * 1.03) = ~1124
        CAD: 1400 * 1.05 / (0.73 * 1.00) = ~2014
        """
        source = make_source(single_query_config, rates, FakeSession())

        _low_gb, high_gb = source._price_bounds_local(Region.GB, "GBP")
        _low_ca, high_ca = source._price_bounds_local(Region.CA, "CAD")
        _low_us, high_us = source._price_bounds_local(Region.US, "USD")

        assert high_gb == pytest.approx(1123.9, abs=1.0)
        assert high_ca == pytest.approx(2013.7, abs=1.0)
        assert high_us == pytest.approx(1470.0, abs=1.0)

        # The weak-currency region must search a wider local range than the US,
        # or Canadian deals would never appear at all.
        assert high_ca > high_us > high_gb

    def test_fixed_price_only(self, single_query_config, rates):
        """Auctions have no settled price to score."""
        session = FakeSession()
        make_source(single_query_config, rates, session).fetch()
        assert "buyingOptions:{FIXED_PRICE}" in session.filters_for("EBAY_US")[0]

    def test_marketplace_header_is_set_per_request(self, single_query_config, rates):
        session = FakeSession()
        make_source(single_query_config, rates, session).fetch()

        marketplaces = {
            call["headers"]["X-EBAY-C-MARKETPLACE-ID"] for call in session.searches
        }
        assert marketplaces == {"EBAY_US", "EBAY_CA", "EBAY_GB", "EBAY_DE", "EBAY_AU"}

    def test_disabled_region_is_not_queried(self, config, rates, monkeypatch):
        config.regions[Region.AU].enabled = False
        try:
            session = FakeSession()
            make_source(config, rates, session).fetch()
            marketplaces = {
                call["headers"]["X-EBAY-C-MARKETPLACE-ID"] for call in session.searches
            }
            assert "EBAY_AU" not in marketplaces
        finally:
            config.regions[Region.AU].enabled = True


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------


class TestListingMapping:
    def test_basic_fields(self, single_query_config, rates):
        session = FakeSession({
            "EBAY_US": search_response([
                item(item_id="v1|111|0", title=HELIOS, price="1184.00",
                     condition_id="1500", seller="bestbuy", postal="19801")
            ])
        })
        listings = make_source(single_query_config, rates, session).fetch()
        listing = next(l for l in listings if l.region == Region.US)

        assert listing.source == "ebay"
        assert listing.listing_id == "v1|111|0"
        assert listing.title == HELIOS
        assert listing.currency == "USD"
        assert listing.sticker_price_local == 1184.00
        assert listing.condition == Condition.OPEN_BOX_GOOD
        assert listing.url.startswith("https://www.ebay.com/itm/")

    def test_fingerprint_is_stable_and_source_prefixed(self, single_query_config, rates):
        session = FakeSession({"EBAY_US": search_response([item(item_id="v1|999|0")])})
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.fingerprint() == "ebay:v1|999|0"

    @pytest.mark.parametrize(
        "condition_id,expected",
        [
            ("1000", Condition.NEW),
            ("1500", Condition.OPEN_BOX_GOOD),
            ("2000", Condition.MFR_CERTIFIED_REFURB),
            ("2010", Condition.EBAY_REFURBISHED),
            ("2020", Condition.EBAY_REFURBISHED),
            ("2030", Condition.EBAY_REFURBISHED),
            ("3000", Condition.USED),
        ],
    )
    def test_condition_ids(self, single_query_config, rates, condition_id, expected):
        session = FakeSession({
            "EBAY_US": search_response([
                item(condition_id=condition_id, seller="random_seller")
            ])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.condition == expected

    def test_open_box_is_not_assumed_to_be_excellent(self, single_query_config, rates):
        """eBay open box has no grade, unlike Best Buy's tiers. Assuming the
        best case is how you end up disappointed on arrival."""
        session = FakeSession({
            "EBAY_US": search_response([item(condition_id="1500", seller="anyone")])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.condition == Condition.OPEN_BOX_GOOD
        assert listing.condition != Condition.OPEN_BOX_EXCELLENT

    def test_manufacturer_storefront_is_promoted_to_certified_refurb(
        self, single_query_config, rates
    ):
        """eBay tags Acer's own outlet as plain seller-refurbished, but those
        units carry a real manufacturer warranty."""
        session = FakeSession({
            "EBAY_US": search_response([
                item(condition_id="2500", seller="acer_recertified")
            ])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.condition == Condition.MFR_CERTIFIED_REFURB

    def test_seller_feedback_is_coerced_from_strings(self, single_query_config, rates):
        session = FakeSession({
            "EBAY_US": search_response([
                item(feedback_score=25314, feedback_percent="99.4")
            ])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.seller_feedback_count == 25314
        assert listing.seller_feedback_percent == pytest.approx(99.4)

    def test_missing_feedback_becomes_none_not_zero(self, single_query_config, rates):
        """Zero feedback and unknown feedback score very differently, so the
        distinction has to survive the mapping."""
        payload = item()
        payload["seller"].pop("feedbackScore")
        payload["seller"].pop("feedbackPercentage")
        session = FakeSession({"EBAY_US": search_response([payload])})

        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.seller_feedback_count is None
        assert listing.seller_feedback_percent is None

    def test_shipping_cost_is_captured(self, single_query_config, rates):
        session = FakeSession({
            "EBAY_US": search_response([item(shipping="24.99")])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.domestic_shipping_local == 24.99

    def test_pickup_only_listings_are_marked_undeliverable(
        self, single_query_config, rates
    ):
        """The forwarding contact isn't driving to a warehouse."""
        session = FakeSession({
            "EBAY_US": search_response([item(pickup_only=True)])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.ships_domestically is False

    def test_currency_mismatch_is_dropped_rather_than_converted(
        self, single_query_config, rates
    ):
        """A EUR price on the US marketplace would silently get US tax rules
        applied to it, so it is discarded instead."""
        session = FakeSession({
            "EBAY_US": search_response([item(price="1100.00", currency="EUR")])
        })
        listings = make_source(single_query_config, rates, session).fetch()
        assert listings == []

    def test_unpriced_items_are_skipped(self, single_query_config, rates):
        payload = item()
        payload.pop("price")
        session = FakeSession({"EBAY_US": search_response([payload])})
        assert make_source(single_query_config, rates, session).fetch() == []

    def test_warranty_note_is_populated(self, single_query_config, rates):
        session = FakeSession({"EBAY_GB": search_response([item(
            price="899.00", currency="GBP", country="GB", postal=None
        )])})
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert "Pakistan" in listing.warranty_note


# ---------------------------------------------------------------------------
# Jurisdiction inference
# ---------------------------------------------------------------------------


class TestJurisdiction:
    @pytest.mark.parametrize(
        "postal,expected",
        [("T2P 1J9", "AB"), ("M5V 3L9", "ON"), ("V6B 1A1", "BC"), ("H3B 2Y5", "QC")],
    )
    def test_canadian_province_from_postal_code(
        self, single_query_config, rates, postal, expected
    ):
        """Alberta is worth 8 points of landed cost over Ontario, so this is
        worth getting right."""
        session = FakeSession({
            "EBAY_CA": search_response([
                item(price="1499.00", currency="CAD", country="CA", postal=postal)
            ])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.jurisdiction == expected

    @pytest.mark.parametrize(
        "postal,expected",
        [("19801", "DE"), ("59001", "MT"), ("03301", "NH"), ("97035", "OR")],
    )
    def test_us_zero_tax_states_are_identified(
        self, single_query_config, rates, postal, expected
    ):
        session = FakeSession({
            "EBAY_US": search_response([item(postal=postal)])
        })
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.jurisdiction == expected

    def test_other_us_states_fall_back_to_the_pessimistic_rate(
        self, single_query_config, rates
    ):
        """We don't ship a full ZIP table: getting a taxed state slightly wrong
        changes nothing, but wrongly claiming 0% would understate the price."""
        session = FakeSession({"EBAY_US": search_response([item(postal="10001")])})
        listing = make_source(single_query_config, rates, session).fetch()[0]
        assert listing.jurisdiction is None


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_403_blocks_the_source_instead_of_retrying(
        self, single_query_config, rates
    ):
        """Never retry into a ban."""
        session = FakeSession(search_status=403)
        source = make_source(single_query_config, rates, session)

        with pytest.raises(SourceBlocked):
            source.fetch()

        assert len(session.searches) == 1, "a 403 must not be retried"

    def test_429_is_retried_with_backoff(self, single_query_config, rates):
        session = FakeSession(search_status=429)
        source = make_source(single_query_config, rates, session)
        source.fetch()   # errors are swallowed per-query, not raised

        # max_retries is 3 in config, across five marketplaces.
        assert len(session.searches) == 3 * 5

    def test_a_blocked_source_does_not_kill_the_run(self, single_query_config, rates):
        session = FakeSession(search_status=403)
        source = make_source(single_query_config, rates, session)

        results = run_sources([source])
        assert results[0].blocked is True
        assert results[0].listings == []
        assert "403" in results[0].errors[0]
        assert "BLOCKED" in results[0].summary()

    def test_runner_reports_but_survives_an_auth_failure(
        self, single_query_config, rates
    ):
        session = FakeSession(oauth_status=401)
        results = run_sources([make_source(single_query_config, rates, session)])
        assert results[0].ok is False
        assert "auth" in results[0].errors[0]

    def test_disabled_source_is_skipped(self, config, rates):
        config.raw_sources["ebay"]["enabled"] = False
        try:
            results = run_sources([make_source(config, rates, FakeSession())])
            assert results[0].skipped_reason == "disabled in config"
        finally:
            config.raw_sources["ebay"]["enabled"] = True


# ---------------------------------------------------------------------------
# Deduplication within a run
# ---------------------------------------------------------------------------


def test_the_same_item_matching_several_queries_appears_once(
    single_query_config, rates
):
    """Our queries deliberately overlap, so the same item comes back repeatedly."""
    single_query_config.raw_sources["ebay"]["queries"] = [
        "gaming laptop RTX 5070 Ti", "gaming laptop OLED 32GB",
    ]
    session = FakeSession({"EBAY_US": search_response([item(item_id="v1|777|0")])})
    listings = make_source(single_query_config, rates, session).fetch()

    us_listings = [l for l in listings if l.listing_id == "v1|777|0"]
    assert len(us_listings) == 1
    # ...but it was genuinely searched for once per configured query.
    assert len(session.filters_for("EBAY_US")) == len(
        single_query_config.raw_sources["ebay"]["queries"]
    )


# ---------------------------------------------------------------------------
# Storefront queries
# ---------------------------------------------------------------------------


class TestStorefronts:
    """Best Buy, Acer and Lenovo sell open-box stock through eBay. Querying
    those storefronts directly is also the workaround for Best Buy's own API,
    whose signup requires a US/Canada phone number."""

    @pytest.fixture
    def storefront_config(self, config):
        ebay = config.raw_sources["ebay"]
        original_q, original_s = ebay.get("queries"), ebay.get("storefronts")
        ebay["queries"] = ["gaming laptop RTX 5070 Ti 32GB"]
        ebay["storefronts"] = {
            "enabled": True,
            "sellers": ["bestbuy", "acer"],
            "queries": ["gaming laptop"],
        }
        yield config
        ebay["queries"], ebay["storefronts"] = original_q, original_s

    def test_each_storefront_is_queried_with_a_sellers_filter(
        self, storefront_config, rates
    ):
        session = FakeSession()
        make_source(storefront_config, rates, session).fetch()

        filters = session.filters_for("EBAY_US")
        assert any("sellers:{bestbuy}" in f for f in filters)
        assert any("sellers:{acer}" in f for f in filters)

    def test_storefronts_are_queried_separately_not_or_ed_together(
        self, storefront_config, rates
    ):
        """One combined `sellers:{a|b}` query would make a zero result
        impossible to attribute to a specific mistyped username."""
        session = FakeSession()
        make_source(storefront_config, rates, session).fetch()

        assert not any(
            "sellers:{bestbuy|acer}" in f for f in session.filters_for("EBAY_US")
        )

    def test_storefront_listings_are_returned(self, storefront_config, rates):
        session = FakeSession({
            "EBAY_US": search_response([
                item(item_id="v1|store|0", title=HELIOS, price="1184.00",
                     condition_id="1500", seller="bestbuy", postal="19801")
            ])
        })
        listings = make_source(storefront_config, rates, session).fetch()
        assert any(l.listing_id == "v1|store|0" for l in listings)

    def test_a_storefront_that_finds_nothing_is_reported(
        self, storefront_config, rates, caplog
    ):
        """A mistyped username would otherwise look exactly like a quiet day.
        The usernames could not be verified before a live key exists, so this
        warning is the only thing standing between a typo and silent nothing."""
        import logging

        session = FakeSession()   # every marketplace returns nothing
        with caplog.at_level(logging.WARNING):
            make_source(storefront_config, rates, session).fetch()

        warnings = " ".join(r.getMessage() for r in caplog.records)
        assert "bestbuy" in warnings
        assert "username is probably wrong" in warnings

    def test_a_storefront_with_results_is_not_warned_about(
        self, storefront_config, rates, caplog
    ):
        """Each storefront must get its OWN genuine result to avoid the
        warning — a fixture that only ever returns "bestbuy"-seller items
        would previously have let a mistyped "acer" pass silently, because
        the old code counted any item as a hit regardless of whose it was.
        That is exactly the bug the seller-identity check above closes."""
        import logging

        session = FakeSession({
            "EBAY_US": search_response([
                item(item_id="v1|bb|0", seller="bestbuy"),
                item(item_id="v1|ac|0", seller="acer"),
            ])
        })
        source = make_source(storefront_config, rates, session)
        with caplog.at_level(logging.WARNING):
            source.fetch()

        assert source.storefront_hits["bestbuy"] > 0
        assert source.storefront_hits["acer"] > 0
        assert not [
            r for r in caplog.records if "probably wrong" in r.getMessage()
        ]

    def test_an_item_from_the_wrong_seller_is_dropped_not_attributed(
        self, storefront_config, rates, caplog
    ):
        """eBay's sellers filter has been observed live to be silently
        ignored for an unrecognised name, returning the whole unfiltered
        category instead of an error. Trusting it blindly would let a wrong
        storefront name launder a random seller's stock as e.g. "Best Buy"
        and hand it the major-retailer trust bonus, so every item is checked
        against the seller actually requested."""
        import logging

        session = FakeSession({
            # Every query — including both storefront passes — gets back an
            # item from a THIRD, unrelated seller. This is what the silent
            # filter-ignore bug looks like in practice.
            "EBAY_US": search_response([
                item(item_id="v1|other|0", seller="some_random_reseller")
            ])
        })
        source = make_source(storefront_config, rates, session)
        with caplog.at_level(logging.WARNING):
            listings = source.fetch()

        # The mismatched item must not be attributed to either storefront...
        assert source.storefront_hits["bestbuy"] == 0
        assert source.storefront_hits["acer"] == 0
        assert not any(l.seller_name == "some_random_reseller" and
                       l.is_major_retailer for l in listings)

        # ...and both should be reported as not actually found.
        warnings = [r.getMessage() for r in caplog.records]
        assert any("dropped" in w and "different seller" in w for w in warnings)
        assert any("bestbuy" in w and "probably wrong" in w for w in warnings)
        assert any("acer" in w and "probably wrong" in w for w in warnings)

    def test_disabling_storefronts_skips_the_pass(self, storefront_config, rates):
        storefront_config.raw_sources["ebay"]["storefronts"]["enabled"] = False
        session = FakeSession()
        make_source(storefront_config, rates, session).fetch()

        assert not any("sellers:" in f for f in session.filters_for("EBAY_US"))

    def test_storefront_items_are_not_duplicated_from_the_keyword_pass(
        self, storefront_config, rates
    ):
        """The same Best Buy item matches both a keyword query and its
        storefront query."""
        session = FakeSession({
            "EBAY_US": search_response([item(item_id="v1|dup|0", seller="bestbuy")])
        })
        listings = make_source(storefront_config, rates, session).fetch()
        assert [l.listing_id for l in listings].count("v1|dup|0") == 1


# ---------------------------------------------------------------------------
# Truncated-title enrichment
# ---------------------------------------------------------------------------


TRUNCATED_HP = (
    "HP - Victus 15.6\" 144Hz Full HD Gaming Laptop - Intel Core i5 13420H (2023) -..."
)
assert len(TRUNCATED_HP) >= 79 and TRUNCATED_HP.endswith("...")


class TestTruncatedTitleEnrichment:
    """eBay cuts a real, meaningful share of listing titles off at 80
    characters, mid-word, before the spec that would otherwise have parsed --
    verified live on 2026-08-18 against roughly 60% of that day's otherwise-
    unparseable listings. This reuses the ordinary text parser rather than
    mapping eBay's aspect names to spec fields directly, by folding the full
    title plus item specifics into the listing's description."""

    def test_a_truncated_title_is_enriched_from_the_full_item(
        self, single_query_config, rates
    ):
        session = FakeSession(
            {"EBAY_US": search_response([
                item(item_id="v1|198009519465|0", title=TRUNCATED_HP, postal="19801")
            ])},
            item_responses={
                "v1|198009519465|0": item_detail(
                    title=TRUNCATED_HP + " NVIDIA GeForce RTX 5060, 16GB RAM, 512GB SSD",
                    aspects={
                        "Graphics Processing Type": "NVIDIA GeForce RTX 5060",
                        "RAM Size": "16 GB",
                        "SSD Capacity": "512 GB",
                    },
                ),
            },
        )
        listings = make_source(single_query_config, rates, session).fetch()
        listing = next(l for l in listings if l.listing_id == "v1|198009519465|0")

        # searchable_text lowercases everything, by design (the parser is
        # case-insensitive).
        text = listing.searchable_text
        assert "rtx 5060" in text
        assert "16 gb" in text
        assert "512 gb" in text

    def test_this_actually_rescues_a_listing_from_unparseable(
        self, single_query_config, rates, config
    ):
        """The point of the whole feature: a listing that would otherwise be
        thrown away for having no discoverable spec now parses correctly."""
        from dealhunter.evaluate import evaluate
        from dealhunter.fx import static_rates
        from dealhunter.models import RejectReason

        session = FakeSession(
            {"EBAY_US": search_response([
                item(item_id="v1|resc|0", title=TRUNCATED_HP, postal="19801")
            ])},
            item_responses={
                "v1|resc|0": item_detail(
                    title=TRUNCATED_HP + " full spec",
                    aspects={
                        "Graphics Processing Type": "NVIDIA GeForce RTX 5060",
                        "RAM Size": "16 GB",
                        "SSD Capacity": "1 TB",
                        "Screen Resolution": "2560x1600",
                    },
                ),
            },
        )
        listings = make_source(single_query_config, rates, session).fetch()
        listing = next(l for l in listings if l.listing_id == "v1|resc|0")

        rates_fixed = static_rates({"CAD": 0.73, "GBP": 1.27, "EUR": 1.09,
                                    "SEK": 0.095, "AUD": 0.66})
        evaluated = evaluate(listing, config, rates_fixed)
        assert RejectReason.UNPARSEABLE not in evaluated.reject_reasons

    def test_a_non_truncated_title_makes_no_extra_request(
        self, single_query_config, rates
    ):
        session = FakeSession({
            "EBAY_US": search_response([item(item_id="v1|full|0", title="Short Title")])
        })
        make_source(single_query_config, rates, session).fetch()

        assert not any("/item/" in c["url"] and "item_summary" not in c["url"]
                       for c in session.calls)

    def test_a_failed_enrichment_is_non_fatal(self, single_query_config, rates):
        """The listing keeps whatever the truncated title alone parsed to --
        worse than enriched, but no worse than before this feature existed."""
        session = FakeSession(
            {"EBAY_US": search_response([
                item(item_id="v1|fail|0", title=TRUNCATED_HP, postal="19801")
            ])},
            item_status=500,
        )
        listings = make_source(single_query_config, rates, session).fetch()
        listing = next(l for l in listings if l.listing_id == "v1|fail|0")
        assert listing.title == TRUNCATED_HP   # unchanged, nothing crashed

    def test_a_missing_item_id_in_the_fixture_returns_404_gracefully(
        self, single_query_config, rates
    ):
        session = FakeSession({
            "EBAY_US": search_response([
                item(item_id="v1|nodetail|0", title=TRUNCATED_HP, postal="19801")
            ])
        })  # no item_responses entry at all -> 404
        listings = make_source(single_query_config, rates, session).fetch()
        listing = next(l for l in listings if l.listing_id == "v1|nodetail|0")
        assert listing.title == TRUNCATED_HP

    def test_disabling_enrichment_skips_the_extra_request(
        self, single_query_config, rates
    ):
        single_query_config.raw_sources["ebay"]["enrich_truncated_titles"] = False
        try:
            session = FakeSession(
                {"EBAY_US": search_response([
                    item(item_id="v1|off|0", title=TRUNCATED_HP, postal="19801")
                ])},
                item_responses={"v1|off|0": item_detail(title="does not matter")},
            )
            make_source(single_query_config, rates, session).fetch()
            assert not any("/item/" in c["url"] and "item_summary" not in c["url"]
                           for c in session.calls)
        finally:
            single_query_config.raw_sources["ebay"].pop("enrich_truncated_titles", None)

    def test_item_id_with_pipes_is_url_encoded(self, single_query_config, rates):
        """eBay item IDs contain literal pipe characters (e.g. v1|123|0),
        which must be percent-encoded as a URL path segment."""
        session = FakeSession(
            {"EBAY_US": search_response([
                item(item_id="v1|99999|0", title=TRUNCATED_HP, postal="19801")
            ])},
            item_responses={"v1|99999|0": item_detail(title="fine")},
        )
        make_source(single_query_config, rates, session).fetch()

        item_call = next(c for c in session.calls
                         if "/item/" in c["url"] and "item_summary" not in c["url"])
        assert "|" not in item_call["url"]       # encoded, not raw
        assert "%7C" in item_call["url"]

    def test_a_short_title_ending_in_dots_is_not_treated_as_truncated(
        self, single_query_config, rates
    ):
        from dealhunter.sources.ebay import _looks_truncated

        assert _looks_truncated("Gaming Laptop...") is False
        assert _looks_truncated(TRUNCATED_HP) is True

    def test_enrichment_respects_the_request_budget(self, single_query_config, rates):
        single_query_config.raw_sources["http"]["max_requests_per_run"] = 1
        try:
            session = FakeSession(
                {"EBAY_US": search_response([
                    item(item_id="v1|budget|0", title=TRUNCATED_HP, postal="19801")
                ])},
                item_responses={"v1|budget|0": item_detail(title="should not be fetched")},
            )
            # Must not raise even with the budget exhausted immediately.
            make_source(single_query_config, rates, session).fetch()
        finally:
            single_query_config.raw_sources["http"]["max_requests_per_run"] = 200
