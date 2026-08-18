"""
eBay Browse API response fixtures.

These are hand-built payloads matching the shape of `item_summary/search`
responses — field names, nesting, and eBay's habit of returning numbers as
strings are all reproduced faithfully. They are synthetic, not captured from a
live account, so treat them as a contract test for our mapping rather than as
evidence about real inventory.

If eBay changes the response shape, this is the file to update; the fake
session below is the only thing standing between the mapping code and the
network.
"""

from __future__ import annotations

from typing import Any


def item(
    item_id: str = "v1|123456789012|0",
    title: str = "Gaming Laptop",
    price: str = "1199.99",
    currency: str = "USD",
    condition_id: str = "1000",
    condition: str = "New",
    seller: str = "bestbuy",
    feedback_score: int = 250000,
    feedback_percent: str = "98.7",
    country: str = "US",
    postal: str | None = "19801",
    shipping: str | None = "0.00",
    pickup_only: bool = False,
    item_group_type: str | None = None,
) -> dict[str, Any]:
    """One `itemSummary`, with eBay's real field names and string-typed numbers."""
    payload: dict[str, Any] = {
        "itemId": item_id,
        "title": title,
        "itemWebUrl": f"https://www.ebay.com/itm/{item_id.split('|')[1]}",
        "price": {"value": price, "currency": currency},
        "conditionId": condition_id,
        "condition": condition,
        "seller": {
            "username": seller,
            "feedbackScore": feedback_score,
            "feedbackPercentage": feedback_percent,
        },
        "itemLocation": {"country": country},
        "buyingOptions": ["FIXED_PRICE"],
    }

    if postal:
        payload["itemLocation"]["postalCode"] = postal

    if pickup_only:
        payload["shippingOptions"] = [
            {"type": "PICKUP", "shippingCostType": "FIXED",
             "shippingCost": {"value": "0.00", "currency": currency}}
        ]
    elif shipping is not None:
        payload["shippingOptions"] = [
            {"type": "SHIPPING", "shippingCostType": "FIXED",
             "shippingCost": {"value": shipping, "currency": currency}}
        ]

    if item_group_type:
        payload["itemGroupType"] = item_group_type

    return payload


def search_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"total": len(items), "limit": 100, "offset": 0, "itemSummaries": items}


def item_detail(title: str, aspects: dict[str, str] | None = None) -> dict:
    """A `/buy/browse/v1/item/{id}` response: the full title plus item
    specifics, which is what a truncated search-result title is enriched from."""
    return {
        "title": title,
        "localizedAspects": [
            {"name": name, "value": value} for name, value in (aspects or {}).items()
        ],
    }


OAUTH_RESPONSE = {
    "access_token": "v^1.1#i^1#fake-application-token",
    "expires_in": 7200,
    "token_type": "Application Access Token",
}


# ---------------------------------------------------------------------------
# A fake requests-compatible session
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Stands in for `requests.Session`.

    `responses` maps a marketplace id to the payload its search should return.
    Every request is recorded on `.calls` so tests can assert on the filter
    string, the marketplace header, and how many requests were made.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        search_status: int = 200,
        oauth_status: int = 200,
        item_responses: dict[str, Any] | None = None,
        item_status: int = 200,
    ):
        self.responses = responses or {}
        self.search_status = search_status
        self.oauth_status = oauth_status
        # itemId (URL-decoded) -> Item resource payload, for the full-item
        # detail endpoint used to enrich eBay's truncated titles.
        self.item_responses = item_responses or {}
        self.item_status = item_status
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers or {}})
        return FakeResponse(self.oauth_status, OAUTH_RESPONSE)

    def get(self, url, params=None, headers=None, timeout=None):
        from urllib.parse import unquote

        headers = headers or {}
        params = params or {}
        self.calls.append(
            {"method": "GET", "url": url, "params": params, "headers": headers}
        )

        # The full-item detail endpoint: .../buy/browse/v1/item/{item_id}
        # (distinct from .../item_summary/search, which "item" alone would
        # also match, so item_summary is excluded explicitly).
        if "/item/" in url and "item_summary" not in url:
            if self.item_status != 200:
                return FakeResponse(self.item_status, {})
            item_id = unquote(url.rsplit("/item/", 1)[-1])
            payload = self.item_responses.get(item_id)
            if payload is None:
                return FakeResponse(404, {"errors": [{"message": "not found"}]})
            return FakeResponse(200, payload)

        if self.search_status != 200:
            return FakeResponse(self.search_status, {})

        marketplace = headers.get("X-EBAY-C-MARKETPLACE-ID", "")
        payload = self.responses.get(marketplace)

        # Only the first page has content; a short page stops pagination.
        if payload is None or params.get("offset", 0) > 0:
            return FakeResponse(200, search_response([]))
        return FakeResponse(200, payload)

    # -- assertions helpers --------------------------------------------------

    @property
    def searches(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "GET"]

    def filters_for(self, marketplace: str) -> list[str]:
        return [
            call["params"]["filter"]
            for call in self.searches
            if call["headers"].get("X-EBAY-C-MARKETPLACE-ID") == marketplace
        ]
