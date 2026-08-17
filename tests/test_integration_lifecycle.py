"""
The multi-run lifecycle.

Everything else tests one run. This tests what happens across four of them,
which is where the agent's actual value lives — and where the failures would be
silent rather than loud:

    run 1  a deal appears           -> alert
    run 2  same deal, same price    -> silence
    run 3  price drops 14%          -> PRICE DROP alert, floor lowered
    run 4  it vanishes for 8 days   -> marked GONE, reported in the digest
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dealhunter.evaluate import evaluate_all
from dealhunter.models import Condition, Flag, Region
from dealhunter.notify import route
from dealhunter.store import Store
from tests.fixtures import make_listing

LISTING_ID = "v1|helios|0"


def helios(price: float):
    """The priority target, at a given price, from Best Buy in Oregon (0% tax)."""
    return make_listing(
        "helios_neo_16s_openbox",
        price=price,
        jurisdiction="OR",
        condition=Condition.OPEN_BOX_EXCELLENT,
        seller_name="Best Buy",
        listing_id=LISTING_ID,
    )


@pytest.fixture
def store(tmp_path, config):
    with Store(tmp_path / "lifecycle.db") as store:
        store.seed_floors(config)
        yield store


def do_run(store, config, rates, listings, *, send_digest=False):
    """One full pass: evaluate, record, route, remember what was sent."""
    evaluated = evaluate_all(listings, config, rates, floors=store.floors())

    drops, rises = store.price_movements(evaluated)
    gone = store.expire_stale(days=7)
    store.record_listings(evaluated)
    floor_notes = store.update_floors(evaluated)

    decision = route(
        evaluated, config,
        already_notified=store.already_notified(),
        send_digest=send_digest,
    )
    if decision.digest is not None:
        decision.digest.price_drops = floor_notes + [m.summary() for m in drops]
        decision.digest.price_rises = [m.summary() for m in rises]
        decision.digest.gone = gone

    alerted_urls = {alert.url for alert in decision.alerts}
    store.record_notifications([
        (item.fingerprint, item.landed.landed_usd, item.score.total)
        for item in evaluated
        if not item.rejected and item.listing.url in alerted_urls
    ])

    run_id = store.start_run()
    store.finish_run(run_id, listings_seen=len(listings),
                     alerts_sent=len(decision.alerts), digest_sent=send_digest)
    return decision, evaluated


class TestLifecycle:
    def test_the_full_four_run_cycle(self, store, config, rates):
        # -- run 1: a new deal appears --------------------------------------
        first, _ = do_run(store, config, rates, [helios(1184.0)])

        assert len(first.alerts) == 1
        assert first.alerts[0].is_priority is True
        assert store.stats()["active"] == 1

        # -- run 2: unchanged. Silence. -------------------------------------
        second, _ = do_run(store, config, rates, [helios(1184.0)])

        assert second.alerts == [], "an unchanged listing must not re-alert"
        assert second.is_silent is True
        # And no duplicate price history for a price that didn't move.
        assert store.stats()["price_points"] == 1

        # -- run 3: it drops 13.9%. Re-alert, and set a record. -------------
        third, _ = do_run(store, config, rates, [helios(1019.0)], send_digest=True)

        assert len(third.alerts) == 1
        banner = third.alerts[0].headline_tag
        # $165 off $1,184 = 13.9%, comfortably over the 5% re-notify threshold.
        assert "PRICE DROP" in banner
        assert "13.9%" in banner
        # All three banners stack: it dropped, it set a record, and it is the
        # priority target under its standing trigger price.
        assert "NEW RECORD LOW" in banner
        assert "PRIORITY" in banner
        assert store.stats()["price_points"] == 2

        # The floor moved with it: $1,019 beats the seeded $1,184.
        assert store.floors()["acer_predator_helios_neo_16s_ai"] == 1019.0
        assert any("new record low" in note for note in third.digest.price_drops)

        # -- run 4: it disappears for 8 days --------------------------------
        store.connection.execute(
            "UPDATE listings SET last_seen = ?",
            ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),),
        )
        store.connection.commit()

        fourth, _ = do_run(store, config, rates, [], send_digest=True)

        assert store.stats()["gone"] == 1
        assert len(fourth.digest.gone) == 1
        assert "Helios Neo" in fourth.digest.gone[0]

    def test_a_lowered_floor_changes_how_later_listings_score(
        self, store, config, rates
    ):
        """The feedback loop that makes this an agent rather than a scraper."""
        do_run(store, config, rates, [helios(1019.0)])
        assert store.floors()["acer_predator_helios_neo_16s_ai"] == 1019.0

        # A listing at the OLD floor is no longer a good deal.
        later = evaluate_all([helios(1184.0)], config, rates, floors=store.floors())[0]
        floor_component = next(
            c for c in later.score.components if c.name == "Price vs floor"
        )
        assert floor_component.points < 0, (
            "at 16% above the new record low this should be penalised, "
            "not rewarded as it was against the seeded floor"
        )

    def test_a_reappearing_listing_comes_back_to_life(self, store, config, rates):
        do_run(store, config, rates, [helios(1184.0)])
        store.connection.execute("UPDATE listings SET status = 'gone'")
        store.connection.commit()

        do_run(store, config, rates, [helios(1184.0)])
        assert store.stats()["active"] == 1
        assert store.stats()["gone"] == 0

    def test_running_the_same_pass_twice_is_idempotent(self, store, config, rates):
        do_run(store, config, rates, [helios(1184.0)])
        before = store.stats()

        do_run(store, config, rates, [helios(1184.0)])
        after = dict(before)
        after["runs"] = before["runs"] + 1     # only the run counter should move

        assert store.stats() == after

    def test_a_small_wobble_does_not_re_alert(self, store, config, rates):
        """Down 2% is noise. Only a drop over the configured 5% is news."""
        do_run(store, config, rates, [helios(1184.0)])
        second, _ = do_run(store, config, rates, [helios(1160.0)])
        assert second.alerts == []

    def test_a_community_claim_cannot_move_the_floor_across_runs(
        self, store, config, rates
    ):
        """A Reddit post claiming $700 must not become the permanent baseline
        that every future Helios Neo is scored against."""
        claim = helios(700.0)
        claim.source = "reddit:LaptopDeals"
        claim.listing_id = "reddit-abc"
        claim.source_flags.append(Flag.UNVERIFIED_SOURCE)

        do_run(store, config, rates, [claim])
        assert store.floors()["acer_predator_helios_neo_16s_ai"] == 1184.0

    def test_the_digest_scheduler_reads_back_from_the_store(self, store, config, rates):
        from dealhunter.notify import should_send_digest

        do_run(store, config, rates, [helios(1184.0)], send_digest=True)

        # Having just sent one, we must not send another today.
        assert should_send_digest(
            datetime.now(timezone.utc), store.last_digest_at(), config
        ) is False

    def test_state_survives_a_process_restart(self, tmp_path, config, rates):
        """The property GitHub Actions depends on: the database is the only
        thing carried between runs, so it has to hold everything."""
        path = tmp_path / "restart.db"

        with Store(path) as first:
            first.seed_floors(config)
            decision, _ = do_run(first, config, rates, [helios(1184.0)])
            assert len(decision.alerts) == 1

        # New process, same file.
        with Store(path) as second:
            second.seed_floors(config)
            decision, _ = do_run(second, config, rates, [helios(1184.0)])
            assert decision.alerts == [], "dedup must survive a restart"


class TestCrossRegionLifecycle:
    def test_the_cheapest_landed_listing_wins_across_regions(
        self, store, config, rates
    ):
        """Three listings, three currencies, one honest ranking."""
        listings = [
            helios(1184.0),
            make_listing("legion_pro_5_oled", region=Region.CA, price=1499.0,
                         jurisdiction="AB", condition=Condition.OPEN_BOX_EXCELLENT,
                         seller_name="Best Buy Canada", listing_id="ca-1"),
            make_listing("uk_listing", region=Region.GB, price=899.0,
                         condition=Condition.MFR_CERTIFIED_REFURB,
                         seller_name="lenovo_certified", listing_id="gb-1"),
        ]
        _decision, evaluated = do_run(store, config, rates, listings,
                                      send_digest=True)

        kept = [e for e in evaluated if not e.rejected]
        assert len(kept) == 3

        by_landed = sorted(kept, key=lambda e: e.landed.landed_usd)
        # C$1,499 in Alberta ($1,149) beats £899 in the UK ($1,176), despite
        # the bigger sticker number — which is the entire point of the system.
        assert by_landed[0].listing.listing_id == "ca-1"
        assert by_landed[0].landed.landed_usd == pytest.approx(1148.98, abs=0.02)

    def test_every_stored_listing_keeps_the_fx_rate_it_was_priced_with(
        self, store, config, rates
    ):
        do_run(store, config, rates, [
            make_listing("legion_pro_5_oled", region=Region.CA, price=1499.0,
                         jurisdiction="AB", seller_name="Best Buy Canada",
                         listing_id="ca-1"),
        ])

        row = store.connection.execute(
            "SELECT fx_rate, fx_source, fx_fetched_at FROM listings"
        ).fetchone()
        assert row["fx_rate"] == pytest.approx(0.73)
        assert row["fx_source"] == "test-fixture"
        assert row["fx_fetched_at"] is not None
