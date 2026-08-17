"""
Notification: routing, rendering, and the channel implementations.

No network. The Discord and ntfy tests drive the real notifier classes through
a fake session and assert on the exact payload that would have gone over the
wire — including Discord's character limits, which return a 400 and silently
lose the message if you exceed them.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from dealhunter.evaluate import evaluate
from dealhunter.models import Condition, Flag, Region
from dealhunter.notify import (
    ConsoleNotifier,
    DiscordNotifier,
    NtfyNotifier,
    build_alert,
    build_notifiers,
    dispatch,
    route,
    should_send_digest,
)
from dealhunter.notify.base import Digest
from dealhunter.notify.discord import (
    MAX_DESCRIPTION,
    MAX_FIELD_VALUE,
    MAX_TITLE,
)
from dealhunter.notify.router import PKT
from tests.fixtures import make_listing


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakePoster:
    """Records every POST. `statuses` is consumed one per call."""

    def __init__(self, statuses: list[int] | None = None, payload=None):
        self.statuses = list(statuses or [])
        self.payload = payload
        self.posts: list[dict] = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.posts.append(
            {"url": url, "json": json, "data": data, "headers": headers or {}}
        )
        status = self.statuses.pop(0) if self.statuses else 204
        return FakeResponse(status, self.payload)


# ---------------------------------------------------------------------------
# Test listings
# ---------------------------------------------------------------------------


def scored(config, rates, **kwargs):
    """An evaluated listing that passed the filters."""
    result = evaluate(make_listing(**kwargs), config, rates)
    assert not result.rejected, f"unexpectedly rejected: {result.reject_reasons}"
    return result


@pytest.fixture
def high_score(config, rates):
    """The Helios Neo at its floor — priority, immediate alert."""
    return scored(config, rates, title_key="helios_neo_16s_openbox",
                  price=1184.0, jurisdiction="OR",
                  condition=Condition.OPEN_BOX_EXCELLENT, seller_name="Best Buy")


@pytest.fixture
def mid_score(config, rates):
    """Digest band."""
    return scored(config, rates, title_key="ambiguous_5070",
                  price=1150.0, jurisdiction="OR",
                  condition=Condition.OPEN_BOX_GOOD, seller_name="someone",
                  feedback=2000, percent=98.5)


@pytest.fixture
def canadian(config, rates):
    return scored(config, rates, title_key="legion_pro_5_oled", region=Region.CA,
                  price=1499.0, jurisdiction="AB",
                  condition=Condition.OPEN_BOX_EXCELLENT,
                  seller_name="Best Buy Canada")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_high_score_becomes_an_immediate_alert(self, config, high_score):
        decision = route([high_score], config)
        assert len(decision.alerts) == 1
        assert decision.is_silent is False

    def test_mid_score_is_held_for_the_digest(self, config, mid_score):
        assert config.notification["digest_score"] <= mid_score.score.total
        assert mid_score.score.total < config.notification["immediate_alert_score"]

        decision = route([mid_score], config, send_digest=False)
        assert decision.alerts == []
        assert decision.digest is None

        with_digest = route([mid_score], config, send_digest=True)
        assert with_digest.alerts == []
        assert with_digest.digest is not None
        assert len(with_digest.digest.top_picks) == 1

    def test_low_score_is_silent(self, config, rates):
        """Below 55: database only. Nothing is sent, ever."""
        low = scored(config, rates, title_key="legion_5_pro_16ach6h_used",
                     price=1380.0, jurisdiction="OR",
                     condition=Condition.USED, seller_name="nobody",
                     feedback=60, percent=97.0)
        assert low.score.total < config.notification["digest_score"]

        decision = route([low], config, send_digest=True)
        assert decision.alerts == []
        assert decision.digest.top_picks == []

    def test_nothing_worth_seeing_means_total_silence(self, config):
        """A system that pings you to say it found nothing is a system you mute."""
        decision = route([], config, send_digest=True)
        assert decision.is_silent is True
        assert decision.digest.is_empty is True

    def test_rejected_listings_never_reach_routing(self, config, rates):
        rejected = evaluate(
            make_listing("wuxga_trap", price=900.0, jurisdiction="OR"), config, rates
        )
        assert rejected.rejected
        decision = route([rejected], config, send_digest=True)
        assert decision.alerts == []
        assert decision.digest.top_picks == []

    def test_priority_listings_sort_above_higher_scores(self, config, high_score, rates):
        """A priority pick leads even if something else scored higher."""
        other = scored(config, rates, title_key="msi_vector_16hx",
                       price=1200.0, jurisdiction="OR",
                       condition=Condition.NEW, seller_name="Newegg")
        decision = route([other, high_score], config)
        assert decision.alerts[0].is_priority is True


class TestDeduplicationAndPriceDrops:
    def test_already_notified_listings_are_suppressed(self, config, high_score):
        """The same listing must not alert on every eight-hour run."""
        seen = {high_score.fingerprint: high_score.landed.landed_usd}
        decision = route([high_score], config, already_notified=seen)

        assert decision.alerts == []
        assert high_score.fingerprint in decision.suppressed

    def test_a_price_drop_over_5_percent_re_alerts(self, config, high_score):
        """$1,184 after we last reported $1,400 is a 15% drop."""
        seen = {high_score.fingerprint: 1400.0}
        decision = route([high_score], config, already_notified=seen)

        assert len(decision.alerts) == 1
        assert "PRICE DROP" in decision.alerts[0].headline_tag
        assert "15.4%" in decision.alerts[0].headline_tag

    def test_a_drop_under_the_threshold_stays_quiet(self, config, high_score):
        """Down 2% is noise, not news."""
        seen = {high_score.fingerprint: high_score.landed.landed_usd * 1.02}
        decision = route([high_score], config, already_notified=seen)
        assert decision.alerts == []

    def test_a_price_rise_does_not_re_alert(self, config, high_score):
        seen = {high_score.fingerprint: high_score.landed.landed_usd * 0.8}
        decision = route([high_score], config, already_notified=seen)
        assert decision.alerts == []


# ---------------------------------------------------------------------------
# Digest scheduling
# ---------------------------------------------------------------------------


class TestDigestScheduling:
    def test_before_the_digest_hour_nothing_is_sent(self, config):
        # 08:00 PKT = 03:00 UTC
        now = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
        assert should_send_digest(now, None, config) is False

    def test_first_run_after_the_digest_hour_sends(self, config):
        # 09:30 PKT = 04:30 UTC
        now = datetime(2026, 8, 17, 4, 30, tzinfo=timezone.utc)
        assert should_send_digest(now, None, config) is True

    def test_a_second_run_the_same_day_does_not_resend(self, config):
        now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)   # 17:00 PKT
        already_sent = datetime(2026, 8, 17, 4, 30, tzinfo=timezone.utc)
        assert should_send_digest(now, already_sent, config) is False

    def test_the_next_day_sends_again(self, config):
        now = datetime(2026, 8, 18, 4, 30, tzinfo=timezone.utc)
        yesterday = datetime(2026, 8, 17, 4, 30, tzinfo=timezone.utc)
        assert should_send_digest(now, yesterday, config) is True

    def test_a_missed_digest_hour_is_caught_by_the_next_run(self, config):
        """Actions cron drifts and runs get skipped. Matching a clock time
        exactly would silently lose a day's digest; 'first run past the hour'
        does not."""
        now = datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc)   # 21:00 PKT
        assert should_send_digest(now, None, config) is True

    def test_naive_datetimes_are_treated_as_utc(self, config):
        now = datetime(2026, 8, 17, 4, 30)
        assert should_send_digest(now, None, config) is True

    def test_pkt_is_utc_plus_five_with_no_dst(self):
        assert PKT.utcoffset(None) == timedelta(hours=5)


# ---------------------------------------------------------------------------
# Alert content
# ---------------------------------------------------------------------------


class TestAlertContent:
    def test_both_prices_always_appear_together(self, config, canadian):
        """The ranking rule: never a converted number on its own."""
        alert = build_alert(canadian, config)
        assert "C$1,499.00" in alert.price_line
        assert "$1,148.98" in alert.price_line

    def test_region_is_always_named(self, config, canadian):
        alert = build_alert(canadian, config)
        assert alert.region_display == "Canada"
        assert alert.region_flag == "🇨🇦"

    def test_alert_carries_every_required_field(self, config, high_score):
        alert = build_alert(high_score, config)
        assert alert.title
        assert alert.url
        assert alert.price_line
        assert alert.floor_line
        assert alert.spec_line
        assert alert.keyboard_line
        assert alert.condition_line
        assert alert.score_line
        assert alert.warranty_line

    def test_a_missing_warranty_note_says_so_explicitly(self, config, high_score):
        """A blank line reads as 'no warranty concerns'. It isn't — it means
        we don't know, which is a different thing."""
        alert = build_alert(high_score, config)
        assert "not stated" in alert.warranty_line
        assert "Pakistan" in alert.warranty_line

    def test_floor_delta_is_stated(self, config, rates):
        below = scored(config, rates, title_key="legion_pro_5_oled",
                       price=999.0, jurisdiction="OR", seller_name="Best Buy")
        alert = build_alert(below, config)
        assert "BELOW the known floor" in alert.floor_line
        assert "$1,049" in alert.floor_line

    def test_beating_the_floor_gets_a_loud_tag(self, config, rates):
        below = scored(config, rates, title_key="legion_pro_5_oled",
                       price=999.0, jurisdiction="OR", seller_name="Best Buy")
        alert = build_alert(below, config)
        assert "NEW RECORD LOW" in alert.headline_tag

    def test_priority_target_gets_a_loud_tag(self, config, high_score):
        alert = build_alert(high_score, config)
        assert "PRIORITY" in alert.headline_tag
        assert alert.is_priority is True

    def test_unverified_and_high_risk_flags_become_warnings(self, config, rates):
        risky = scored(config, rates, title_key="ambiguous_5070",
                       price=1150.0, jurisdiction="OR",
                       condition=Condition.USED, seller_name="newbie",
                       feedback=9, percent=100.0)
        alert = build_alert(risky, config)
        joined = " ".join(alert.warnings)
        assert "HIGH RISK" in joined
        assert "VRAM UNVERIFIED" in joined

    def test_multi_variation_listings_are_warned_about(self, config, rates):
        listing = make_listing("helios_neo_16s_openbox", price=1184.0,
                               jurisdiction="OR", seller_name="Best Buy")
        listing.source_flags.append(Flag.MULTI_VARIATION_LISTING)
        evaluated = evaluate(listing, config, rates)

        alert = build_alert(evaluated, config)
        assert any("MULTI-VARIATION" in warning for warning in alert.warnings)

    def test_keyboard_line_explains_the_iso_penalty(self, config, rates):
        uk = scored(config, rates, title_key="uk_listing", region=Region.GB, price=800.0)
        alert = build_alert(uk, config)
        assert "UK ISO" in alert.keyboard_line
        assert "-4" in alert.keyboard_line

    def test_unverified_canadian_keyboard_says_check_the_photos(self, config, rates):
        """No SKU suffix to go on, so the layout is genuinely unknown.

        (The `canadian` fixture uses an 83LT000MUS SKU, which *does* resolve to
        ANSI — see test_filters.py for that path.)
        """
        ambiguous = scored(config, rates, title_key="legion_5i_15", region=Region.CA,
                           price=1499.0, jurisdiction="AB",
                           condition=Condition.OPEN_BOX_EXCELLENT,
                           seller_name="Best Buy Canada")
        alert = build_alert(ambiguous, config)
        assert "UNVERIFIED" in alert.keyboard_line
        assert "photos" in alert.keyboard_line

    def test_spec_line_shows_unknowns_as_unknown(self, config, rates):
        unknown_tgp = scored(config, rates, title_key="panel_unstated",
                             price=1180.0, jurisdiction="OR", seller_name="Best Buy")
        alert = build_alert(unknown_tgp, config)
        assert "? TGP" in alert.spec_line


class TestRegionalAdvantageExplanation:
    def test_a_canadian_win_is_explained_with_real_numbers(
        self, config, canadian, rates
    ):
        """The brief asks for this sentence explicitly."""
        us_peer = scored(config, rates, title_key="legion_pro_5_oled",
                         price=1240.0, jurisdiction="OR", seller_name="Best Buy")
        alert = build_alert(canadian, config, peers=[canadian, us_peer])

        assert "beat the US field" in alert.regional_advantage
        assert "CAD weakness" in alert.regional_advantage
        assert "AB at 5% tax" in alert.regional_advantage
        assert "$91 under the best US listing" in alert.regional_advantage

    def test_no_explanation_when_the_us_listing_is_cheaper(
        self, config, canadian, rates
    ):
        cheaper_us = scored(config, rates, title_key="legion_pro_5_oled",
                            price=1000.0, jurisdiction="OR", seller_name="Best Buy")
        alert = build_alert(canadian, config, peers=[canadian, cheaper_us])
        assert alert.regional_advantage == ""

    def test_no_explanation_for_a_us_pick(self, config, high_score):
        alert = build_alert(high_score, config, peers=[high_score])
        assert alert.regional_advantage == ""


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


class TestDiscord:
    def test_unconfigured_without_a_webhook_url(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        assert DiscordNotifier().is_configured() is False
        assert DiscordNotifier(webhook_url="https://x/y").is_configured() is True

    def test_alert_becomes_an_embed_with_both_prices(self, config, canadian):
        poster = FakePoster()
        notifier = DiscordNotifier(webhook_url="https://discord.test/hook",
                                   session=poster)
        notifier.send_alert(build_alert(canadian, config))

        embed = poster.posts[0]["json"]["embeds"][0]
        assert "C$1,499.00" in embed["description"]
        assert "$1,148.98" in embed["description"]
        assert embed["url"] == canadian.listing.url

    def test_priority_alerts_are_red(self, config, high_score):
        from dealhunter.notify.discord import COLOUR_PRIORITY

        poster = FakePoster()
        DiscordNotifier(webhook_url="https://x/y", session=poster).send_alert(
            build_alert(high_score, config)
        )
        assert poster.posts[0]["json"]["embeds"][0]["color"] == COLOUR_PRIORITY

    def test_every_documented_limit_is_respected(self, config, rates):
        """Exceeding any of these returns a 400 and loses the message."""
        listing = make_listing(
            title="Acer Predator Helios Neo 16S AI " + ("very long title " * 40)
                  + "2560x1600 240Hz OLED RTX 5070 Ti 12GB 140W 32GB 1TB SSD",
            price=1184.0, jurisdiction="OR", seller_name="Best Buy",
        )
        evaluated = evaluate(listing, config, rates)
        poster = FakePoster()
        DiscordNotifier(webhook_url="https://x/y", session=poster).send_alert(
            build_alert(evaluated, config)
        )

        embed = poster.posts[0]["json"]["embeds"][0]
        assert len(embed["title"]) <= MAX_TITLE
        assert len(embed["description"]) <= MAX_DESCRIPTION
        assert len(embed["fields"]) <= 25
        for field in embed["fields"]:
            assert len(field["value"]) <= MAX_FIELD_VALUE

    def test_rate_limits_are_honoured_using_discords_own_retry_after(
        self, config, high_score
    ):
        """Discord tells us exactly how long to wait — don't guess."""
        waits = []
        poster = FakePoster(statuses=[429, 204], payload={"retry_after": 0.75})
        notifier = DiscordNotifier(
            webhook_url="https://x/y", session=poster, sleep=waits.append
        )
        notifier.send_alert(build_alert(high_score, config))

        assert waits == [0.75]
        assert len(poster.posts) == 2

    def test_a_400_raises_with_discords_own_error_body(self, config, high_score):
        poster = FakePoster(statuses=[400], payload={"embeds": ["too long"]})
        notifier = DiscordNotifier(webhook_url="https://x/y", session=poster)

        with pytest.raises(RuntimeError, match="400"):
            notifier.send_alert(build_alert(high_score, config))

    def test_digest_is_one_embed_not_ten(self, config, high_score, mid_score):
        poster = FakePoster()
        digest = Digest(
            top_picks=[build_alert(high_score, config), build_alert(mid_score, config)],
            listings_seen=42, listings_rejected=17,
        )
        DiscordNotifier(webhook_url="https://x/y", session=poster).send_digest(digest)

        embeds = poster.posts[0]["json"]["embeds"]
        assert len(embeds) == 1
        assert "42 listings seen" in embeds[0]["description"]

    def test_digest_reports_failed_sources(self, config):
        poster = FakePoster()
        digest = Digest(failed_sources=["ebay: BLOCKED — HTTP 403"])
        DiscordNotifier(webhook_url="https://x/y", session=poster).send_digest(digest)
        assert "403" in poster.posts[0]["json"]["embeds"][0]["description"]


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------


class TestNtfy:
    def test_unconfigured_without_a_topic(self, monkeypatch):
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        assert NtfyNotifier().is_configured() is False
        assert NtfyNotifier(topic="secret-topic").is_configured() is True

    def test_posts_to_the_topic_url(self, config, high_score):
        poster = FakePoster(statuses=[200])
        NtfyNotifier(topic="my-secret", session=poster).send_alert(
            build_alert(high_score, config)
        )
        assert poster.posts[0]["url"] == "https://ntfy.sh/my-secret"

    def test_headers_are_latin1_safe(self, config, canadian):
        """ntfy passes Title through an HTTP header, and the flag emoji in a
        region name would raise a UnicodeEncodeError on send."""
        poster = FakePoster(statuses=[200])
        NtfyNotifier(topic="t", session=poster).send_alert(build_alert(canadian, config))

        for key, value in poster.posts[0]["headers"].items():
            value.encode("latin-1")     # raises if we got this wrong
            key.encode("latin-1")

    def test_tapping_the_push_opens_the_listing(self, config, high_score):
        poster = FakePoster(statuses=[200])
        NtfyNotifier(topic="t", session=poster).send_alert(build_alert(high_score, config))
        assert poster.posts[0]["headers"]["Click"] == high_score.listing.url

    def test_priority_listings_push_as_urgent(self, config, high_score, mid_score):
        poster = FakePoster(statuses=[200, 200])
        notifier = NtfyNotifier(topic="t", session=poster)

        notifier.send_alert(build_alert(high_score, config))
        notifier.send_alert(build_alert(mid_score, config))

        assert poster.posts[0]["headers"]["Priority"] == "urgent"
        assert poster.posts[1]["headers"]["Priority"] == "high"

    def test_body_carries_the_decision_facts(self, config, canadian):
        poster = FakePoster(statuses=[200])
        NtfyNotifier(topic="t", session=poster).send_alert(build_alert(canadian, config))

        body = poster.posts[0]["data"].decode("utf-8")
        assert "C$1,499.00" in body
        assert "$1,148.98" in body
        assert "keyboard:" in body


# ---------------------------------------------------------------------------
# Dispatch and channel selection
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_one_channel_failing_does_not_stop_the_others(self, config, high_score):
        working = FakePoster(statuses=[200])
        broken = FakePoster(statuses=[400, 400, 400])

        results = dispatch(
            [
                DiscordNotifier(webhook_url="https://x/y", session=broken,
                                sleep=lambda _s: None),
                NtfyNotifier(topic="t", session=working),
            ],
            [build_alert(high_score, config)],
            None,
        )

        assert results[0].ok is False
        assert results[1].ok is True
        assert results[1].sent == 1

    def test_unconfigured_channels_are_skipped_not_failed(
        self, config, high_score, monkeypatch
    ):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        results = dispatch([DiscordNotifier()], [build_alert(high_score, config)], None)
        assert results[0].skipped_reason == "not configured"
        assert results[0].ok is True

    def test_an_empty_digest_is_not_sent(self, config):
        poster = FakePoster()
        dispatch([DiscordNotifier(webhook_url="https://x/y", session=poster)],
                 [], Digest())
        assert poster.posts == []

    def test_console_channel_prints_everything(self, config, high_score):
        stream = io.StringIO()
        dispatch([ConsoleNotifier(stream=stream)],
                 [build_alert(high_score, config)], None)

        output = stream.getvalue()
        assert "IMMEDIATE ALERT" in output
        assert "$1,184.00" in output
        assert "landed cost derivation" in output


class TestChannelSelection:
    def test_dry_run_uses_the_console_and_nothing_else(self, config, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://real/hook")
        monkeypatch.setenv("NTFY_TOPIC", "real-topic")

        notifiers = build_notifiers(config, dry_run=True)
        assert [n.name for n in notifiers] == ["console"]

    def test_configured_channels_are_used_when_not_dry_running(
        self, config, monkeypatch
    ):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://real/hook")
        monkeypatch.setenv("NTFY_TOPIC", "real-topic")

        names = [n.name for n in build_notifiers(config, dry_run=False)]
        assert names == ["discord", "ntfy"]

    def test_falls_back_to_console_when_nothing_is_configured(
        self, config, monkeypatch
    ):
        """Doing the work and silently discarding the results would be worse."""
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("NTFY_TOPIC", raising=False)

        notifiers = build_notifiers(config, dry_run=False)
        assert [n.name for n in notifiers] == ["console"]
