"""
Best Buy Developer API source (US).

Best Buy open-box is where the best OLED + 5070 Ti pricing has historically
appeared, so getting condition-tier pricing working matters more here than the
raw new-product feed does.

Getting a key
-------------
developer.bestbuy.com -> sign up -> the key is issued immediately and free.
Set it as `BESTBUY_API_KEY`. Rate limit is 5 queries/second, 50,000/day — we
use a handful per run.

Two endpoints
-------------
* ``/v1/products(<query>)`` — the stable, documented catalogue search. This is
  the one to trust.
* ``/beta/products/openBox`` — open-box inventory with condition tiers. It is
  **beta**, its shape has changed before, and the brief already flags that
  open-box tiers "aren't reliably exposed". The code treats a failure here as
  non-fatal: you still get the new-product results, and the digest tells you
  the open-box call broke.

Condition tiers come back as strings like "Excellent", "Good", "Fair", which
map onto the scoring rubric directly — unlike eBay, where "Open box" is
ungraded and has to be scored conservatively.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from ..config import Config
from ..models import Condition, Listing, Region
from .base import HttpPolicy, RequestBudget, Source, SourceAuthError, SourceBlocked, SourceError

log = logging.getLogger(__name__)

API_BASE = "https://api.bestbuy.com"

# Best Buy's open-box condition names, mapped onto our scoring tiers. These are
# the same Excellent/Good/Fair grades the rubric was written against.
OPEN_BOX_CONDITIONS: dict[str, Condition] = {
    "excellent": Condition.OPEN_BOX_EXCELLENT,
    "certified": Condition.OPEN_BOX_EXCELLENT,   # "Geek Squad Certified"
    "good": Condition.OPEN_BOX_GOOD,
    "fair": Condition.OPEN_BOX_FAIR,
}


class BestBuySource(Source):
    name = "bestbuy"
    regions = (Region.US,)

    def __init__(
        self,
        config: Config,
        session=None,
        api_key: str | None = None,
        sleep=time.sleep,
    ):
        super().__init__(config)
        self.settings = (config.raw_sources or {}).get("bestbuy") or {}
        self.policy = HttpPolicy.from_config(config)
        self.budget = RequestBudget(self.policy, sleep=sleep)
        self.requests_made = 0
        self.api_key = api_key or os.environ.get(
            self.settings.get("api_key_env", "BESTBUY_API_KEY"), ""
        )
        self._session = session
        self.open_box_error: str | None = None

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    # -- entry point -------------------------------------------------------

    def fetch(self) -> list[Listing]:
        if not self.api_key:
            raise SourceAuthError(
                "BESTBUY_API_KEY is not set. Get one free at developer.bestbuy.com."
            )

        listings: list[Listing] = []
        seen_skus: set[str] = set()

        for search in self.settings.get("searches") or []:
            if self.budget.exhausted:
                break
            try:
                products = self._search(search)
            except SourceBlocked:
                raise
            except SourceError as exc:
                log.warning("Best Buy search %r failed: %s", search, exc)
                continue

            for product in products:
                sku = str(product.get("sku") or "")
                if not sku or sku in seen_skus:
                    continue
                seen_skus.add(sku)
                listings.append(self._new_listing(product))

        # Open box is the interesting part, and also the fragile part.
        if self.settings.get("fetch_open_box", True) and seen_skus:
            try:
                listings.extend(self._fetch_open_box(sorted(seen_skus)))
            except Exception as exc:  # noqa: BLE001 — beta endpoint, non-fatal
                self.open_box_error = f"open-box lookup failed: {type(exc).__name__}: {exc}"
                log.warning(
                    "Best Buy open-box lookup failed (%s). New-product results "
                    "are unaffected.", exc,
                )

        return listings

    # -- catalogue search --------------------------------------------------

    def _search(self, query: str) -> list[dict[str, Any]]:
        """One catalogue query. Best Buy's filter syntax lives in the URL path."""
        category = self.settings.get("category_id", "abcat0502000")   # Laptops
        max_price = float(self.config.budget["hard_ceiling_usd"]) * float(
            self.settings.get("ceiling_margin", 1.05)
        )

        # (search=X&categoryPath.id=Y&salePrice<=Z) — parenthesised, not query
        # params. This is Best Buy's own convention and it is easy to get wrong.
        path = (
            f"/v1/products((search={query})"
            f"&(categoryPath.id={category})"
            f"&(salePrice<={max_price:.2f}))"
        )

        payload = self._get(path, {
            "format": "json",
            "show": "sku,name,salePrice,regularPrice,url,onlineAvailability,"
                    "condition,manufacturer,modelNumber,percentSavings",
            "pageSize": int(self.settings.get("page_size", 100)),
        })
        return payload.get("products") or []

    def _fetch_open_box(self, skus: list[str]) -> list[Listing]:
        """Open-box offers for SKUs we already found. Beta endpoint."""
        batch_size = int(self.settings.get("open_box_batch", 20))
        listings: list[Listing] = []

        for index in range(0, len(skus), batch_size):
            if self.budget.exhausted:
                break
            batch = skus[index: index + batch_size]
            payload = self._get(
                f"/beta/products/openBox(sku in({','.join(batch)}))",
                {"apiKey": self.api_key, "format": "json"},
            )

            for offer in payload.get("results") or []:
                listings.extend(self._open_box_listings(offer))

        return listings

    def _open_box_listings(self, offer: dict[str, Any]) -> list[Listing]:
        """One product's open-box offers, one Listing per condition tier."""
        sku = str(offer.get("sku") or "")
        name = offer.get("names", {}).get("title") or offer.get("name") or ""
        url = offer.get("links", {}).get("web") or offer.get("url") or ""

        listings = []
        for tier in offer.get("offers") or []:
            condition_raw = str(tier.get("condition") or "")
            condition = OPEN_BOX_CONDITIONS.get(
                condition_raw.lower(), Condition.OPEN_BOX_GOOD
            )
            price = (tier.get("prices") or {}).get("current")
            if price is None:
                continue

            listings.append(Listing(
                source=self.name,
                # The tier is part of the identity: the same SKU can be listed
                # as Excellent and Fair at different prices simultaneously.
                listing_id=f"{sku}:openbox:{condition_raw.lower()}",
                title=f"{name} - Open Box {condition_raw}",
                url=url or f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}",
                region=Region.US,
                currency="USD",
                sticker_price_local=float(price),
                condition=condition,
                condition_raw=condition_raw,
                seller_name="Best Buy",
                is_major_retailer=True,
                warranty_note="Best Buy US warranty; not serviceable in Pakistan.",
                raw=offer,
            ))

        return listings

    def _new_listing(self, product: dict[str, Any]) -> Listing:
        price = product.get("salePrice") or product.get("regularPrice") or 0.0
        return Listing(
            source=self.name,
            listing_id=str(product.get("sku")),
            title=product.get("name") or "",
            url=product.get("url") or "",
            region=Region.US,
            currency="USD",
            sticker_price_local=float(price),
            ships_domestically=bool(product.get("onlineAvailability", True)),
            condition=Condition.NEW,
            seller_name="Best Buy",
            is_major_retailer=True,
            warranty_note="Best Buy US warranty; not serviceable in Pakistan.",
            raw=product,
        )

    # -- HTTP --------------------------------------------------------------

    def _get(self, path: str, params: dict) -> dict[str, Any]:
        params = {"apiKey": self.api_key, **params}

        for attempt in range(self.policy.max_retries):
            self.budget.wait_turn()
            self.requests_made += 1

            response = self.session.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"User-Agent": self.policy.user_agent},
                timeout=self.policy.timeout_seconds,
            )

            if response.status_code == 200:
                return response.json()
            if response.status_code == 403:
                raise SourceBlocked(
                    "Best Buy returned 403 — the API key is invalid or has been "
                    "throttled. Not retrying."
                )
            if response.status_code == 429 or response.status_code >= 500:
                self.budget._sleep(self.policy.backoff_seconds * (2 ** attempt))
                continue

            raise SourceError(f"Best Buy returned HTTP {response.status_code}")

        raise SourceError("Best Buy request failed after retries")
