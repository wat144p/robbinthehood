"""
Populate a demo database so the dashboard is usable before any API keys land.

    python seed_demo.py
    python dashboard.py --db data/demo.db

Writes to **data/demo.db**, never to the real data/deals.db. Keeping them
separate matters: these listings are invented, and letting them touch the real
database would poison the price floors that every future score is measured
against.

It simulates six sweeps over two days so the things you actually want to look
at exist — new arrivals, price drops, a listing that vanished, varying
freshness, and cross-region comparisons where the cheapest sticker is not the
cheapest landed cost.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dealhunter import load_config                    # noqa: E402
from dealhunter.evaluate import evaluate_all          # noqa: E402
from dealhunter.fx import static_rates                # noqa: E402
from dealhunter.models import Condition, Listing, Region  # noqa: E402
from dealhunter.regions import prime_keyboard_defaults    # noqa: E402
from dealhunter.store import Store                    # noqa: E402

DEMO_DB = Path("data/demo.db")

RATES = static_rates(
    {"CAD": 0.73, "GBP": 1.27, "EUR": 1.09, "SEK": 0.095, "AUD": 0.66},
    source="demo-fixed",
)


def listing(
    listing_id: str, title: str, price: float, *,
    region: Region = Region.US, currency: str = "USD",
    condition: Condition = Condition.NEW, seller: str = "DEMO Best Buy",
    jurisdiction: str | None = "OR", source: str = "demo:ebay",
    feedback: int | None = None, percent: float | None = None,
    major: bool = True,
) -> Listing:
    return Listing(
        source=source, listing_id=listing_id, title=title,
        url=f"https://example.invalid/demo/{listing_id}",
        region=region, currency=currency, sticker_price_local=price,
        condition=condition, seller_name=seller, jurisdiction=jurisdiction,
        seller_feedback_count=feedback, seller_feedback_percent=percent,
        is_major_retailer=major,
        warranty_note="DEMO DATA — not a real listing.",
    )


# Each entry is (sweep_index, listings visible in that sweep). A listing that
# stops appearing eventually gets marked gone, which is what we want to show.
def sweeps() -> list[list[Listing]]:
    helios = ("Acer Predator Helios Neo 16S AI 16\" 2560x1600 240Hz OLED G-SYNC "
              "500nit Core Ultra 9 275HX RTX 5070 Ti 12GB 140W 32GB DDR5 1TB SSD")
    legion_pro = ("Lenovo Legion Pro 5 16 83LT000MUS 16\" WQXGA OLED 165Hz 500nit "
                  "Ryzen 7 8745HX RTX 5060 8GB @115W 32GB 1TB SSD")
    legion_7i = ("Lenovo Legion 7i 16 16\" 2.5K OLED 165Hz Core Ultra 7 255HX "
                 "RTX 5060 8GB 32GB 1TB SSD")
    vector = ("MSI Vector 16 HX 16\" QHD+ 240Hz IPS Ryzen 9 7945HX "
              "RTX 5070 Ti 12GB 140W TGP 32GB DDR5 1TB NVMe")
    aero = ("GIGABYTE Aero X16 16\" 2560x1600 165Hz IPS 400nit Ryzen AI 7 350 "
            "RTX 5070 32GB 1TB SSD")
    legion_5i = ("Lenovo Legion 5i 15.1\" 2560x1600 OLED 165Hz Ultra 7 255HX "
                 "RTX 5060 32GB 1TB")

    def core(helios_price: float, legion_ca_price: float, uk_price: float):
        """The listings present in most sweeps, at the given prices."""
        return [
            listing("bb-helios-ob", helios + " - Open Box", helios_price,
                    condition=Condition.OPEN_BOX_EXCELLENT,
                    seller="DEMO Best Buy", jurisdiction="OR",
                    source="demo:bestbuy"),
            listing("ca-legionpro", legion_pro, legion_ca_price,
                    region=Region.CA, currency="CAD", jurisdiction="AB",
                    condition=Condition.OPEN_BOX_EXCELLENT,
                    seller="DEMO Best Buy Canada", source="demo:bestbuy_ca"),
            listing("gb-legion7i", legion_7i, uk_price,
                    region=Region.GB, currency="GBP", jurisdiction=None,
                    condition=Condition.MFR_CERTIFIED_REFURB,
                    seller="DEMO lenovo_certified", source="demo:ebay",
                    feedback=48000, percent=99.3),
            listing("us-vector", vector, 1379.0,
                    condition=Condition.NEW, seller="DEMO Newegg",
                    jurisdiction="DE", source="demo:ebay"),
        ]

    # -- sweep 1, two days ago: the baseline --------------------------------
    s1 = core(1349.0, 1649.0, 1049.0) + [
        listing("us-aero", aero, 1215.0,
                condition=Condition.OPEN_BOX_GOOD, seller="DEMO gigabyte_outlet",
                jurisdiction="NH", source="demo:ebay",
                feedback=22000, percent=99.1, major=False),
        # This one disappears after sweep 2 — it sold.
        listing("us-soldout", legion_5i, 1179.0,
                condition=Condition.OPEN_BOX_EXCELLENT, seller="DEMO Best Buy",
                jurisdiction="MT", source="demo:bestbuy"),
    ]

    s2 = core(1349.0, 1649.0, 1029.0) + [
        listing("us-aero", aero, 1215.0,
                condition=Condition.OPEN_BOX_GOOD, seller="DEMO gigabyte_outlet",
                jurisdiction="NH", source="demo:ebay",
                feedback=22000, percent=99.1, major=False),
        listing("us-soldout", legion_5i, 1179.0,
                condition=Condition.OPEN_BOX_EXCELLENT, seller="DEMO Best Buy",
                jurisdiction="MT", source="demo:bestbuy"),
    ]

    # -- sweep 3: the Canadian one drops, a community post shows up ---------
    s3 = core(1349.0, 1549.0, 1029.0) + [
        listing("us-aero", aero, 1189.0,
                condition=Condition.OPEN_BOX_GOOD, seller="DEMO gigabyte_outlet",
                jurisdiction="NH", source="demo:ebay",
                feedback=22000, percent=99.1, major=False),
        listing("rd-claim", "[$999] " + legion_pro + " @ DEMO retailer", 999.0,
                condition=Condition.UNKNOWN, seller="", jurisdiction=None,
                source="demo:reddit", major=False),
    ]

    s4 = core(1289.0, 1549.0, 1029.0) + [
        listing("us-aero", aero, 1189.0,
                condition=Condition.OPEN_BOX_GOOD, seller="DEMO gigabyte_outlet",
                jurisdiction="NH", source="demo:ebay",
                feedback=22000, percent=99.1, major=False),
    ]

    # -- sweep 5: a risky private seller appears ---------------------------
    s5 = core(1289.0, 1499.0, 1039.0) + [
        listing("us-aero", aero, 1189.0,
                condition=Condition.OPEN_BOX_GOOD, seller="DEMO gigabyte_outlet",
                jurisdiction="NH", source="demo:ebay",
                feedback=22000, percent=99.1, major=False),
        listing("us-risky", helios + " READ - no battery", 949.0,
                condition=Condition.USED, seller="DEMO quickflip",
                jurisdiction="MT", source="demo:ebay",
                feedback=31, percent=96.5, major=False),
    ]

    # -- sweep 6, just now: the priority target drops under its trigger -----
    s6 = core(1184.0, 1499.0, 1039.0) + [
        listing("us-aero", aero, 1189.0,
                condition=Condition.OPEN_BOX_GOOD, seller="DEMO gigabyte_outlet",
                jurisdiction="NH", source="demo:ebay",
                feedback=22000, percent=99.1, major=False),
        listing("us-risky", helios + " READ - no battery", 949.0,
                condition=Condition.USED, seller="DEMO quickflip",
                jurisdiction="MT", source="demo:ebay",
                feedback=31, percent=96.5, major=False),
        # A brand-new arrival in the most recent sweep.
        listing("us-newarrival", vector.replace("32GB", "32GB") + " - Open Box",
                1249.0, condition=Condition.OPEN_BOX_EXCELLENT,
                seller="DEMO Best Buy", jurisdiction="DE", source="demo:bestbuy"),
    ]

    return [s1, s2, s3, s4, s5, s6]


def main() -> None:
    config = load_config()
    prime_keyboard_defaults(config)

    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    for sidecar in ("-wal", "-shm"):
        Path(str(DEMO_DB) + sidecar).unlink(missing_ok=True)

    all_sweeps = sweeps()
    # Space the sweeps 8 hours apart, ending now, so freshness grading and the
    # "what changed" windows have something real to work with.
    now = datetime.now(timezone.utc)
    times = [now - timedelta(hours=8 * (len(all_sweeps) - 1 - i))
             for i in range(len(all_sweeps))]

    with Store(DEMO_DB) as store:
        store.seed_floors(config)

        for index, (listings, moment) in enumerate(zip(all_sweeps, times), start=1):
            evaluated = evaluate_all(listings, config, RATES, floors=store.floors())
            run_id = store.start_run()
            store.record_listings(evaluated, config)
            store.update_floors(evaluated)

            # Backdate this sweep's rows so the timeline looks real.
            stamp = moment.isoformat()
            fingerprints = [e.fingerprint for e in evaluated]
            placeholders = ",".join("?" * len(fingerprints))
            store.connection.execute(
                f"UPDATE listings SET last_seen = ? WHERE fingerprint IN ({placeholders})",
                [stamp, *fingerprints],
            )
            if index == 1:
                store.connection.execute(
                    f"UPDATE listings SET first_seen = ? "
                    f"WHERE fingerprint IN ({placeholders})",
                    [stamp, *fingerprints],
                )
            store.connection.execute(
                "UPDATE price_history SET seen_at = ? WHERE seen_at > ?",
                (stamp, stamp),
            )
            store.connection.execute(
                "UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (stamp, stamp, run_id),
            )
            store.connection.commit()

            store.finish_run(run_id, listings_seen=len(listings),
                             alerts_sent=1 if index == len(all_sweeps) else 0)
            store.connection.execute(
                "UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (stamp, stamp, run_id),
            )
            store.connection.commit()

            kept = [e for e in evaluated if not e.rejected]
            print(f"  sweep {index}/{len(all_sweeps)}  {moment:%Y-%m-%d %H:%M}Z  "
                  f"{len(listings)} listings, {len(kept)} passed filters")

        # The Legion 5i stopped appearing after sweep 2 — age it out.
        store.expire_stale(days=7)
        store.connection.execute(
            "UPDATE listings SET status = 'gone' WHERE fingerprint LIKE '%us-soldout'"
        )
        store.connection.commit()

        stats = store.stats()

    print(f"\nWrote {DEMO_DB}")
    print(f"  {stats['listings']} listings, {stats['price_points']} price points, "
          f"{stats['runs']} sweeps")
    print("\nNow run:")
    print(f"  python dashboard.py --db {DEMO_DB}")


if __name__ == "__main__":
    main()
