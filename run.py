"""
robbin-the-hood — command line entry point.

    python run.py --once --dry-run           one pass, print instead of sending
    python run.py --once                     one pass, send to configured channels
    python run.py --once --source ebay       just the eBay integration
    python run.py --once --offline           cached/fallback FX, no FX calls
    python run.py --once --force-digest      send the digest regardless of time

Stage 3 status: sources fetch, listings are scored and ranked, and anything
worth seeing goes out over Discord and/or ntfy. Persistence (stage 5) plugs
into the two marked points below — until then every run treats every listing as
newly seen, so re-running will re-alert.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dealhunter import load_config
from dealhunter.discovery import SourceDiscovery
from dealhunter.evaluate import evaluate_all
from dealhunter.fx import FxService
from dealhunter.models import EvaluatedListing, RejectReason
from dealhunter.notify import build_notifiers, dispatch, route, should_send_digest
from dealhunter.store import Store

log = logging.getLogger("robbin")


def load_dotenv(path: str = ".env") -> None:
    """Load `KEY=value` lines from a .env file into the environment.

    Ten lines instead of a dependency. Existing environment variables always
    win, so CI secrets are never overwritten by a stray local file.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="robbin-the-hood",
        description="Hunt gaming laptop deals across seven regions.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single pass and exit (the only mode until stage 5).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be sent instead of sending it.",
    )
    parser.add_argument(
        "--source", action="append", metavar="NAME",
        help="Only run the named source. Repeatable.",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Skip live FX lookups and use the cache or config fallbacks.",
    )
    parser.add_argument(
        "--force-digest", action="store_true",
        help="Send the daily digest regardless of the time of day.",
    )
    parser.add_argument(
        "--config", default=None, metavar="PATH",
        help="Path to config.yaml (defaults to the one beside this script).",
    )
    parser.add_argument(
        "--show-rejected", action="store_true",
        help="Print what was filtered out, and why.",
    )
    parser.add_argument(
        "--probe", metavar="SITE",
        help="Fetch one page from an sources.html site and report what its "
             "configured selectors actually match. Run this before enabling a "
             "site — it is the only honest way to know the selectors work.",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Run the source-discovery pass now instead of waiting for the "
             "first run of the month.",
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="Path to the SQLite database (default: data/deals.db).",
    )
    parser.add_argument(
        "--ignore-state", action="store_true",
        help="Ignore the already-notified table, so everything looks new. "
             "Useful for previewing alerts with --dry-run.",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print database statistics and exit.",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="Drop history for long-gone listings and compact the database. "
             "Worth running before committing the DB back to the repo.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    # Imported here so `--help` doesn't pay for the source machinery.
    from dealhunter.sources import build_sources, collect_listings, run_sources
    from dealhunter.sources.html import HtmlSource

    config = load_config(args.config)

    # --probe is a diagnostic, not a run. It fetches one page and reports what
    # the selectors match, then exits without touching sources or notifiers.
    if args.probe:
        print(HtmlSource(config).probe(args.probe))
        return 0

    # -- state ---------------------------------------------------------------
    # Everything that makes this an agent rather than a scraper: what we've
    # already told you about, what prices have done, and the record lows.
    store = Store.from_config(config, args.db)
    store.seed_floors(config)
    run_id = store.start_run()

    if args.stats:
        for key, value in store.stats().items():
            print(f"  {key:<14} {value:,}")
        store.close()
        return 0

    # -- FX ------------------------------------------------------------------
    fx_service = FxService.from_config(config)
    rates = fx_service.get_rates(offline=args.offline)
    fx_note = ""
    if rates.is_stale:
        fx_note = (
            f"FX rates are stale (source: {rates.source}, fetched "
            f"{rates.fetched_at:%Y-%m-%d %H:%M UTC}) — landed figures are approximate."
        )
        log.warning("%s", fx_note)

    # -- fetch ---------------------------------------------------------------
    sources = build_sources(config, rates, only=args.source)
    if not sources:
        print("No sources matched. Check --source and the config.", file=sys.stderr)
        return 2

    source_results = run_sources(sources)
    listings = collect_listings(source_results)

    # -- monthly source discovery -------------------------------------------
    # Candidates are written to discovered_sources.yaml and surfaced in the
    # digest for your approval — nothing is ever auto-enabled.
    pending_sources: list[str] = []
    discovery = SourceDiscovery(config)
    discovery_ran = False
    if args.discover or discovery.should_run(last_run=store.last_discovery_at()):
        discovery_ran = True
        try:
            pending_sources = [c.summary() for c in discovery.run()]
            log.info("Discovery pass found %d candidate(s)", len(pending_sources))
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            log.warning("Source discovery failed: %s", exc)

    # -- evaluate ------------------------------------------------------------
    # Floors come from the database, which may hold a verified low below the
    # config seed.
    evaluated = evaluate_all(listings, config, rates, floors=store.floors())
    kept = [item for item in evaluated if not item.rejected]
    rejected = [item for item in evaluated if item.rejected]

    # Read price movements BEFORE recording, while the stored value is still
    # the previous one.
    price_drops, price_rises = store.price_movements(evaluated)
    gone = store.expire_stale(days=int(config.notification.get("expire_days", 7)))

    store.record_listings(evaluated)
    floor_updates = store.update_floors(evaluated)
    for note in floor_updates:
        log.info("Floor updated: %s", note)

    # -- route ---------------------------------------------------------------
    send_digest = args.force_digest or should_send_digest(
        datetime.now(timezone.utc), store.last_digest_at(), config
    )

    decision = route(
        evaluated,
        config,
        already_notified={} if args.ignore_state else store.already_notified(),
        send_digest=send_digest,
        failed_sources=[r.summary() for r in source_results if not r.ok],
        pending_source_approvals=pending_sources,
        fx_note=fx_note,
    )

    if decision.digest is not None:
        decision.digest.price_drops = floor_updates + [m.summary() for m in price_drops]
        decision.digest.price_rises = [m.summary() for m in price_rises]
        decision.digest.gone = gone

    # -- report to the console -----------------------------------------------
    print("=" * 78)
    print(f"SOURCES  ({len(listings)} listings fetched)")
    print("=" * 78)
    for result in source_results:
        print(f"  {result.summary()}")
    print(
        f"\n{len(kept)} passed filters, {len(rejected)} rejected. "
        f"{len(decision.alerts)} alert(s)"
        + (", digest included" if send_digest else ", no digest this run")
        + "."
    )
    print()

    # -- send ----------------------------------------------------------------
    notifiers = build_notifiers(config, dry_run=args.dry_run)

    if decision.is_silent:
        # "Notify only when there's something worth seeing." Nothing cleared
        # the bar, so nothing is sent — not even a "nothing found" message.
        print("Nothing cleared the notification bar. Staying silent.")
    else:
        notify_results = dispatch(notifiers, decision.alerts, decision.digest)
        print("─" * 78)
        for result in notify_results:
            print(f"  {result.summary()}")

        failed_channels = [r for r in notify_results if not r.ok]
        if failed_channels:
            print("\n! Notification failures:", file=sys.stderr)
            for result in failed_channels:
                print(f"    {result.summary()}", file=sys.stderr)

        # Remember what we sent, but only if a real channel took it. A dry run
        # must not mark listings as notified — otherwise the first live run
        # would go silent about everything you just previewed.
        if not args.dry_run and any(r.sent for r in notify_results):
            alerted = {alert.url: alert for alert in decision.alerts}
            store.record_notifications([
                (item.fingerprint, item.landed.landed_usd, item.score.total)
                for item in kept
                if item.listing.url in alerted
            ])

    if args.show_rejected:
        _print_rejections(rejected)

    failed_sources = [r for r in source_results if not r.ok]
    if failed_sources:
        print("\n! Source failures this run:", file=sys.stderr)
        for result in failed_sources:
            print(f"    {result.summary()}", file=sys.stderr)

    # -- close out the run ---------------------------------------------------
    store.finish_run(
        run_id,
        listings_seen=len(listings),
        alerts_sent=len(decision.alerts),
        # Only claim the digest was sent if it actually went somewhere real,
        # or the 09:00 PKT scheduler would skip tomorrow's after a dry run.
        digest_sent=bool(
            send_digest and decision.digest and not decision.digest.is_empty
            and not args.dry_run
        ),
        discovery_run=discovery_ran and not args.dry_run,
        notes="; ".join(r.summary() for r in failed_sources)[:1000],
    )

    if args.prune:
        removed = store.prune()
        store.vacuum()
        print(f"\nPruned {removed} old price points and compacted the database.")

    store.close()

    # Exit non-zero only when every source failed — a partial run is a success,
    # which is the whole point of isolating source failures.
    return 1 if source_results and all(not r.ok for r in source_results) else 0


def _print_rejections(rejected: list[EvaluatedListing]) -> None:
    print("\n" + "=" * 78)
    print("REJECTED")
    print("=" * 78)
    for item in rejected:
        reasons = ", ".join(r.value for r in item.reject_reasons)
        print(f"  [{reasons}] {item.listing.title[:80]}")

    counts: dict[RejectReason, int] = {}
    for item in rejected:
        for reason in item.reject_reasons:
            counts[reason] = counts.get(reason, 0) + 1

    if counts:
        # Worth watching: a spike in UNPARSEABLE means the parser needs work,
        # not that the filters need loosening.
        print("\n  by reason:")
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {count:4d}  {reason.value}")


if __name__ == "__main__":
    sys.exit(main())
