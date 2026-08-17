"""
Source modules.

Each source is independently enable-able from `config.yaml` under `sources:`,
and each one produces plain `Listing` objects through the same `Source`
interface. Adding a source means writing one class and one config block —
nothing downstream changes.

Registered so far:

    ebay      eBay Browse API — five marketplaces through one integration
    bestbuy   Best Buy Developer API (US), including open-box condition tiers
    rss       generic RSS/Atom deal feeds — OzBargain, HotUKDeals, mydealz…
    reddit    Reddit's free JSON API — r/LaptopDeals and friends
    html      config-driven scraper for trackers and retailers

The order matters slightly: API sources run first because they are cheap,
reliable and return structured data. Scrapers run last, so if the request
budget runs out it is the least trustworthy source that gets truncated.
"""

from __future__ import annotations

from ..config import Config
from ..fx import FxRates
from .base import (
    HttpPolicy,
    Source,
    SourceAuthError,
    SourceBlocked,
    SourceError,
    SourceResult,
    collect_listings,
    run_sources,
)
from .bestbuy import BestBuySource
from .ebay import EbaySource
from .html import HtmlSource
from .reddit import RedditSource
from .rss import RssSource

__all__ = [
    "HttpPolicy",
    "Source",
    "SourceAuthError",
    "SourceBlocked",
    "SourceError",
    "SourceResult",
    "BestBuySource",
    "EbaySource",
    "HtmlSource",
    "RedditSource",
    "RssSource",
    "build_sources",
    "collect_listings",
    "run_sources",
]


def build_sources(
    config: Config, rates: FxRates, only: list[str] | None = None
) -> list[Source]:
    """Instantiate every source the config has enabled.

    `only` restricts the run to named sources, which is what `--source ebay`
    on the CLI uses for debugging a single integration.
    """
    available: list[Source] = [
        # Structured APIs first — cheap, reliable, and they give us seller
        # feedback and condition tiers that the scrapers can only guess at.
        EbaySource(config, rates),
        BestBuySource(config),
        # Community feeds: high signal, but every listing is an unverified
        # claim until a retailer page confirms it.
        RssSource(config),
        RedditSource(config),
        # Scrapers last, so a budget overrun truncates the least reliable tier.
        HtmlSource(config),
    ]

    if only:
        wanted = {name.lower() for name in only}
        available = [source for source in available if source.name in wanted]

    return available
