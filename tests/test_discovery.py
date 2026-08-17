"""
Monthly source discovery.

The load-bearing property is the one at the bottom: **nothing here ever
auto-enables a scraper.** A source that turns itself on is a source that gets
you IP-banned by a site you never chose to visit.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from dealhunter.discovery import Candidate, SourceDiscovery
from tests.fixtures_feeds import ROBOTS_ALLOW_ALL, FakeFeedSession, FakeResponse

TRACKER_HTML = """
<html><head>
  <title>Laptop Deals Tracker</title>
  <link rel="alternate" type="application/rss+xml" href="https://tracker.test/feed.xml"/>
</head><body>
  <h1>Gaming laptop deals</h1>
  <div class="deal">Lenovo Legion Pro 5 — $1,199 at Best Buy (USD)</div>
  <div class="deal">Predator Helios Neo — £1,150 at Currys</div>
</body></html>
"""

BARE_HTML = "<html><head><title>Some Blog</title></head><body><p>Hello</p></body></html>"


@pytest.fixture
def discovery_config(config, tmp_path):
    original = config.raw_sources.get("discovery")
    config.raw_sources["discovery"] = {
        "enabled": True,
        "output_path": str(tmp_path / "discovered_sources.yaml"),
        "harvest_subreddits": [],
        "seed_candidates": ["https://tracker.test"],
    }
    yield config
    config.raw_sources["discovery"] = original


@pytest.fixture
def discovery_session():
    return FakeFeedSession({
        "robots.txt": ROBOTS_ALLOW_ALL,
        "tracker.test": FakeResponse(200, TRACKER_HTML.encode()),
    })


class TestScheduling:
    def test_runs_on_a_first_ever_run(self, discovery_config):
        discovery = SourceDiscovery(discovery_config)
        assert discovery.should_run(datetime(2026, 8, 17, tzinfo=timezone.utc), None) is True

    def test_does_not_rerun_within_the_same_month(self, discovery_config):
        discovery = SourceDiscovery(discovery_config)
        assert discovery.should_run(
            datetime(2026, 8, 28, tzinfo=timezone.utc),
            datetime(2026, 8, 2, tzinfo=timezone.utc),
        ) is False

    def test_runs_again_in_a_new_month(self, discovery_config):
        discovery = SourceDiscovery(discovery_config)
        assert discovery.should_run(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 28, tzinfo=timezone.utc),
        ) is True

    def test_monthly_not_every_run(self, discovery_config):
        """This makes requests to sites we have no relationship with. Doing it
        eight times a day would be rude and would get us blocked."""
        discovery = SourceDiscovery(discovery_config)
        same_day_later = discovery.should_run(
            datetime(2026, 8, 17, 16, tzinfo=timezone.utc),
            datetime(2026, 8, 17, 4, tzinfo=timezone.utc),
        )
        assert same_day_later is False

    def test_can_be_switched_off(self, discovery_config):
        discovery_config.raw_sources["discovery"]["enabled"] = False
        assert SourceDiscovery(discovery_config).should_run() is False


class TestEvaluation:
    def test_a_candidate_with_a_feed_and_prices_scores_well(
        self, discovery_config, discovery_session
    ):
        discovery = SourceDiscovery(discovery_config, session=discovery_session,
                                    sleep=lambda _s: None)
        candidates = discovery.run()

        found = next(c for c in candidates if "tracker.test" in c.url)
        assert found.feed_url == "https://tracker.test/feed.xml"
        assert found.has_prices is True
        assert found.confidence >= 0.6
        assert found.name == "Laptop Deals Tracker"

    def test_regions_are_inferred_from_page_content(
        self, discovery_config, discovery_session
    ):
        discovery = SourceDiscovery(discovery_config, session=discovery_session,
                                    sleep=lambda _s: None)
        found = discovery.run()[0]
        assert "US" in found.regions
        assert "GB" in found.regions

    def test_an_rss_feed_is_called_out_as_the_cheap_path(
        self, discovery_config, discovery_session
    ):
        discovery = SourceDiscovery(discovery_config, session=discovery_session,
                                    sleep=lambda _s: None)
        assert "rss source" in discovery.run()[0].notes

    def test_a_page_with_no_prices_scores_low(self, discovery_config):
        session = FakeFeedSession({
            "robots.txt": ROBOTS_ALLOW_ALL,
            "tracker.test": FakeResponse(200, BARE_HTML.encode()),
        })
        discovery = SourceDiscovery(discovery_config, session=session,
                                    sleep=lambda _s: None)
        found = discovery.run()[0]

        assert found.has_prices is False
        assert found.confidence < 0.3
        assert "JS-rendered" in found.notes

    def test_robots_disallow_zeroes_the_confidence(self, discovery_config):
        session = FakeFeedSession({
            "robots.txt": FakeResponse(200, b"User-agent: *\nDisallow: /\n"),
            "tracker.test": FakeResponse(200, TRACKER_HTML.encode()),
        })
        discovery = SourceDiscovery(discovery_config, session=session,
                                    sleep=lambda _s: None)
        found = discovery.run()[0]

        assert found.robots_allows is False
        assert found.confidence == 0.0

    def test_an_unreachable_candidate_does_not_crash_the_pass(self, discovery_config):
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL},
                                  default=FakeResponse(500, b""))
        discovery = SourceDiscovery(discovery_config, session=session,
                                    sleep=lambda _s: None)
        found = discovery.run()
        assert found[0].confidence == 0.0

    def test_sources_we_already_read_are_skipped(self, config, tmp_path):
        """No point rediscovering OzBargain every month."""
        config.raw_sources["discovery"] = {
            "enabled": True,
            "output_path": str(tmp_path / "out.yaml"),
            "harvest_subreddits": [],
            "seed_candidates": ["https://www.ozbargain.com.au/deals/feed"],
        }
        session = FakeFeedSession({"robots.txt": ROBOTS_ALLOW_ALL})
        assert SourceDiscovery(config, session=session, sleep=lambda _s: None).run() == []


class TestApprovalGate:
    def test_candidates_are_written_as_pending(self, discovery_config, discovery_session):
        discovery = SourceDiscovery(discovery_config, session=discovery_session,
                                    sleep=lambda _s: None)
        discovery.run()

        with open(discovery.output_path, encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)

        assert payload["candidates"]
        assert all(c["status"] == "pending" for c in payload["candidates"])

    def test_nothing_is_written_into_the_live_source_config(
        self, discovery_config, discovery_session
    ):
        """The whole point: discovery never enables a scraper by itself."""
        before = yaml.safe_dump(discovery_config.raw_sources)
        SourceDiscovery(discovery_config, session=discovery_session,
                        sleep=lambda _s: None).run()
        assert yaml.safe_dump(discovery_config.raw_sources) == before

    def test_only_pending_candidates_are_surfaced_for_approval(
        self, discovery_config, discovery_session
    ):
        discovery = SourceDiscovery(discovery_config, session=discovery_session,
                                    sleep=lambda _s: None)
        discovery.save([Candidate(url="https://tracker.test", status="rejected")])

        assert discovery.run() == [], "a rejected candidate must not come back"

    def test_your_verdict_survives_a_rerun(self, discovery_config, discovery_session):
        """Facts get refreshed; your decision does not get overwritten."""
        discovery = SourceDiscovery(discovery_config, session=discovery_session,
                                    sleep=lambda _s: None)
        discovery.save([
            Candidate(url="https://tracker.test", status="approved",
                      first_seen="2026-01-01", confidence=0.1)
        ])
        discovery.run()

        saved = {c.url: c for c in discovery.load_existing()}
        entry = saved["https://tracker.test"]
        assert entry.status == "approved"          # your verdict, preserved
        assert entry.first_seen == "2026-01-01"    # original date, preserved
        assert entry.confidence > 0.1              # facts, refreshed

    def test_the_output_file_explains_how_to_approve(
        self, discovery_config, discovery_session
    ):
        discovery = SourceDiscovery(discovery_config, session=discovery_session,
                                    sleep=lambda _s: None)
        discovery.run()

        with open(discovery.output_path, encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)

        assert "Nothing here is active" in payload["_comment"]
        assert "approved" in payload["_comment"]
