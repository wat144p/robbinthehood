"""
Tier 0/1 sources: RSS feeds, Reddit, Best Buy, and the HTML scraper.

The RSS tests run against fixtures reproducing the **verified live structure**
of OzBargain and HotUKDeals, including their custom namespaced elements — those
are the whole reason those feeds beat title-parsing.
"""

from __future__ import annotations

import pytest

from dealhunter.models import Condition, Flag, Region
from dealhunter.robots import RobotsCache
from dealhunter.sources.base import run_sources
from dealhunter.sources.bestbuy import BestBuySource
from dealhunter.sources.html import HtmlSource, _extract
from dealhunter.sources.reddit import RedditSource
from dealhunter.sources.rss import RssSource, local_name
from tests.fixtures_feeds import (
    ROBOTS_ALLOW_ALL,
    FakeFeedSession,
    FakeResponse,
    atom_feed,
    bestbuy_open_box,
    bestbuy_product,
    bestbuy_products,
    hotukdeals_feed,
    ozbargain_feed,
    reddit_listing,
    reddit_post,
)

LAPTOP_KEYWORDS = "laptop|notebook|legion|predator|omen|nitro|rog |tuf |vector|aero"


def no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


class TestRobots:
    def test_allow_rules_are_obeyed(self):
        session = FakeFeedSession({"robots.txt": FakeResponse(
            200, b"User-agent: *\nDisallow: /private/\n"
        )})
        robots = RobotsCache(user_agent="robbin/0.1", session=session)

        assert robots.is_allowed("https://example.test/deals") is True
        assert robots.is_allowed("https://example.test/private/x") is False

    def test_a_404_means_no_restrictions(self):
        """RFC 9309: no robots.txt published means crawling is allowed."""
        session = FakeFeedSession({"robots.txt": FakeResponse(404, b"")})
        robots = RobotsCache(user_agent="robbin/0.1", session=session)
        assert robots.is_allowed("https://example.test/anything") is True

    def test_a_5xx_means_complete_disallow(self):
        """The site is unwell — don't add to it. This is what the RFC says, and
        it's the half people usually get backwards."""
        session = FakeFeedSession({"robots.txt": FakeResponse(503, b"")})
        robots = RobotsCache(user_agent="robbin/0.1", session=session)
        assert robots.is_allowed("https://example.test/anything") is False

    def test_a_network_failure_is_treated_like_a_5xx(self):
        class ExplodingSession:
            def get(self, *a, **kw):
                raise ConnectionError("no route to host")

        robots = RobotsCache(user_agent="robbin/0.1", session=ExplodingSession())
        assert robots.is_allowed("https://example.test/x") is False

    def test_crawl_delay_is_read(self):
        session = FakeFeedSession({"robots.txt": FakeResponse(
            200, b"User-agent: *\nCrawl-delay: 10\nAllow: /\n"
        )})
        robots = RobotsCache(user_agent="robbin/0.1", session=session)
        assert robots.crawl_delay("https://example.test/x") == 10.0

    def test_robots_is_fetched_once_per_host(self):
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL})
        robots = RobotsCache(user_agent="robbin/0.1", session=session)

        for _ in range(5):
            robots.is_allowed("https://example.test/page")

        assert len([c for c in session.calls if "robots" in c["url"]]) == 1


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


@pytest.fixture
def rss_config(config):
    """Trim to two feeds so assertions are readable."""
    original = config.raw_sources["rss"]["feeds"]
    config.raw_sources["rss"]["feeds"] = {
        "ozbargain": {
            "enabled": True, "url": "https://ozb.test/deals/feed",
            "region": "AU", "currency": "AUD",
            "keyword_pattern": LAPTOP_KEYWORDS,
        },
        "hotukdeals": {
            "enabled": True, "url": "https://huk.test/rss/hot",
            "region": "GB", "currency": "GBP",
            "keyword_pattern": LAPTOP_KEYWORDS,
        },
    }
    yield config
    config.raw_sources["rss"]["feeds"] = original


@pytest.fixture
def rss_session():
    return FakeFeedSession({
        "robots.txt": ROBOTS_ALLOW_ALL,
        "ozb.test": FakeResponse(200, ozbargain_feed()),
        "huk.test": FakeResponse(200, hotukdeals_feed()),
    })


class TestRssParsing:
    def test_namespaces_are_stripped_to_local_names(self):
        """Matching local names keeps us working if a platform changes its
        namespace URI."""
        assert local_name("{https://www.pepper.com/xmlns}merchant") == "merchant"
        assert local_name("title") == "title"

    def test_laptop_deals_are_picked_out_of_an_all_category_feed(
        self, rss_config, rss_session
    ):
        """The same OzBargain feed carries laptops, Steam keys and dartboards."""
        listings = RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        titles = [l.title for l in listings]

        assert any("Legion Pro 5" in t for t in titles)
        assert not any("Lossless Scaling" in t for t in titles)
        assert not any("Dartboard" in t for t in titles)

    def test_ozbargain_meta_gives_the_real_destination(self, rss_config, rss_session):
        """<ozb:meta url=…> is the retailer; <link> is only the forum thread."""
        listings = RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        legion = next(l for l in listings if "Legion Pro 5" in l.title)
        assert legion.url == "https://www.lenovo.com/au/en/p/legion-pro-5"

    def test_pepper_merchant_element_supplies_price_and_retailer(
        self, rss_config, rss_session
    ):
        """HotUKDeals hands us both already separated — no title parsing."""
        listings = RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        legion = next(l for l in listings if "Legion 7i" in l.title)

        assert legion.sticker_price_local == 1150.0
        assert legion.currency == "GBP"
        assert legion.seller_name == "Currys"
        assert legion.region == Region.GB

    def test_deal_temperature_is_not_read_as_a_price(self, rss_config, rss_session):
        """HotUKDeals titles start with "312° - ", which is votes, not money."""
        listings = RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        legion = next(l for l in listings if "Legion 7i" in l.title)
        assert legion.sticker_price_local == 1150.0

    def test_price_is_parsed_from_the_title_when_there_is_no_merchant_element(
        self, rss_config, rss_session
    ):
        listings = RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        legion = next(l for l in listings if "Legion Pro 5" in l.title)
        assert legion.sticker_price_local == 2199.0
        assert legion.currency == "AUD"

    def test_stale_items_are_dropped(self, rss_config, rss_session):
        listings = RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        assert not any("ancient" in l.title for l in listings)

    def test_everything_from_a_community_feed_is_unverified(
        self, rss_config, rss_session
    ):
        """A community post is a claim about a price, not the price."""
        listings = RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        assert listings
        for listing in listings:
            assert Flag.UNVERIFIED_SOURCE in listing.source_flags

    def test_atom_feeds_parse_too(self, config):
        config.raw_sources["rss"]["feeds"] = {
            "tracker": {"enabled": True, "url": "https://atom.test/feed",
                        "region": "US", "currency": "USD",
                        "keyword_pattern": LAPTOP_KEYWORDS},
        }
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "atom.test": FakeResponse(200, atom_feed()),
        })
        listings = RssSource(config, session=session, sleep=no_sleep).fetch()

        assert len(listings) == 1
        assert listings[0].sticker_price_local == 1184.0
        assert listings[0].url == "https://example.test/deal/1"


class TestRssFailureHandling:
    def test_robots_disallow_skips_the_feed(self, rss_config):
        session = FakeFeedSession({
            "robots.txt": FakeResponse(200, b"User-agent: *\nDisallow: /\n"),
        })
        source = RssSource(rss_config, session=session, sleep=no_sleep)
        assert source.fetch() == []
        assert any("robots.txt" in err for err in source.feed_errors)

    def test_a_403_blocks_that_feed_only(self, rss_config):
        """RedFlagDeals and Slickdeals both do this now. One bot wall must not
        cost us the other feeds."""
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "ozb.test": FakeResponse(403, b"forbidden"),
            "huk.test": FakeResponse(200, hotukdeals_feed()),
        })
        source = RssSource(rss_config, session=session, sleep=no_sleep)
        listings = source.fetch()

        assert any("Legion 7i" in l.title for l in listings)
        assert any("403" in err for err in source.feed_errors)

    def test_a_disabled_feed_is_not_fetched(self, rss_config, rss_session):
        rss_config.raw_sources["rss"]["feeds"]["ozbargain"]["enabled"] = False
        RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
        assert not any("ozb.test" in c["url"] for c in rss_session.calls)

    def test_a_disabled_region_is_not_fetched(self, rss_config, rss_session):
        rss_config.regions[Region.AU].enabled = False
        try:
            RssSource(rss_config, session=rss_session, sleep=no_sleep).fetch()
            assert not any("ozb.test" in c["url"] for c in rss_session.calls)
        finally:
            rss_config.regions[Region.AU].enabled = True

    def test_the_source_survives_a_totally_broken_feed(self, rss_config):
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "ozb.test": FakeResponse(200, b"<<< not xml at all"),
            "huk.test": FakeResponse(200, hotukdeals_feed()),
        })
        results = run_sources([RssSource(rss_config, session=session, sleep=no_sleep)])
        assert results[0].listings, "the working feed should still produce listings"


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------


@pytest.fixture
def reddit_config(config):
    original = config.raw_sources["reddit"]["subreddits"]
    config.raw_sources["reddit"]["subreddits"] = {
        "LaptopDeals": {
            "enabled": True, "region": "US", "currency": "USD",
            "flair_include": [r"1[,.]?000?\s*[-–to]+\s*\$?1[,.]?200",
                              r"1[,.]?200?\s*[-–to]+\s*\$?1[,.]?400"],
            "keyword_pattern": "laptop|legion|predator|rtx",
        },
    }
    yield config
    config.raw_sources["reddit"]["subreddits"] = original


def reddit_session(posts):
    return FakeFeedSession({"reddit.com": FakeResponse(
        200, payload=reddit_listing(posts), text="{}"
    )})


class TestReddit:
    def test_a_flaired_deal_post_becomes_a_listing(self, reddit_config):
        session = reddit_session([reddit_post()])
        listings = RedditSource(reddit_config, session=session, sleep=no_sleep).fetch()

        assert len(listings) == 1
        listing = listings[0]
        assert listing.sticker_price_local == 1184.0
        assert listing.region == Region.US
        assert listing.source == "reddit:LaptopDeals"
        assert Flag.UNVERIFIED_SOURCE in listing.source_flags

    def test_the_outbound_retailer_link_is_used_not_the_thread(self, reddit_config):
        session = reddit_session([reddit_post(url="https://www.bestbuy.com/site/6604821")])
        listings = RedditSource(reddit_config, session=session, sleep=no_sleep).fetch()
        assert listings[0].url == "https://www.bestbuy.com/site/6604821"

    @pytest.mark.parametrize(
        "flair,kept",
        [
            ("$1000-$1200", True),
            ("$1000 - $1200", True),
            ("$1200-$1400", True),
            ("$600-$800", False),
            ("Under $500", False),
        ],
    )
    def test_price_bracket_flairs_are_filtered(self, reddit_config, flair, kept):
        """r/LaptopDeals brackets by price; we want the two bands in budget."""
        session = reddit_session([reddit_post(flair=flair)])
        listings = RedditSource(reddit_config, session=session, sleep=no_sleep).fetch()
        assert bool(listings) is kept

    def test_stickied_and_self_posts_are_skipped(self, reddit_config):
        session = reddit_session([
            reddit_post(post_id="a", stickied=True),
            reddit_post(post_id="b", is_self=True),
        ])
        assert RedditSource(reddit_config, session=session, sleep=no_sleep).fetch() == []

    def test_old_posts_are_skipped(self, reddit_config):
        session = reddit_session([reddit_post(age_hours=200)])
        assert RedditSource(reddit_config, session=session, sleep=no_sleep).fetch() == []

    def test_posts_without_a_price_are_skipped(self, reddit_config):
        """A post with no price is not actionable, and guessing would be worse."""
        session = reddit_session([
            reddit_post(title="Is the Legion Pro 5 a good laptop for ML?")
        ])
        assert RedditSource(reddit_config, session=session, sleep=no_sleep).fetch() == []

    def test_condition_is_read_from_the_post_text(self, reddit_config):
        session = reddit_session([reddit_post(
            title="[$1,184] Predator Helios Neo 16S AI RTX 5070 Ti — Open Box Excellent"
        )])
        listings = RedditSource(reddit_config, session=session, sleep=no_sleep).fetch()
        assert listings[0].condition == Condition.OPEN_BOX_EXCELLENT

    def test_a_descriptive_user_agent_is_sent(self, reddit_config):
        """Reddit blocks default agents hard."""
        session = reddit_session([reddit_post()])
        RedditSource(reddit_config, session=session, sleep=no_sleep).fetch()

        agent = session.calls[0]["headers"]["User-Agent"]
        assert "robbin-the-hood" in agent
        assert "python-requests" not in agent

    def test_a_429_blocks_the_subreddit_without_killing_the_source(self, reddit_config):
        session = FakeFeedSession({"reddit.com": FakeResponse(429, b"slow down")})
        source = RedditSource(reddit_config, session=session, sleep=no_sleep)

        assert source.fetch() == []
        assert any("429" in err for err in source.subreddit_errors)


class TestRedditAuth:
    """Reddit's unauthenticated JSON API is gated as of 2026-08-17:
    www.reddit.com 403s regardless of User-Agent, and old.reddit.com serves an
    HTML interstitial. OAuth client-credentials is the working path."""

    def test_without_credentials_the_403_explains_the_fix(
        self, reddit_config, monkeypatch
    ):
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)

        session = FakeFeedSession({"reddit.com": FakeResponse(403, b"blocked")})
        source = RedditSource(reddit_config, session=session, sleep=no_sleep)
        source.fetch()

        error = source.subreddit_errors[0]
        assert "OAuth" in error
        assert "REDDIT_CLIENT_ID" in error
        assert "prefs/apps" in error

    def test_an_html_interstitial_is_treated_as_a_block_not_zero_results(
        self, reddit_config, monkeypatch
    ):
        """old.reddit.com returns 200 with text/html. Parsing that as "no deals
        today" would hide the problem indefinitely."""
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)

        session = FakeFeedSession({"reddit.com": FakeResponse(
            200, b"<!DOCTYPE html><title>Welcome to Reddit</title>",
            headers={"content-type": "text/html; charset=utf-8"},
        )})
        source = RedditSource(reddit_config, session=session, sleep=no_sleep)
        source.fetch()

        assert any("interstitial" in err for err in source.subreddit_errors)

    def test_credentials_switch_it_to_the_oauth_host(self, reddit_config):
        session = FakeFeedSession({
            "oauth.reddit.com": FakeResponse(200, payload=reddit_listing([reddit_post()])),
            "api/v1/access_token": FakeResponse(
                200, payload={"access_token": "tok", "expires_in": 86400}
            ),
        })
        source = RedditSource(reddit_config, session=session, sleep=no_sleep,
                              client_id="id", client_secret="secret")
        listings = source.fetch()

        assert len(listings) == 1
        fetch_call = next(c for c in session.calls if "/r/LaptopDeals" in c["url"])
        assert fetch_call["url"].startswith("https://oauth.reddit.com")
        assert fetch_call["headers"]["Authorization"] == "Bearer tok"

    def test_the_token_is_fetched_once_and_reused(self, config):
        config.raw_sources["reddit"]["subreddits"] = {
            name: {"enabled": True, "region": "US", "currency": "USD",
                   "keyword_pattern": "laptop|predator|rtx"}
            for name in ("LaptopDeals", "buildapcsales", "GamingLaptops")
        }
        session = FakeFeedSession({
            "oauth.reddit.com": FakeResponse(200, payload=reddit_listing([reddit_post()])),
            "api/v1/access_token": FakeResponse(
                200, payload={"access_token": "tok", "expires_in": 86400}
            ),
        })
        RedditSource(config, session=session, sleep=no_sleep,
                     client_id="id", client_secret="secret").fetch()

        token_calls = [c for c in session.calls if "access_token" in c["url"]]
        assert len(token_calls) == 1

    def test_rejected_credentials_say_so_clearly(self, reddit_config):
        session = FakeFeedSession({
            "api/v1/access_token": FakeResponse(401, b"unauthorized"),
        })
        source = RedditSource(reddit_config, session=session, sleep=no_sleep,
                              client_id="bad", client_secret="bad")
        source.fetch()

        assert any("rejected the credentials" in err for err in source.subreddit_errors)


# ---------------------------------------------------------------------------
# Best Buy
# ---------------------------------------------------------------------------


class TestBestBuy:
    def test_missing_key_is_a_clear_auth_error(self, config, monkeypatch):
        from dealhunter.sources.base import SourceAuthError

        monkeypatch.delenv("BESTBUY_API_KEY", raising=False)
        with pytest.raises(SourceAuthError, match="BESTBUY_API_KEY"):
            BestBuySource(config, session=FakeFeedSession()).fetch()

    def test_open_box_condition_tiers_become_separate_listings(self, config):
        """The same SKU is listed Excellent/Good/Fair at different prices, and
        each is a genuinely different buying decision."""
        session = FakeFeedSession({
            "/beta/products/openBox": FakeResponse(200, payload=bestbuy_open_box()),
            "/v1/products": FakeResponse(
                200, payload=bestbuy_products([bestbuy_product()])
            ),
        })
        listings = BestBuySource(config, session=session, api_key="k",
                                 sleep=no_sleep).fetch()

        open_box = [l for l in listings if "Open Box" in l.title]
        assert {l.condition for l in open_box} == {
            Condition.OPEN_BOX_EXCELLENT,
            Condition.OPEN_BOX_GOOD,
            Condition.OPEN_BOX_FAIR,
        }
        excellent = next(l for l in open_box if l.condition == Condition.OPEN_BOX_EXCELLENT)
        assert excellent.sticker_price_local == 1184.00
        assert excellent.is_major_retailer is True

    def test_open_box_listings_have_distinct_fingerprints(self, config):
        session = FakeFeedSession({
            "/beta/products/openBox": FakeResponse(200, payload=bestbuy_open_box()),
            "/v1/products": FakeResponse(
                200, payload=bestbuy_products([bestbuy_product()])
            ),
        })
        listings = BestBuySource(config, session=session, api_key="k",
                                 sleep=no_sleep).fetch()
        fingerprints = [l.fingerprint() for l in listings]
        assert len(fingerprints) == len(set(fingerprints))

    def test_a_broken_open_box_call_does_not_lose_the_new_products(self, config):
        """The open-box endpoint is beta and has changed shape before."""
        session = FakeFeedSession({
            "/beta/products/openBox": FakeResponse(500, b"server error"),
            "/v1/products": FakeResponse(
                200, payload=bestbuy_products([bestbuy_product()])
            ),
        })
        source = BestBuySource(config, session=session, api_key="k", sleep=no_sleep)
        listings = source.fetch()

        assert any(l.condition == Condition.NEW for l in listings)
        assert source.open_box_error is not None

    def test_the_price_ceiling_comes_from_the_budget(self, config):
        session = FakeFeedSession({
            "/v1/products": FakeResponse(200, payload=bestbuy_products([])),
            "/beta/products/openBox": FakeResponse(200, payload={"results": []}),
        })
        BestBuySource(config, session=session, api_key="k", sleep=no_sleep).fetch()

        search_call = next(c for c in session.calls if "/v1/products" in c["url"])
        assert "salePrice<=1470.00" in search_call["url"]


# ---------------------------------------------------------------------------
# HTML scraper
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<html><body>
  <article class="product-tile">
    <h2 class="product-title">Lenovo Legion Pro 5 16 WQXGA OLED RTX 5060 32GB 1TB</h2>
    <span class="price">£1,149.99</span>
    <a class="product-link" href="/products/legion-pro-5">View</a>
  </article>
  <article class="product-tile">
    <h2 class="product-title">Acer Nitro V 16 FHD RTX 4060 16GB 512GB</h2>
    <span class="price">£699.00</span>
    <a class="product-link" href="/products/nitro-v">View</a>
  </article>
</body></html>
"""


@pytest.fixture
def html_config(config):
    original = config.raw_sources["html"]["sites"]
    config.raw_sources["html"]["sites"] = {
        "testshop": {
            "enabled": True,
            "base_url": "https://shop.test",
            "region": "GB", "currency": "GBP",
            "paths": ["/gaming/laptops"],
            "seller_name": "TestShop",
            "condition": "NEW",
            "selectors": {
                "item": "article.product-tile",
                "title": "h2.product-title",
                "price": "span.price",
                "url": "a.product-link@href",
            },
        },
    }
    yield config
    config.raw_sources["html"]["sites"] = original


class TestHtmlSelectors:
    def test_attribute_suffix_reads_an_attribute(self):
        from dealhunter.sources.html import _soup

        node = _soup('<div><a class="x" href="/deal">Buy</a></div>')
        assert _extract(node, "a.x") == "Buy"
        assert _extract(node, "a.x@href") == "/deal"

    def test_a_missing_selector_returns_empty_not_an_error(self):
        from dealhunter.sources.html import _soup

        node = _soup("<div></div>")
        assert _extract(node, "a.nope@href") == ""
        assert _extract(node, None) == ""


class TestHtmlSource:
    def test_listings_are_extracted_and_urls_made_absolute(self, html_config):
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "shop.test": FakeResponse(200, SAMPLE_HTML.encode()),
        })
        listings = HtmlSource(html_config, session=session, sleep=no_sleep).fetch()

        assert len(listings) == 2
        legion = next(l for l in listings if "Legion" in l.title)
        assert legion.sticker_price_local == 1149.99
        assert legion.url == "https://shop.test/products/legion-pro-5"
        assert legion.seller_name == "TestShop"

    def test_a_selector_matching_nothing_is_reported_loudly(self, html_config):
        """Silently returning zero results would look like 'no deals today'."""
        html_config.raw_sources["html"]["sites"]["testshop"]["selectors"]["item"] = ".gone"
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "shop.test": FakeResponse(200, SAMPLE_HTML.encode()),
        })
        source = HtmlSource(html_config, session=session, sleep=no_sleep)
        source.fetch()

        assert any("matched nothing" in err for err in source.site_errors)

    def test_a_captcha_page_disables_the_site(self, html_config):
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "shop.test": FakeResponse(
                200, b"<html><body>Please verify you are human</body></html>"
            ),
        })
        source = HtmlSource(html_config, session=session, sleep=no_sleep)
        source.fetch()

        assert "testshop" in source.disabled_this_run
        assert any("captcha" in err for err in source.site_errors)

    def test_a_403_disables_the_site_without_retrying(self, html_config):
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "shop.test": FakeResponse(403, b"forbidden"),
        })
        source = HtmlSource(html_config, session=session, sleep=no_sleep)
        source.fetch()

        assert "testshop" in source.disabled_this_run
        assert len([c for c in session.calls if "gaming/laptops" in c["url"]]) == 1

    def test_robots_disallow_skips_the_path(self, html_config):
        session = FakeFeedSession({
            "robots.txt": FakeResponse(200, b"User-agent: *\nDisallow: /gaming/\n"),
            "shop.test": FakeResponse(200, SAMPLE_HTML.encode()),
        })
        assert HtmlSource(html_config, session=session, sleep=no_sleep).fetch() == []

    def test_no_site_ships_enabled_with_unverified_selectors(self, config):
        """Selectors written without seeing live markup are fiction, and a
        scraper that silently returns zero looks exactly like 'no deals today'.

        A site may only be enabled once its selectors have been confirmed with
        `--probe` against the live page — which means they cannot be blank.
        """
        for name, block in (config.raw_sources["html"]["sites"] or {}).items():
            if not block.get("enabled", False):
                continue
            selectors = block.get("selectors") or {}
            for key in ("item", "title", "price", "url"):
                assert (selectors.get(key) or "").strip(), (
                    f"{name} is enabled but its {key!r} selector is empty — "
                    f"verify it with `python run.py --probe {name}` first"
                )

    def test_unverified_sites_are_disabled(self, config):
        """The converse: anything still carrying blank selectors stays off."""
        for name, block in (config.raw_sources["html"]["sites"] or {}).items():
            selectors = block.get("selectors") or {}
            if not (selectors.get("item") or "").strip():
                assert block.get("enabled", False) is False, (
                    f"{name} has no item selector but is enabled"
                )

    def test_probe_reports_what_the_selectors_actually_match(self, html_config):
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "shop.test": FakeResponse(200, SAMPLE_HTML.encode()),
        })
        report = HtmlSource(html_config, session=session, sleep=no_sleep).probe("testshop")

        assert "matched 2 node(s)" in report
        assert "Legion Pro 5" in report

    def test_probe_explains_an_empty_match(self, html_config):
        html_config.raw_sources["html"]["sites"]["testshop"]["selectors"]["item"] = ".gone"
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "shop.test": FakeResponse(200, SAMPLE_HTML.encode()),
        })
        report = HtmlSource(html_config, session=session, sleep=no_sleep).probe("testshop")

        assert "matched 0 node(s)" in report
        assert "JavaScript-rendered" in report


# ---------------------------------------------------------------------------
# render: js — the headless-browser path for client-rendered sites
# ---------------------------------------------------------------------------


@pytest.fixture
def rendered_html_config(config):
    """Same as html_config, but the site is marked render: js."""
    original = config.raw_sources["html"]["sites"]
    config.raw_sources["html"]["sites"] = {
        "jsshop": {
            "enabled": True,
            "render": "js",
            "base_url": "https://js.test",
            "region": "US", "currency": "USD",
            "paths": ["/laptops"],
            "seller_name": "JsShop",
            "condition": "NEW",
            "selectors": {
                "item": "article.product-tile",
                "title": "h2.product-title",
                "price": "span.price",
                "url": "a.product-link@href",
            },
        },
    }
    yield config
    config.raw_sources["html"]["sites"] = original


RENDERED_HTML = """
<html><body>
  <article class="product-tile">
    <h2 class="product-title">ASUS ROG Strix G16 RTX 5070 Ti 32GB 1TB</h2>
    <span class="price">$1,349.00</span>
    <a class="product-link" href="/products/rog-strix-g16">View</a>
  </article>
</body></html>
"""


class TestRenderJs:
    def test_render_js_calls_the_headless_fetcher_not_plain_requests(
        self, rendered_html_config, monkeypatch
    ):
        """The whole point of the flag: a plain GET must never be used for a
        site marked render: js."""
        calls = []

        def fake_fetch(url, *, user_agent, timeout_seconds, wait_for_selector=None,
                       extra_wait_seconds=1.5):
            calls.append({"url": url, "wait_for_selector": wait_for_selector})
            return RENDERED_HTML

        monkeypatch.setattr(
            "dealhunter.headless.fetch_rendered_html", fake_fetch
        )
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL})
        listings = HtmlSource(rendered_html_config, session=session,
                              sleep=no_sleep).fetch()

        assert len(calls) == 1
        assert calls[0]["url"] == "https://js.test/laptops"
        # It must never have hit the plain requests session for the page —
        # only robots.txt, which is always a normal GET.
        assert not any("js.test/laptops" in c["url"] for c in session.calls)
        assert len(listings) == 1
        assert listings[0].sticker_price_local == 1349.00

    def test_the_item_selector_is_passed_as_the_wait_for_selector(
        self, rendered_html_config, monkeypatch
    ):
        """So the headless fetcher waits for the actual product grid to
        appear, not just for network activity to go quiet — which on a slow
        product page can still be too early."""
        captured = {}

        def fake_fetch(url, *, user_agent, timeout_seconds, wait_for_selector=None,
                       extra_wait_seconds=1.5):
            captured["wait_for_selector"] = wait_for_selector
            return RENDERED_HTML

        monkeypatch.setattr("dealhunter.headless.fetch_rendered_html", fake_fetch)
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL})
        HtmlSource(rendered_html_config, session=session, sleep=no_sleep).fetch()

        assert captured["wait_for_selector"] == "article.product-tile"

    def test_a_missing_playwright_install_is_reported_not_fatal(
        self, rendered_html_config, monkeypatch
    ):
        """Genuinely exercises the real 'Playwright not installed' path,
        since this test environment does not have it installed."""
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL})
        source = HtmlSource(rendered_html_config, session=session, sleep=no_sleep)

        listings = source.fetch()   # must not raise

        assert listings == []
        assert any("pip install" in err for err in source.site_errors)

    def test_a_headless_fetch_failure_is_reported_like_any_other_source_error(
        self, rendered_html_config, monkeypatch
    ):
        from dealhunter.headless import HeadlessFetchFailed

        def fake_fetch(*a, **kw):
            raise HeadlessFetchFailed("page.test did not finish rendering in 30s")

        monkeypatch.setattr("dealhunter.headless.fetch_rendered_html", fake_fetch)
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL})
        source = HtmlSource(rendered_html_config, session=session, sleep=no_sleep)

        assert source.fetch() == []
        assert any("did not finish rendering" in err for err in source.site_errors)

    def test_a_non_rendered_site_never_touches_the_headless_module(
        self, html_config, monkeypatch
    ):
        """The cheap path stays cheap: a site without render: js must not
        import or call the headless fetcher at all."""
        def explode(*a, **kw):
            raise AssertionError("plain HTTP sites must not use the headless fetcher")

        monkeypatch.setattr("dealhunter.headless.fetch_rendered_html", explode)
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "shop.test": FakeResponse(200, SAMPLE_HTML.encode()),
        })
        listings = HtmlSource(html_config, session=session, sleep=no_sleep).fetch()
        assert len(listings) == 2   # unaffected — proves the path was never hit

    def test_probe_reports_which_fetch_mode_a_site_uses(
        self, rendered_html_config, monkeypatch
    ):
        def fake_fetch(url, *, user_agent, timeout_seconds, wait_for_selector=None,
                       extra_wait_seconds=1.5):
            return RENDERED_HTML

        monkeypatch.setattr("dealhunter.headless.fetch_rendered_html", fake_fetch)
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL})
        report = HtmlSource(rendered_html_config, session=session,
                            sleep=no_sleep).probe("jsshop")

        assert "headless browser" in report
