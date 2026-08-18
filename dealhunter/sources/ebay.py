"""
eBay Browse API source.

The highest value-per-unit-of-work integration in the whole project: a single
OAuth client-credentials flow gives us five regions, because the Browse API is
per-marketplace via one request header:

    EBAY_US -> US/USD    EBAY_CA -> CA/CAD    EBAY_GB -> GB/GBP
    EBAY_DE -> DE/EUR    EBAY_AU -> AU/AUD

It also covers every condition tier we care about — new, open box,
manufacturer-certified refurbished, eBay Refurbished, and used — with seller
feedback attached, which is exactly what the trust component of the score needs.

Getting credentials
-------------------
1. developer.ebay.com -> register (free)
2. Create a **Production** keyset (the Sandbox one returns fake inventory)
3. Copy the App ID (Client ID) and Cert ID (Client Secret)
4. Export them:  EBAY_CLIENT_ID / EBAY_CLIENT_SECRET

No user consent flow is needed. Browse API search works with an application
token from the client-credentials grant, which this module handles itself.

Rate limits
-----------
The free tier allows roughly 5,000 Browse calls per day. At three queries
across five marketplaces every eight hours we use well under a hundred, so the
budget in config exists to catch a runaway loop, not to ration normal use.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..config import Config
from ..fx import FxRates
from ..geo import jurisdiction_from_postal
from ..models import Condition, Flag, Listing, Region
from .base import (
    HttpPolicy,
    RequestBudget,
    Source,
    SourceAuthError,
    SourceBlocked,
    SourceError,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

ENDPOINTS = {
    "production": {
        "oauth": "https://api.ebay.com/identity/v1/oauth2/token",
        "search": "https://api.ebay.com/buy/browse/v1/item_summary/search",
    },
    "sandbox": {
        "oauth": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "search": "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search",
    },
}

# The only scope the Browse API needs for public search.
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

# ---------------------------------------------------------------------------
# Marketplace -> region
# ---------------------------------------------------------------------------
# Note the absences: eBay has no meaningful Sweden marketplace, and EBAY_BE
# exists but carries almost no laptop inventory. Belgium and Sweden are covered
# by their local retailers in stage 4 instead.
MARKETPLACE_REGIONS: dict[str, tuple[Region, str, str]] = {
    # marketplace id : (region, currency, ISO country for the location filter)
    "EBAY_US": (Region.US, "USD", "US"),
    "EBAY_CA": (Region.CA, "CAD", "CA"),
    "EBAY_GB": (Region.GB, "GBP", "GB"),
    "EBAY_DE": (Region.DE, "EUR", "DE"),
    "EBAY_AU": (Region.AU, "AUD", "AU"),
}

# ---------------------------------------------------------------------------
# Condition mapping
# ---------------------------------------------------------------------------
# eBay's numeric condition IDs, mapped onto our scoring tiers.
#
# The subtle one is 1500 ("Open box"). eBay has no Excellent/Good/Fair grading
# the way Best Buy does, so we map it to the middle tier rather than assuming
# the best case. Over-crediting condition is how you end up driving to collect
# a scuffed machine.
CONDITION_IDS: dict[str, Condition] = {
    "1000": Condition.NEW,
    "1500": Condition.OPEN_BOX_GOOD,          # "Open box" - ungraded, so mid tier
    "2000": Condition.MFR_CERTIFIED_REFURB,   # Certified - Refurbished
    "2010": Condition.EBAY_REFURBISHED,       # Excellent - Refurbished
    "2020": Condition.EBAY_REFURBISHED,       # Very Good - Refurbished
    "2030": Condition.EBAY_REFURBISHED,       # Good - Refurbished
    "2500": Condition.EBAY_REFURBISHED,       # Seller refurbished
    "3000": Condition.USED,
    "7000": Condition.USED,                   # "For parts" - the junk-title
                                              # rule and filters handle these
}


@dataclass
class _Token:
    value: str
    expires_at: datetime

    @property
    def valid(self) -> bool:
        # Refresh a minute early so a long run can't expire mid-flight.
        return datetime.now(timezone.utc) < self.expires_at - timedelta(seconds=60)


class EbaySource(Source):
    """Queries the eBay Browse API across every configured marketplace."""

    name = "ebay"
    regions = (Region.US, Region.CA, Region.GB, Region.DE, Region.AU)

    def __init__(
        self,
        config: Config,
        rates: FxRates,
        session: Any = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        sleep=time.sleep,
    ):
        """
        `session` is anything with requests-compatible `.get()` and `.post()`.
        Injecting it is what lets the tests run the full mapping path against
        recorded payloads without touching the network.

        `rates` is needed up front because we derive each marketplace's price
        ceiling from the landed-USD budget — see `_price_bounds_local`.
        """
        super().__init__(config)
        self.rates = rates
        self.settings = (config.raw_sources or {}).get("ebay") or {}
        self.policy = HttpPolicy.from_config(config)
        self.budget = RequestBudget(self.policy, sleep=sleep)
        self.requests_made = 0

        self.client_id = client_id or os.environ.get("EBAY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("EBAY_CLIENT_SECRET", "")

        environment = self.settings.get("environment", "production")
        if environment not in ENDPOINTS:
            raise SourceError(
                f"sources.ebay.environment must be 'production' or 'sandbox', "
                f"got {environment!r}"
            )
        self.endpoints = ENDPOINTS[environment]

        self._session = session
        self._token: _Token | None = None
        #: seller -> listings found, so a mistyped storefront name is
        #: reported rather than silently returning nothing.
        self.storefront_hits: dict[str, int] = {}

    # -- session -----------------------------------------------------------

    @property
    def session(self):
        """Lazily build a requests session so importing this module is cheap."""
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.policy.user_agent})
        return self._session

    # -- entry point -------------------------------------------------------

    def fetch(self) -> list[Listing]:
        """Run every configured query against every enabled marketplace."""
        if not self.client_id or not self.client_secret:
            raise SourceAuthError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set. "
                "See the module docstring for how to get them."
            )

        queries: list[str] = self.settings.get("queries") or []
        if not queries:
            raise SourceError("sources.ebay.queries is empty — nothing to search for")

        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for marketplace in self._active_marketplaces():
            for query in queries:
                if self.budget.exhausted:
                    log.warning(
                        "eBay request budget exhausted; stopping early with %d listings",
                        len(listings),
                    )
                    return listings

                try:
                    items = self._search(marketplace, query)
                except (SourceBlocked, SourceAuthError):
                    # Both are whole-source conditions, not query-level ones.
                    # A ban must never be retried, and bad credentials will
                    # fail identically for every remaining marketplace — so
                    # abort rather than generating five identical failures.
                    # These are caught before SourceError because they subclass it.
                    raise
                except SourceError as exc:
                    # One bad query shouldn't cost us the other marketplaces.
                    log.warning("eBay %s query %r failed: %s", marketplace, query, exc)
                    continue

                for item in items:
                    listing = self._to_listing(item, marketplace)
                    if listing is None:
                        continue
                    # The same item legitimately matches several of our queries.
                    if listing.listing_id in seen_ids:
                        continue
                    seen_ids.add(listing.listing_id)
                    listings.append(listing)

        listings.extend(self._fetch_storefronts(seen_ids))
        return listings

    def _fetch_storefronts(self, seen_ids: set[str]) -> list[Listing]:
        """Query named seller storefronts directly.

        Best Buy, Acer, Lenovo and the large refurbishers all sell through
        eBay, and it is largely the same open-box and certified-refurb stock
        their own sites carry. That matters for two reasons: it is inventory a
        keyword search can miss when a seller writes an unusual title, and it
        is the only route to Best Buy's open-box pricing if you cannot get a
        Best Buy API key.

        Seller usernames cannot be verified without a live key — eBay returns
        HTTP 418 to unauthenticated clients — so a storefront that produces
        nothing anywhere is reported loudly rather than passed over. A silently
        wrong username would look exactly like a quiet day.
        """
        block = self.settings.get("storefronts") or {}
        if not block.get("enabled", False):
            return []

        sellers = [str(s).strip() for s in (block.get("sellers") or []) if str(s).strip()]
        queries = block.get("queries") or ["gaming laptop"]
        if not sellers:
            return []

        listings: list[Listing] = []
        # Query each storefront separately rather than OR-ing them together, so
        # a zero result can be attributed to a specific (probably mistyped) name.
        for seller in sellers:
            self.storefront_hits.setdefault(seller, 0)

            for marketplace in self._active_marketplaces():
                for query in queries:
                    if self.budget.exhausted:
                        log.warning("eBay budget exhausted during the storefront pass")
                        return listings
                    try:
                        items = self._search(marketplace, query, sellers=[seller])
                    except (SourceBlocked, SourceAuthError):
                        raise
                    except SourceError as exc:
                        log.warning(
                            "eBay storefront %s on %s failed: %s", seller, marketplace, exc
                        )
                        continue

                    # eBay's `sellers:{name}` filter has been observed to be
                    # SILENTLY IGNORED for a username it doesn't recognise,
                    # returning the entire unfiltered category instead of an
                    # error or an empty result (confirmed live 2026-08-18: a
                    # deliberately fake seller name returned the same result
                    # set, with unrelated sellers, as several real-looking
                    # guesses). Trusting the filter blindly would let a wrong
                    # storefront name silently attribute a random seller's
                    # stock to e.g. "Best Buy" and hand it the major-retailer
                    # trust bonus. So every item is checked against the seller
                    # we actually asked for, and anything else is dropped.
                    genuine = [
                        it for it in items
                        if (it.get("seller") or {}).get("username", "").lower()
                        == seller.lower()
                    ]
                    if len(genuine) != len(items):
                        log.warning(
                            "eBay storefront %r: %d of %d results on %s were from "
                            "a different seller and were dropped — the filter was "
                            "likely ignored because the username is wrong.",
                            seller, len(items) - len(genuine), len(items), marketplace,
                        )

                    # Count only genuine matches. A storefront whose whole
                    # inventory the keyword pass already found is working
                    # perfectly and must not be reported as mistyped.
                    self.storefront_hits[seller] += len(genuine)

                    for item in genuine:
                        listing = self._to_listing(item, marketplace)
                        if listing is None or listing.listing_id in seen_ids:
                            continue
                        seen_ids.add(listing.listing_id)
                        listings.append(listing)

        for seller, hits in self.storefront_hits.items():
            if hits == 0:
                log.warning(
                    "eBay storefront %r returned nothing on any marketplace. The "
                    "username is probably wrong — check it at "
                    "ebay.com/str/%s and fix sources.ebay.storefronts.sellers.",
                    seller, seller,
                )

        return listings

    def _active_marketplaces(self) -> list[str]:
        """Configured marketplaces whose region is enabled in config."""
        enabled_regions = set(self.config.enabled_regions())
        configured = self.settings.get("marketplaces") or list(MARKETPLACE_REGIONS)

        active = []
        for marketplace in configured:
            mapping = MARKETPLACE_REGIONS.get(marketplace)
            if mapping is None:
                log.warning("Unknown eBay marketplace %r in config; skipping", marketplace)
                continue
            if mapping[0] in enabled_regions:
                active.append(marketplace)
        return active

    # -- OAuth -------------------------------------------------------------

    def _get_token(self) -> str:
        """Client-credentials application token, cached until it expires.

        No user consent is involved — this grant is for public data access,
        which is all the Browse API search needs.
        """
        if self._token and self._token.valid:
            return self._token.value

        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        self.budget.wait_turn()
        self.requests_made += 1
        response = self.session.post(
            self.endpoints["oauth"],
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.policy.user_agent,
            },
            data={"grant_type": "client_credentials", "scope": OAUTH_SCOPE},
            timeout=self.policy.timeout_seconds,
        )

        if response.status_code in (400, 401):
            raise SourceAuthError(
                f"eBay rejected the credentials (HTTP {response.status_code}). "
                f"Check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET, and that the keyset "
                f"matches sources.ebay.environment."
            )
        if response.status_code != 200:
            raise SourceError(f"eBay OAuth returned HTTP {response.status_code}")

        payload = response.json()
        self._token = _Token(
            value=payload["access_token"],
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=int(payload.get("expires_in", 7200))),
        )
        return self._token.value

    # -- search ------------------------------------------------------------

    def _search(
        self, marketplace: str, query: str, sellers: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """One query against one marketplace, paginated.

        `sellers` restricts the search to named storefronts.
        """
        region, currency, country = MARKETPLACE_REGIONS[marketplace]
        token = self._get_token()

        limit = int(self.settings.get("results_per_query", 100))
        max_pages = int(self.settings.get("max_pages", 2))
        items: list[dict[str, Any]] = []

        for page in range(max_pages):
            if self.budget.exhausted:
                break

            params = {
                "q": query,
                "limit": limit,
                "offset": page * limit,
                "filter": self._build_filter(region, currency, country, sellers),
                # "Laptops & Netbooks". Verified live on 2026-08-18: without
                # this, a plain title search for a model name pulls in
                # compatible parts sold under that title ("SSD passend für
                # Acer Predator Helios Neo 16S AI") — accessories, not
                # laptops. With it, the same query returns only laptops.
                "category_ids": "177",
                "sort": "price",
            }
            headers = {
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": marketplace,
                "User-Agent": self.policy.user_agent,
                # Tells eBay where the buyer is, which makes the returned
                # shipping costs the domestic ones our contact would pay.
                "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3D{country}",
            }

            payload = self._request_json(params, headers)
            page_items = payload.get("itemSummaries") or []
            items.extend(page_items)

            # Stop as soon as a page comes back short — there is no next one.
            if len(page_items) < limit:
                break

        log.info("eBay %s %r -> %d items", marketplace, query, len(items))
        return items

    def _request_json(self, params: dict, headers: dict) -> dict[str, Any]:
        """GET with backoff on 429/5xx, and a hard stop on 403."""
        last_error = ""

        for attempt in range(self.policy.max_retries):
            self.budget.wait_turn()
            self.requests_made += 1

            response = self.session.get(
                self.endpoints["search"],
                params=params,
                headers=headers,
                timeout=self.policy.timeout_seconds,
            )

            if response.status_code == 200:
                return response.json()

            if response.status_code == 403:
                # Not a transient problem. Stop the source rather than
                # hammering our way towards a longer ban.
                raise SourceBlocked(
                    "eBay returned HTTP 403. The keyset may lack Browse API "
                    "access, or the application has been throttled."
                )

            if response.status_code == 429 or response.status_code >= 500:
                wait = self.policy.backoff_seconds * (2 ** attempt)
                last_error = f"HTTP {response.status_code}"
                log.warning("eBay %s; backing off %.1fs", last_error, wait)
                self.budget._sleep(wait)
                continue

            raise SourceError(f"eBay search returned HTTP {response.status_code}")

        raise SourceError(f"eBay search failed after retries ({last_error})")

    def _build_filter(
        self, region: Region, currency: str, country: str,
        sellers: list[str] | None = None,
    ) -> str:
        """Build the Browse API `filter` parameter.

        Two constraints matter beyond the obvious:

        * `itemLocationCountry` + `deliveryCountry` together enforce the
          domestic-shipping requirement. We never want a seller who has to ship
          internationally — the forwarding contact receives it locally.
        * The price ceiling is derived from the landed-USD budget rather than
          hardcoded per currency, so it stays correct as FX moves.
        """
        low, high = self._price_bounds_local(region, currency)
        condition_ids = self.settings.get("condition_ids") or list(CONDITION_IDS)

        clauses = [
            f"itemLocationCountry:{country}",
            f"deliveryCountry:{country}",
            f"conditionIds:{{{'|'.join(str(c) for c in condition_ids)}}}",
            f"price:[{low:.0f}..{high:.0f}]",
            f"priceCurrency:{currency}",
            # Auctions have no settled price to score, so fixed-price only.
            "buyingOptions:{FIXED_PRICE}",
        ]

        if sellers:
            clauses.append(f"sellers:{{{'|'.join(sellers)}}}")

        return ",".join(clauses)

    def _price_bounds_local(self, region: Region, currency: str) -> tuple[float, float]:
        """Convert the landed-USD budget into a local-currency search range.

        Working backwards from the ceiling:

            max_sticker_local = ceiling_usd / (fx_rate * (1 + risk_premium))

        Deliberately *not* divided by (1 + checkout tax): that would tighten the
        range, and a slightly generous ceiling costs nothing — anything genuinely
        over budget gets rejected by the landed-cost filter a moment later.
        Being too tight here would silently hide real deals.
        """
        region_cfg = self.config.region(region)
        fx = self.rates.to_usd(currency)
        divisor = fx * (1.0 + region_cfg.risk_premium)

        ceiling_usd = float(self.config.budget["hard_ceiling_usd"])
        floor_usd = float(self.settings.get("min_price_usd", 400))
        margin = float(self.settings.get("ceiling_margin", 1.05))

        return floor_usd / divisor, (ceiling_usd * margin) / divisor

    # -- mapping -----------------------------------------------------------

    def _to_listing(self, item: dict[str, Any], marketplace: str) -> Listing | None:
        """Map one Browse API `itemSummary` onto our `Listing`.

        Returns None for items we can't price, which the API occasionally
        produces for group listings with no representative price.
        """
        region, currency, country = MARKETPLACE_REGIONS[marketplace]

        price_block = item.get("price") or {}
        try:
            price = float(price_block["value"])
        except (KeyError, TypeError, ValueError):
            return None

        # Guard the currency: eBay returns the marketplace's own currency, but
        # if that ever changes the landed maths would apply the wrong tax rules.
        item_currency = (price_block.get("currency") or currency).upper()
        if item_currency != currency:
            log.warning(
                "eBay %s returned %s for item %s; skipping to avoid mixing tax rules",
                marketplace, item_currency, item.get("itemId"),
            )
            return None

        location = item.get("itemLocation") or {}
        seller = item.get("seller") or {}
        shipping, ships_domestically = self._shipping_from(item, currency)

        listing = Listing(
            source=self.name,
            listing_id=str(item.get("itemId") or ""),
            title=item.get("title") or "",
            url=item.get("itemWebUrl") or "",
            region=region,
            currency=currency,
            sticker_price_local=price,
            domestic_shipping_local=shipping,
            ships_domestically=ships_domestically,
            condition=self._condition_for(item, seller),
            condition_raw=str(item.get("condition") or ""),
            seller_name=str(seller.get("username") or ""),
            seller_feedback_count=_int_or_none(seller.get("feedbackScore")),
            seller_feedback_percent=_float_or_none(seller.get("feedbackPercentage")),
            is_major_retailer=self._is_major_storefront(seller),
            jurisdiction=jurisdiction_from_postal(
                location.get("country") or country, location.get("postalCode")
            )
            or location.get("stateOrProvince"),
            warranty_note=self._warranty_note(item, region),
            raw=item,
        )

        # A multi-variation listing advertises its cheapest variant, which is
        # usually a lower-spec config than the one described in the title — the
        # price and the specs may simply not belong to the same machine.
        #
        # Resolving this properly means a second call per listing to
        # item_summary/search?item_group_id= to pull the real variant prices.
        # For now we flag it so the alert says "verify the config before
        # buying" rather than quietly quoting an optimistic price.
        if item.get("itemGroupType"):
            listing.source_flags.append(Flag.MULTI_VARIATION_LISTING)

        return listing

    def _shipping_from(self, item: dict, currency: str) -> tuple[float, bool]:
        """Domestic shipping cost, and whether it ships at all.

        Pickup-only listings are useless to us: the forwarding contact isn't
        going to drive to a warehouse.
        """
        options: Iterable[dict] = item.get("shippingOptions") or []
        cheapest: float | None = None
        pickup_only = True

        for option in options:
            option_type = (option.get("shippingCostType") or "").upper()
            cost_block = option.get("shippingCost") or {}

            if (option.get("type") or "").upper() == "PICKUP":
                continue
            pickup_only = False

            value = _float_or_none(cost_block.get("value"))
            if value is None:
                # "Calculated" shipping with no quote — treat as unknown rather
                # than free, so we don't understate the landed cost.
                continue
            if (cost_block.get("currency") or currency).upper() != currency:
                continue
            if cheapest is None or value < cheapest:
                cheapest = value

            if option_type == "FIXED" and value == 0:
                cheapest = 0.0

        if not options:
            # No shipping block at all. The search filter already constrained
            # this to domestic delivery, so assume it ships and cost nothing
            # known — the alert will show shipping as zero, which is what eBay
            # itself displays in this case.
            return 0.0, True

        return (cheapest or 0.0), not pickup_only

    def _condition_for(self, item: dict, seller: dict) -> Condition:
        """Map eBay's condition, upgrading known manufacturer storefronts.

        eBay tags Acer's and Lenovo's own outlet stores as plain "Seller
        refurbished" (2500) even though those carry a real manufacturer
        warranty, so a configured storefront list promotes them.
        """
        condition_id = str(item.get("conditionId") or "")
        condition = CONDITION_IDS.get(condition_id, Condition.UNKNOWN)

        if condition in (Condition.EBAY_REFURBISHED, Condition.UNKNOWN):
            username = (seller.get("username") or "").lower()
            certified = [
                s.lower() for s in (self.settings.get("certified_refurb_sellers") or [])
            ]
            if any(store in username for store in certified):
                return Condition.MFR_CERTIFIED_REFURB

        return condition

    def _is_major_storefront(self, seller: dict) -> bool:
        """True when the seller is one of the major retailers' own eBay stores."""
        username = (seller.get("username") or "").lower()
        majors = [s.lower() for s in self.config.scoring["major_retailers"]]
        return any(name.replace(" ", "") in username.replace("_", "") for name in majors)

    def _warranty_note(self, item: dict, region: Region) -> str:
        """Surface warranty context. Never scored — just shown in the alert.

        None of it is reliably serviceable in Pakistan, so this is about
        knowing what you are giving up, not about ranking.
        """
        title = (item.get("title") or "").lower()
        notes = []

        if "warranty" in title:
            notes.append("listing mentions a warranty — verify the terms")
        if region != Region.US:
            notes.append(
                f"{region.value} warranty; manufacturer coverage is regional and "
                f"not serviceable in Pakistan"
            )
        else:
            notes.append("US warranty; not serviceable in Pakistan")

        return "; ".join(notes)


# ---------------------------------------------------------------------------
# Small coercion helpers — eBay returns numbers as strings, inconsistently
# ---------------------------------------------------------------------------


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
