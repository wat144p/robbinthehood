"""
Generic RSS / Atom deal-feed source.

One tested module covers every regional deal community, because they all
publish standards-compliant feeds:

    OzBargain     AU   confirmed RSS 2.0 + <ozb:meta url=…> destination link
    HotUKDeals    GB   confirmed RSS 2.0 + <pepper:merchant price=… name=…>
    mydealz.de    DE   same Pepper platform as HotUKDeals
    RedFlagDeals  CA   see the note in config.yaml — now bot-gated
    Slickdeals    US   see the note in config.yaml — now 403s non-browsers

Two structured extras are worth exploiting where a feed provides them, because
they beat parsing a human-written title every time:

* **Pepper platform** (HotUKDeals, mydealz and friends) emits
  ``<pepper:merchant name="Argos" price="£65"/>`` — the price and the retailer,
  already separated.
* **OzBargain** emits ``<ozb:meta url="https://store…"/>`` — the actual
  destination, rather than the forum thread the ``<link>`` points at.

Everything else falls back to pulling a price out of the title, which is what
`pricing.py` exists for.

These feeds are **all-category** — the same OzBargain feed that carries a
laptop deal also carries dartboards and dog food. The keyword filter is not an
optimisation, it is load-bearing.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..config import Config
from ..models import Condition, Flag, Listing, Region
from ..pricing import extract_price
from ..robots import RobotsCache
from .base import HttpPolicy, RequestBudget, Source, SourceBlocked, SourceError

log = logging.getLogger(__name__)


@dataclass
class FeedConfig:
    """One feed's settings, from `sources.rss.feeds` in config.yaml."""

    name: str
    url: str
    region: Region
    currency: str
    enabled: bool = True
    max_age_hours: int = 48
    keyword_pattern: str | None = None
    category_pattern: str | None = None

    @classmethod
    def from_dict(cls, name: str, block: dict) -> "FeedConfig":
        return cls(
            name=name,
            url=block["url"],
            region=Region(block["region"]),
            currency=block["currency"],
            enabled=bool(block.get("enabled", True)),
            max_age_hours=int(block.get("max_age_hours", 48)),
            keyword_pattern=block.get("keyword_pattern"),
            category_pattern=block.get("category_pattern"),
        )


class RssSource(Source):
    """Reads deal posts from configured RSS/Atom feeds.

    Every listing produced here carries `UNVERIFIED_SOURCE`: a community post
    is a claim about a price, not the price itself. Per the affiliate-bias
    rule, nothing here is trusted until a retailer page confirms it.
    """

    name = "rss"

    def __init__(self, config: Config, session=None, sleep=time.sleep):
        super().__init__(config)
        self.settings = (config.raw_sources or {}).get("rss") or {}
        self.policy = HttpPolicy.from_config(config)
        self.budget = RequestBudget(self.policy, sleep=sleep)
        self.requests_made = 0
        self._session = session
        self.robots = RobotsCache(user_agent=self.policy.user_agent, session=session)
        self.feed_errors: list[str] = []

    @property
    def regions(self) -> tuple[Region, ...]:  # type: ignore[override]
        return tuple({feed.region for feed in self._feeds()})

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.policy.user_agent})
        return self._session

    def _feeds(self) -> list[FeedConfig]:
        return [
            FeedConfig.from_dict(name, block)
            for name, block in (self.settings.get("feeds") or {}).items()
        ]

    # -- entry point -------------------------------------------------------

    def fetch(self) -> list[Listing]:
        enabled_regions = set(self.config.enabled_regions())
        listings: list[Listing] = []

        for feed in self._feeds():
            if not feed.enabled:
                continue
            if feed.region not in enabled_regions:
                log.debug("Skipping feed %s — region %s disabled", feed.name, feed.region)
                continue
            if self.budget.exhausted:
                log.warning("RSS request budget exhausted; stopping early")
                break

            try:
                listings.extend(self._fetch_feed(feed))
            except SourceBlocked as exc:
                # One feed being bot-gated must not cost us the other four.
                # Recorded so the digest can tell you which one to look at.
                self.feed_errors.append(f"{feed.name}: {exc}")
                log.warning("Feed %s is blocked: %s", feed.name, exc)
            except Exception as exc:  # noqa: BLE001
                self.feed_errors.append(f"{feed.name}: {type(exc).__name__}: {exc}")
                log.warning("Feed %s failed: %s", feed.name, exc)

        return listings

    def _fetch_feed(self, feed: FeedConfig) -> list[Listing]:
        if not self.robots.is_allowed(feed.url):
            raise SourceBlocked(f"robots.txt disallows {feed.url}")

        # Honour the site's own Crawl-delay when it's longer than ours.
        published_delay = self.robots.crawl_delay(feed.url)
        if published_delay and published_delay > self.policy.request_delay_seconds:
            self.budget._sleep(published_delay)

        self.budget.wait_turn()
        self.requests_made += 1
        response = self.session.get(
            feed.url,
            headers={"User-Agent": self.policy.user_agent},
            timeout=self.policy.timeout_seconds,
        )

        if response.status_code in (401, 403, 429):
            # 403 on a public feed almost always means a bot wall, not an
            # outage. Do not retry into it.
            raise SourceBlocked(
                f"HTTP {response.status_code} — the feed is refusing automated "
                f"clients. Disabled for this run."
            )
        if response.status_code != 200:
            raise SourceError(f"HTTP {response.status_code}")

        return self._parse(response.content, feed)

    # -- parsing -----------------------------------------------------------

    def _parse(self, payload: bytes, feed: FeedConfig) -> list[Listing]:
        root = ET.fromstring(payload)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=feed.max_age_hours)

        keyword_re = re.compile(feed.keyword_pattern, re.IGNORECASE) if feed.keyword_pattern else None
        category_re = re.compile(feed.category_pattern, re.IGNORECASE) if feed.category_pattern else None

        listings = []
        for entry in _iter_entries(root):
            item = _read_entry(entry)
            if not item.title:
                continue

            # These feeds carry every category the site sells. Filter first.
            if keyword_re and not keyword_re.search(item.title):
                continue
            if category_re and item.category and not category_re.search(item.category):
                continue
            if item.published and item.published < cutoff:
                continue

            listing = self._to_listing(item, feed)
            if listing is not None:
                listings.append(listing)

        log.info("Feed %s -> %d laptop listings", feed.name, len(listings))
        return listings

    def _to_listing(self, item: "_Entry", feed: FeedConfig) -> Listing | None:
        # A structured price from the Pepper platform beats parsing the title.
        price, currency = None, feed.currency
        if item.merchant_price:
            quote = extract_price(item.merchant_price, feed.currency)
            if quote:
                price, currency = quote.amount, quote.currency

        claimed_discount = None
        if price is None:
            quote = extract_price(f"{item.title} {item.description}", feed.currency)
            if quote is None:
                # No price anywhere. A deal post without a price is not
                # actionable, and inventing one would be worse than skipping it.
                return None
            price, currency = quote.amount, quote.currency
            claimed_discount = quote.claimed_discount_percent

        # A currency the feed's region doesn't use means we misread it, or the
        # post is about a different market. Either way, don't guess.
        expected = self.config.region(feed.region).currency
        if currency != expected:
            log.debug(
                "Feed %s item %r parsed as %s but region uses %s; skipping",
                feed.name, item.title[:60], currency, expected,
            )
            return None

        listing = Listing(
            source=f"rss:{feed.name}",
            listing_id=item.guid or item.link,
            title=item.title,
            url=item.destination_url or item.link,
            region=feed.region,
            currency=currency,
            sticker_price_local=price,
            description=item.description,
            condition=Condition.UNKNOWN,
            seller_name=item.merchant_name or "",
            # A community post is a claim, not a confirmed retailer price.
            source_flags=[Flag.UNVERIFIED_SOURCE],
            raw={"feed": feed.name, "claimed_discount_percent": claimed_discount},
        )

        if claimed_discount is not None:
            # Recorded, never scored. Inflated list prices are the commonest
            # form of fake discount and most of these sites earn commission.
            listing.description += f" [claimed {claimed_discount:g}% off — unverified]"

        return listing


# ---------------------------------------------------------------------------
# Feed parsing helpers
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    title: str = ""
    link: str = ""
    guid: str = ""
    description: str = ""
    category: str = ""
    published: datetime | None = None
    #: From <pepper:merchant price="£65" name="Argos"/> — HotUKDeals, mydealz
    merchant_price: str = ""
    merchant_name: str = ""
    #: From <ozb:meta url="…"/> — the real destination, not the forum thread
    destination_url: str = ""


def local_name(tag: str) -> str:
    """Strip the XML namespace: '{http://…}merchant' -> 'merchant'.

    Matching on the local name rather than the full URI means a platform
    changing its namespace URL doesn't silently break parsing — and we don't
    have to hardcode namespace URIs we can't verify.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_entries(root: ET.Element):
    """Yield <item> (RSS) or <entry> (Atom) elements, wherever they live."""
    for element in root.iter():
        if local_name(element.tag) in ("item", "entry"):
            yield element


def _read_entry(entry: ET.Element) -> _Entry:
    result = _Entry()

    for child in entry:
        tag = local_name(child.tag)
        text = (child.text or "").strip()

        if tag == "title":
            result.title = text
        elif tag == "link":
            # RSS puts the URL in the text; Atom puts it in an href attribute.
            result.link = text or child.attrib.get("href", "")
        elif tag == "guid" or tag == "id":
            result.guid = text
        elif tag in ("description", "summary", "content"):
            result.description = _strip_html(text)
        elif tag == "category":
            result.category = text or child.attrib.get("term", "")
        elif tag in ("pubDate", "published", "updated"):
            result.published = _parse_date(text)
        elif tag == "merchant":
            # Pepper platform: <pepper:merchant name="Argos" price="£65"/>
            result.merchant_price = child.attrib.get("price", "")
            result.merchant_name = child.attrib.get("name", "")
        elif tag == "meta":
            # OzBargain: <ozb:meta url="https://store…"/> is the real target.
            result.destination_url = child.attrib.get("url", "")

    return result


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Feed descriptions are CDATA-wrapped HTML. We only want the words."""
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _parse_date(text: str) -> datetime | None:
    """Parse RFC 822 (RSS) or ISO 8601 (Atom) timestamps."""
    if not text:
        return None

    try:
        parsed = parsedate_to_datetime(text)     # "Mon, 17 Aug 2026 18:15:46 +0100"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        pass

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None
