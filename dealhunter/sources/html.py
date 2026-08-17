"""
Config-driven HTML scraper.

Retailer and tracker markup changes without warning, and a scraper whose
selectors live in Python means a code change and a test run every time a site
tweaks a class name. Here the selectors live in `config.yaml`:

    sources:
      html:
        sites:
          currys:
            base_url: https://www.currys.co.uk
            region: GB
            currency: GBP
            paths: ["/gaming/laptops"]
            selectors:
              item:  "article.product-tile"
              title: "h2.product-title"
              price: "span.price"
              url:   "a.product-link@href"

so fixing a broken site is a YAML edit, not a deploy.

`@attr` on the end of a selector reads an attribute instead of the text —
`a.product-link@href` gets the link, `img@src` gets the image.

**Every site ships disabled.** Selectors written without seeing a site's live
markup are fiction, and a scraper that silently returns zero results is worse
than one that isn't there — you'd think it was working. Enable a site after
verifying its selectors with `--probe`, which prints what the current markup
actually yields.

Two things this deliberately does not do:

* **No JavaScript.** Several targets render their deal cards client-side, so
  this returns nothing on them no matter how good the selectors are. Those need
  a headless browser; see the notes in config.yaml for which ones.
* **No retrying into a wall.** A 403 or a captcha disables the site for the
  run and reports it in the digest.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

from ..config import Config
from ..models import Condition, Flag, Listing, Region
from ..pricing import extract_price
from ..robots import RobotsCache
from .base import HttpPolicy, RequestBudget, Source, SourceBlocked, SourceError

log = logging.getLogger(__name__)

# Markers that a page is a bot wall rather than the content we asked for.
CAPTCHA_MARKERS = (
    "captcha", "are you a robot", "verify you are human", "cf-browser-verification",
    "access denied", "unusual traffic", "px-captcha", "incapsula",
)


@dataclass
class SiteConfig:
    name: str
    base_url: str
    region: Region
    currency: str
    paths: list[str]
    selectors: dict[str, str]
    enabled: bool = False           # opt-in, never opt-out
    condition: str = "UNKNOWN"
    seller_name: str = ""
    is_major_retailer: bool = False
    notes: str = ""
    #: A retailer's "gaming laptops" category page also carries backpacks,
    #: chargers, cooling pads and eGPU enclosures. Without this filter those
    #: all arrive, fail to parse, and bury the real rejections in noise.
    keyword_pattern: str | None = None

    @classmethod
    def from_dict(cls, name: str, block: dict) -> "SiteConfig":
        return cls(
            name=name,
            base_url=block["base_url"].rstrip("/"),
            region=Region(block["region"]),
            currency=block["currency"],
            paths=list(block.get("paths") or ["/"]),
            selectors=dict(block.get("selectors") or {}),
            enabled=bool(block.get("enabled", False)),
            condition=block.get("condition", "UNKNOWN"),
            seller_name=block.get("seller_name", name),
            is_major_retailer=bool(block.get("is_major_retailer", False)),
            notes=block.get("notes", ""),
            keyword_pattern=block.get("keyword_pattern"),
        )


class HtmlSource(Source):
    name = "html"

    def __init__(self, config: Config, session=None, sleep=time.sleep):
        super().__init__(config)
        self.settings = (config.raw_sources or {}).get("html") or {}
        self.policy = HttpPolicy.from_config(config)
        self.budget = RequestBudget(self.policy, sleep=sleep)
        self.requests_made = 0
        self._session = session
        self.robots = RobotsCache(user_agent=self.policy.user_agent, session=session)
        self.site_errors: list[str] = []
        self.disabled_this_run: list[str] = []

    @property
    def regions(self) -> tuple[Region, ...]:  # type: ignore[override]
        return tuple({site.region for site in self._sites()})

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _sites(self) -> list[SiteConfig]:
        return [
            SiteConfig.from_dict(name, block)
            for name, block in (self.settings.get("sites") or {}).items()
        ]

    # -- entry point -------------------------------------------------------

    def fetch(self) -> list[Listing]:
        enabled_regions = set(self.config.enabled_regions())
        listings: list[Listing] = []

        for site in self._sites():
            if not site.enabled or site.region not in enabled_regions:
                continue
            if self.budget.exhausted:
                break

            try:
                listings.extend(self._fetch_site(site))
            except SourceBlocked as exc:
                # Disable and report — never retry into a ban.
                self.disabled_this_run.append(site.name)
                self.site_errors.append(f"{site.name}: {exc}")
                log.warning("Site %s disabled for this run: %s", site.name, exc)
            except Exception as exc:  # noqa: BLE001
                self.site_errors.append(f"{site.name}: {type(exc).__name__}: {exc}")
                log.warning("Site %s failed: %s", site.name, exc)

        return listings

    def _fetch_site(self, site: SiteConfig) -> list[Listing]:
        listings = []
        for path in site.paths:
            if self.budget.exhausted:
                break
            url = urljoin(site.base_url + "/", path.lstrip("/"))

            if not self.robots.is_allowed(url):
                log.info("robots.txt disallows %s; skipping", url)
                continue

            published_delay = self.robots.crawl_delay(url)
            if published_delay and published_delay > self.policy.request_delay_seconds:
                self.budget._sleep(published_delay)

            html = self._get_html(url)
            listings.extend(self._parse(html, site, url))

        return listings

    def _get_html(self, url: str) -> str:
        self.budget.wait_turn()
        self.requests_made += 1

        response = self.session.get(
            url,
            headers={"User-Agent": self.policy.user_agent},
            timeout=self.policy.timeout_seconds,
        )

        if response.status_code in (401, 403, 429):
            raise SourceBlocked(
                f"HTTP {response.status_code} — the site is refusing automated "
                f"clients. It may need a headless browser."
            )
        if response.status_code != 200:
            raise SourceError(f"HTTP {response.status_code} for {url}")

        text = response.text
        lowered = text[:5000].lower()
        if any(marker in lowered for marker in CAPTCHA_MARKERS):
            raise SourceBlocked(
                "the response looks like a captcha or bot wall rather than content"
            )

        return text

    # -- parsing -----------------------------------------------------------

    def _parse(self, html: str, site: SiteConfig, page_url: str) -> list[Listing]:
        soup = _soup(html)
        item_selector = site.selectors.get("item")
        if not item_selector:
            raise SourceError(f"{site.name}: no 'item' selector configured")

        nodes = soup.select(item_selector)
        if not nodes:
            # Almost always a markup change or client-side rendering. Say so
            # loudly rather than quietly reporting success with zero results.
            raise SourceError(
                f"selector {item_selector!r} matched nothing on {page_url}. "
                f"The markup has probably changed, or the page renders its "
                f"content with JavaScript."
            )

        listings = []
        for node in nodes:
            listing = self._node_to_listing(node, site, page_url)
            if listing is not None:
                listings.append(listing)

        log.info("%s %s -> %d listings", site.name, page_url, len(listings))
        return listings

    def _node_to_listing(self, node, site: SiteConfig, page_url: str) -> Listing | None:
        title = _extract(node, site.selectors.get("title"))
        if not title:
            return None

        # Drop accessories before they reach the parser. A "gaming laptops"
        # category page is full of backpacks, chargers and dock enclosures; let
        # through, they all fail as UNPARSEABLE and drown the real rejections.
        if site.keyword_pattern and not re.search(
            site.keyword_pattern, title, re.IGNORECASE
        ):
            return None

        price_text = _extract(node, site.selectors.get("price"))
        quote = extract_price(price_text or title, site.currency)
        if quote is None or quote.currency != site.currency:
            return None

        href = _extract(node, site.selectors.get("url"))
        url = urljoin(page_url, href) if href else page_url

        return Listing(
            source=f"html:{site.name}",
            # No stable ID in the markup, so the fingerprint falls back to a
            # hash of title + seller + price — see Listing.fingerprint().
            listing_id="",
            title=title,
            url=url,
            region=site.region,
            currency=site.currency,
            sticker_price_local=quote.amount,
            description=_extract(node, site.selectors.get("description")) or "",
            condition=_condition_from(site.condition),
            seller_name=site.seller_name,
            is_major_retailer=site.is_major_retailer,
            source_flags=[Flag.UNVERIFIED_SOURCE],
            raw={"site": site.name, "page": page_url},
        )

    # -- selector verification --------------------------------------------

    def probe(self, site_name: str) -> str:
        """Report what a site's configured selectors currently yield.

        Run this before enabling a site. It fetches one page and tells you how
        many items matched and what the first one parsed to, which is the only
        honest way to know whether a selector set works.
        """
        site = next((s for s in self._sites() if s.name == site_name), None)
        if site is None:
            return f"No site named {site_name!r} in sources.html.sites"

        url = urljoin(site.base_url + "/", site.paths[0].lstrip("/"))
        lines = [f"Probing {site.name}: {url}"]

        if not self.robots.is_allowed(url):
            return "\n".join(lines + ["  robots.txt DISALLOWS this path."])

        try:
            html = self._get_html(url)
        except SourceBlocked as exc:
            return "\n".join(lines + [f"  BLOCKED: {exc}"])
        except Exception as exc:  # noqa: BLE001
            return "\n".join(lines + [f"  FAILED: {type(exc).__name__}: {exc}"])

        # Every shipped site starts with empty selectors, so this is the normal
        # first-run state, not an edge case. An empty string makes soupsieve
        # raise SelectorSyntaxError, so check before we hand it over.
        item_selector = (site.selectors.get("item") or "").strip()
        if not item_selector:
            lines.append(f"  fetched OK ({len(html):,} bytes)")
            lines.append("  no 'item' selector configured yet.")
            lines.append(
                "  -> Open the page source and find the repeating element that "
                "wraps one product, then set selectors.item in config.yaml."
            )
            return "\n".join(lines)

        soup = _soup(html)
        nodes = soup.select(item_selector)
        lines.append(f"  item selector matched {len(nodes)} node(s)")

        if not nodes:
            lines.append(
                "  -> Either the markup changed or the page is JavaScript-rendered. "
                "Check the page source (not the inspector, which shows post-JS DOM)."
            )
            return "\n".join(lines)

        for key in ("title", "price", "url", "description"):
            selector = site.selectors.get(key)
            if selector:
                lines.append(f"  {key:<12} {selector!r} -> {_extract(nodes[0], selector)!r}")

        listing = self._node_to_listing(nodes[0], site, url)
        lines.append(
            f"  parsed: {listing.title[:60]!r} @ {listing.sticker_price_local}"
            if listing else "  parsed: FAILED — title or price did not extract"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Selector helpers
# ---------------------------------------------------------------------------

_ATTR_SUFFIX = re.compile(r"^(?P<selector>.*?)@(?P<attribute>[\w:-]+)$")


def _soup(html: str):
    """Parse HTML. Prefers lxml, falls back to the stdlib parser."""
    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — lxml not installed
        return BeautifulSoup(html, "html.parser")


def _extract(node, selector: str | None) -> str:
    """Run a selector against a node, returning text or an attribute.

    Supports the `selector@attribute` form: `a.product@href` returns the href
    rather than the link text. An empty selector returns "".
    """
    if not selector:
        return ""

    attribute = None
    match = _ATTR_SUFFIX.match(selector)
    if match:
        selector, attribute = match.group("selector"), match.group("attribute")

    # An empty selector before the @ means "this node's own attribute".
    target = node if not selector.strip() else node.select_one(selector)
    if target is None:
        return ""

    if attribute:
        value = target.get(attribute, "")
        return " ".join(value) if isinstance(value, list) else str(value).strip()

    return " ".join(target.get_text(" ", strip=True).split())


def _condition_from(name: str) -> Condition:
    try:
        return Condition(name.upper())
    except ValueError:
        return Condition.UNKNOWN
