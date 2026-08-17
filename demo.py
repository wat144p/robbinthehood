"""
Offline demo — drives the real eBay source against recorded-shape payloads.

    python demo.py

No credentials, no network. This runs the genuine stage-2 pipeline (OAuth flow,
filter construction, response mapping, parsing, landed-cost, scoring, ranking)
with only the HTTP socket faked, so what you see is what a real run produces.

It reuses the fake session from the test fixtures rather than duplicating it —
if the eBay response shape changes, there is exactly one place to update.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dealhunter import load_config                                  # noqa: E402
from dealhunter.evaluate import evaluate_all                        # noqa: E402
from dealhunter.fx import static_rates                              # noqa: E402
from dealhunter.notify import ConsoleNotifier, dispatch, route      # noqa: E402
from dealhunter.regions import prime_keyboard_defaults              # noqa: E402
from dealhunter.sources.base import collect_listings, run_sources   # noqa: E402
from dealhunter.sources.ebay import EbaySource                      # noqa: E402
from tests.fixtures_ebay import FakeSession, item, search_response   # noqa: E402

# Fixed rates so the output is reproducible. A real run pulls these live from
# exchangerate.host or the ECB feed and caches them for 12 hours.
DEMO_RATES = static_rates(
    {"CAD": 0.73, "GBP": 1.27, "EUR": 1.09, "SEK": 0.095, "AUD": 0.66},
    source="demo-fixed",
)

PAYLOADS = {
    "EBAY_US": search_response([
        item(item_id="v1|1001|0", price="1184.00", condition_id="1500",
             seller="bestbuy", feedback_score=1250000, feedback_percent="98.9",
             postal="19801",   # Delaware — 0% sales tax
             title="Acer Predator Helios Neo 16S AI 16\" 2560x1600 240Hz OLED "
                   "G-SYNC 500nit Core Ultra 9 275HX RTX 5070 Ti 12GB 140W "
                   "32GB DDR5 1TB SSD"),
        item(item_id="v1|1002|0", price="1015.00", condition_id="2010",
             seller="gigabyte_outlet", feedback_score=22000,
             feedback_percent="99.1", postal="97035",   # Oregon — 0%
             title="GIGABYTE Aero X16 16\" 2560x1600 165Hz IPS 400nit "
                   "Ryzen AI 7 350 RTX 5070 32GB 1TB SSD"),
        item(item_id="v1|1003|0", price="1290.00", condition_id="3000",
             seller="quickflip_deals", feedback_score=41, feedback_percent="97.2",
             postal="59001",   # Montana — 0%
             title="HP OMEN MAX 16 16\" 2560x1600 240Hz OLED Ryzen 9 "
                   "RTX 5070 Ti 12GB 32GB 2TB Storage "
                   "(1TB SSD & 1TB Docking Station)"),
        item(item_id="v1|1004|0", price="899.00", condition_id="3000",
             seller="partsbin", feedback_score=3200, feedback_percent="99.0",
             postal="10001",
             title="HP Omen 16 16.1\" WUXGA 1920x1200 144Hz Core i7-14700HX "
                   "RTX 4060 8GB 16GB 1TB SSD"),
    ]),
    "EBAY_CA": search_response([
        item(item_id="v1|2001|0", price="1499.00", currency="CAD",
             condition_id="1500", seller="bestbuy_canada", feedback_score=88000,
             feedback_percent="98.2", country="CA", postal="T2P 1J9",  # Alberta
             title="Lenovo Legion Pro 5 16 83LT000MUS 16\" WQXGA OLED 165Hz "
                   "500nit Ryzen 7 8745HX RTX 5060 8GB @115W 32GB 1TB SSD"),
        item(item_id="v1|2002|0", price="1499.00", currency="CAD",
             condition_id="1500", seller="bestbuy_canada", feedback_score=88000,
             feedback_percent="98.2", country="CA", postal="M5V 3L9",  # Ontario
             title="Lenovo Legion Pro 5 16 83LT000MUS 16\" WQXGA OLED 165Hz "
                   "500nit Ryzen 7 8745HX RTX 5060 8GB @115W 32GB 1TB SSD"),
    ]),
    "EBAY_GB": search_response([
        item(item_id="v1|3001|0", price="899.00", currency="GBP",
             condition_id="2000", seller="lenovo_certified", feedback_score=48000,
             feedback_percent="99.3", country="GB", postal="M1 1AA",
             title="Lenovo Legion 7i 16 16\" 2.5K OLED 165Hz Core Ultra 7 255HX "
                   "RTX 5060 8GB 32GB 1TB SSD"),
    ]),
    "EBAY_DE": search_response([
        item(item_id="v1|4001|0", price="1049.00", currency="EUR",
             condition_id="1000", seller="notebooksbilliger", feedback_score=51000,
             feedback_percent="99.5", country="DE", postal="10115",
             title="Lenovo Legion Pro 5 16 WQXGA OLED 165Hz Ryzen 7 8745HX "
                   "RTX 5060 8GB 32GB 1TB SSD QWERTZ Tastatur"),
    ]),
}


def main() -> None:
    config = load_config()
    prime_keyboard_defaults(config)

    source = EbaySource(
        config, DEMO_RATES,
        session=FakeSession(PAYLOADS),
        client_id="demo", client_secret="demo",
        sleep=lambda _seconds: None,      # don't actually wait in a demo
    )

    results = run_sources([source])
    listings = collect_listings(results)
    evaluated = evaluate_all(listings, config, DEMO_RATES)

    rejected = [e for e in evaluated if e.rejected]

    print("=" * 78)
    print("SOURCES")
    print("=" * 78)
    for result in results:
        print(f"  {result.summary()}")
    print()

    # Route exactly as a real run would, then send through the console channel.
    # `send_digest=True` forces the daily summary so the demo shows both paths;
    # a real run only sends it after 09:00 PKT.
    decision = route(
        evaluated,
        config,
        already_notified={},
        send_digest=True,
        failed_sources=[r.summary() for r in results if not r.ok],
    )
    dispatch([ConsoleNotifier()], decision.alerts, decision.digest)

    print("=" * 78)
    print("REJECTED  (logged, never notified)")
    print("=" * 78)
    for evaluated_listing in rejected:
        reasons = ", ".join(r.value for r in evaluated_listing.reject_reasons)
        print(f"  [{reasons}]")
        print(f"      {evaluated_listing.listing.title[:70]}...")


if __name__ == "__main__":
    main()
