"""
SQLite state: dedup, price history, floor updates, expiry, idempotency.

The floor tests are the important ones. The floor feeds the ±10 price-vs-floor
scoring component, so a bad floor silently poisons every future score for that
model — and unlike a wrong alert, you would never notice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dealhunter.evaluate import evaluate
from dealhunter.models import Condition, Flag, Region
from dealhunter.store import Store
from tests.fixtures import make_listing


@pytest.fixture
def store(tmp_path, config):
    with Store(tmp_path / "test.db") as store:
        store.seed_floors(config)
        yield store


def scored(config, rates, **kwargs):
    result = evaluate(make_listing(**kwargs), config, rates)
    assert not result.rejected, f"unexpectedly rejected: {result.reject_reasons}"
    return result


# ---------------------------------------------------------------------------
# Schema and idempotency
# ---------------------------------------------------------------------------


class TestSchema:
    def test_opening_an_existing_database_is_a_noop(self, tmp_path, config):
        path = tmp_path / "reopen.db"
        with Store(path) as first:
            first.seed_floors(config)
            before = first.floors()
        with Store(path) as second:
            second.seed_floors(config)
            assert second.floors() == before

    def test_stats_on_an_empty_database(self, store):
        assert store.stats()["listings"] == 0


class TestIdempotency:
    def test_recording_the_same_run_twice_changes_nothing(self, store, config, rates):
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")

        store.record_listings([item])
        first = store.stats()
        store.record_listings([item])
        second = store.stats()

        assert first == second, "a repeated run must not duplicate anything"

    def test_price_history_only_grows_when_the_price_moves(self, store, config, rates):
        """Eight runs a day would otherwise add 24 identical rows per listing."""
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")

        for _ in range(5):
            store.record_listings([item])
        assert store.stats()["price_points"] == 1

        cheaper = scored(config, rates, title_key="helios_neo_16s_openbox",
                         price=1099.0, jurisdiction="OR", seller_name="Best Buy",
                         listing_id=item.listing.listing_id)
        store.record_listings([cheaper])
        assert store.stats()["price_points"] == 2


# ---------------------------------------------------------------------------
# Notifications and dedup
# ---------------------------------------------------------------------------


class TestNotifications:
    def test_a_notified_listing_is_remembered_with_its_price(self, store, config, rates):
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")

        store.record_notifications([(item.fingerprint, 1184.0, 88.0)])
        assert store.already_notified() == {item.fingerprint: 1184.0}

    def test_recording_the_same_notification_twice_is_safe(self, store):
        store.record_notifications([("ebay:1", 1200.0, 80.0)])
        store.record_notifications([("ebay:1", 1150.0, 82.0)])

        # Upsert, not duplicate — and the newer price wins, which is what a
        # later price-drop comparison should measure against.
        assert store.already_notified() == {"ebay:1": 1150.0}

    def test_dedup_survives_a_restart(self, tmp_path, config, rates):
        path = tmp_path / "persist.db"
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")

        with Store(path) as store:
            store.record_notifications([(item.fingerprint, 1184.0, 88.0)])
        with Store(path) as reopened:
            assert item.fingerprint in reopened.already_notified()

    def test_the_router_suppresses_what_the_store_remembers(self, store, config, rates):
        """The end-to-end property: a listing already notified stays quiet."""
        from dealhunter.notify import route

        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")

        assert route([item], config).alerts, "should alert when unseen"

        store.record_notifications([(item.fingerprint, item.landed.landed_usd, 88.0)])
        assert route([item], config,
                     already_notified=store.already_notified()).alerts == []


# ---------------------------------------------------------------------------
# Price movements
# ---------------------------------------------------------------------------


class TestPriceMovements:
    def test_a_drop_is_detected_against_the_stored_price(self, store, config, rates):
        expensive = scored(config, rates, title_key="helios_neo_16s_openbox",
                           price=1350.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([expensive])

        cheaper = scored(config, rates, title_key="helios_neo_16s_openbox",
                         price=1184.0, jurisdiction="OR", seller_name="Best Buy",
                         listing_id=expensive.listing.listing_id)
        drops, rises = store.price_movements([cheaper])

        assert len(drops) == 1
        assert rises == []
        assert drops[0].previous_usd == 1350.0
        assert drops[0].current_usd == 1184.0
        assert "down $166" in drops[0].summary()

    def test_a_rise_is_detected_too(self, store, config, rates):
        cheap = scored(config, rates, title_key="helios_neo_16s_openbox",
                       price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([cheap])

        dearer = scored(config, rates, title_key="helios_neo_16s_openbox",
                        price=1350.0, jurisdiction="OR", seller_name="Best Buy",
                        listing_id=cheap.listing.listing_id)
        drops, rises = store.price_movements([dearer])

        assert drops == []
        assert len(rises) == 1

    def test_a_first_sighting_is_not_a_movement(self, store, config, rates):
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        assert store.price_movements([item]) == ([], [])

    def test_noise_below_the_threshold_is_ignored(self, store, config, rates):
        first = scored(config, rates, title_key="helios_neo_16s_openbox",
                       price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([first])

        barely = scored(config, rates, title_key="helios_neo_16s_openbox",
                        price=1183.0, jurisdiction="OR", seller_name="Best Buy",
                        listing_id=first.listing.listing_id)
        drops, _rises = store.price_movements([barely])
        assert drops == []


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_listings_not_seen_recently_are_marked_gone(self, store, config, rates):
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([item])

        # Backdate the sighting past the 7-day window.
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        store.connection.execute(
            "UPDATE listings SET last_seen = ?", (old,)
        )
        store.connection.commit()

        gone = store.expire_stale(days=7)
        assert len(gone) == 1
        assert store.stats()["gone"] == 1
        assert store.stats()["active"] == 0

    def test_a_reappearing_listing_is_resurrected(self, store, config, rates):
        """Stock comes back. Seeing it again must flip it active, or it would
        stay invisible forever."""
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([item])
        store.connection.execute("UPDATE listings SET status = 'gone'")
        store.connection.commit()

        store.record_listings([item])
        assert store.stats()["active"] == 1
        assert store.stats()["gone"] == 0

    def test_fresh_listings_are_untouched(self, store, config, rates):
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([item])
        assert store.expire_stale(days=7) == []


# ---------------------------------------------------------------------------
# Price floors
# ---------------------------------------------------------------------------


class TestFloorSeeding:
    def test_config_floors_are_seeded(self, store, config):
        floors = store.floors()
        assert floors["acer_predator_helios_neo_16s_ai"] == 1184.0
        assert floors["lenovo_legion_pro_5_16_83lt000mus"] == 1049.0

    def test_reseeding_does_not_raise_a_verified_low_back_up(self, store, config):
        """The database having driven a floor lower is the whole point."""
        store.connection.execute(
            "UPDATE floors SET floor_usd = 899 WHERE model_key = ?",
            ("lenovo_legion_pro_5_16_83lt000mus",),
        )
        store.connection.commit()

        store.seed_floors(config)
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 899.0

    def test_lowering_the_seed_by_hand_wins(self, store, config):
        """You editing config.yaml downward is an explicit instruction."""
        model = config.model_by_key("acer_predator_helios_neo_16s_ai")
        model.floor_usd = 999.0
        try:
            store.seed_floors(config)
            assert store.floors()["acer_predator_helios_neo_16s_ai"] == 999.0
        finally:
            model.floor_usd = 1184.0


class TestFloorUpdates:
    def test_a_verified_cheaper_listing_lowers_the_floor(self, store, config, rates):
        item = scored(config, rates, title_key="legion_pro_5_oled",
                      price=999.0, jurisdiction="OR",
                      condition=Condition.NEW, seller_name="Best Buy")

        notes = store.update_floors([item])
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 999.0
        assert "new record low" in notes[0]

    def test_a_higher_price_does_not_raise_the_floor(self, store, config, rates):
        item = scored(config, rates, title_key="legion_pro_5_oled",
                      price=1300.0, jurisdiction="OR", seller_name="Best Buy")

        store.update_floors([item])
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 1049.0

    def test_a_community_claim_never_sets_a_floor(self, store, config, rates):
        """A Reddit post claiming $900 is not a price. Letting it become the
        baseline would poison every future score for that model."""
        listing = make_listing("legion_pro_5_oled", price=899.0, jurisdiction="OR",
                               seller_name="somebody", source="reddit:LaptopDeals")
        listing.source_flags.append(Flag.UNVERIFIED_SOURCE)
        item = evaluate(listing, config, rates)

        store.update_floors([item])
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 1049.0

    def test_a_high_risk_listing_never_sets_a_floor(self, store, config, rates):
        item = scored(config, rates, title_key="legion_pro_5_oled", price=899.0,
                      jurisdiction="OR", condition=Condition.USED,
                      seller_name="newbie", feedback=4, percent=100.0)

        assert Flag.HIGH_RISK in item.all_flags
        store.update_floors([item])
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 1049.0

    def test_a_multi_variation_listing_never_sets_a_floor(self, store, config, rates):
        """Its advertised price is the cheapest variant, which may not be the
        config in the title."""
        listing = make_listing("legion_pro_5_oled", price=899.0, jurisdiction="OR",
                               seller_name="Best Buy")
        listing.source_flags.append(Flag.MULTI_VARIATION_LISTING)
        item = evaluate(listing, config, rates)

        store.update_floors([item])
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 1049.0

    def test_a_stale_fx_rate_never_sets_a_floor(self, store, config):
        """An approximate landed figure must not become a permanent record."""
        from dealhunter.fx import FxRates

        stale = FxRates(rates={"USD": 1.0, "CAD": 0.73}, source="config-fallback",
                        fetched_at=datetime.now(timezone.utc), is_stale=True)
        item = evaluate(
            make_listing("legion_pro_5_oled", price=899.0, jurisdiction="OR",
                         seller_name="Best Buy"),
            config, stale,
        )

        assert Flag.FX_STALE in item.all_flags
        store.update_floors([item])
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 1049.0

    def test_a_rejected_listing_never_sets_a_floor(self, store, config, rates):
        rejected = evaluate(
            make_listing("legion_pro_5_oled", region=Region.DE, price=800.0),
            config, rates,
        )
        assert rejected.rejected

        store.update_floors([rejected])
        assert store.floors()["lenovo_legion_pro_5_16_83lt000mus"] == 1049.0

    def test_an_updated_floor_feeds_back_into_scoring(self, store, config, rates):
        """The loop that makes this an agent: today's record low is the
        baseline tomorrow's listings are scored against."""
        from dealhunter.evaluate import evaluate_all

        record = scored(config, rates, title_key="legion_pro_5_oled", price=949.0,
                        jurisdiction="OR", condition=Condition.NEW,
                        seller_name="Best Buy")
        store.update_floors([record])

        later = make_listing("legion_pro_5_oled", price=1049.0, jurisdiction="OR",
                             seller_name="Best Buy", listing_id="later")
        with_floor = evaluate_all([later], config, rates, floors=store.floors())[0]
        without = evaluate_all([later], config, rates)[0]

        # $1,049 was exactly the old floor (+10); against the new $949 floor it
        # is 10.5% over, so it must score lower.
        assert with_floor.score.total < without.score.total


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------


class TestRunBookkeeping:
    def test_digest_timestamp_drives_the_scheduler(self, store, config):
        from dealhunter.notify import should_send_digest

        assert store.last_digest_at() is None

        run_id = store.start_run()
        store.finish_run(run_id, digest_sent=True)

        last = store.last_digest_at()
        assert last is not None
        # Having just sent one, we must not send another today.
        assert should_send_digest(datetime.now(timezone.utc), last, config) is False

    def test_a_run_without_a_digest_does_not_count(self, store):
        run_id = store.start_run()
        store.finish_run(run_id, digest_sent=False)
        assert store.last_digest_at() is None

    def test_discovery_timestamp_is_tracked_separately(self, store):
        run_id = store.start_run()
        store.finish_run(run_id, discovery_run=True)
        assert store.last_discovery_at() is not None

    def test_discovery_does_not_rerun_within_the_month(self, store, config):
        from dealhunter.discovery import SourceDiscovery

        run_id = store.start_run()
        store.finish_run(run_id, discovery_run=True)

        discovery = SourceDiscovery(config)
        assert discovery.should_run(last_run=store.last_discovery_at()) is False


class TestMaintenance:
    def test_pruning_drops_history_for_long_gone_listings(self, store, config, rates):
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([item])

        ancient = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        store.connection.execute(
            "UPDATE listings SET status = 'gone', last_seen = ?", (ancient,)
        )
        store.connection.commit()

        store.prune(keep_days=180)
        assert store.stats()["listings"] == 0
        assert store.stats()["price_points"] == 0

    def test_pruning_leaves_active_listings_alone(self, store, config, rates):
        item = scored(config, rates, title_key="helios_neo_16s_openbox",
                      price=1184.0, jurisdiction="OR", seller_name="Best Buy")
        store.record_listings([item])

        store.prune(keep_days=180)
        assert store.stats()["active"] == 1
