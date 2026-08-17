"""
Feed and API fixtures for the Tier 0/1 sources.

The RSS samples reproduce the **verified live structure** of each feed as
checked on 2026-08-17, including the custom namespaced elements that are the
whole reason those feeds are worth more than title-parsing:

    OzBargain    <ozb:meta url="…"/>                    -> real destination URL
    HotUKDeals   <pepper:merchant name=… price=…/>      -> price and retailer

The Reddit fixture matches the shape of `/r/{sub}/new.json`.
"""

from __future__ import annotations

import json
import time
from typing import Any

# ---------------------------------------------------------------------------
# OzBargain — RSS 2.0 with the ozb: namespace
# ---------------------------------------------------------------------------

OZBARGAIN_FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
     xmlns:ozb="https://www.ozbargain.com.au/xmlns/ozb"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>OzBargain Deals</title>
  <item>
    <title>Lenovo Legion Pro 5 16 2560x1600 OLED 165Hz RTX 5060 32GB 1TB $2,199 Delivered @ Lenovo AU</title>
    <link>https://www.ozbargain.com.au/node/971001</link>
    <description><![CDATA[<div><p>Good price on the OLED config.</p></div>]]></description>
    <category domain="https://www.ozbargain.com.au/cat/computing">Computing</category>
    <ozb:meta comment-count="4" link="https://www.ozbargain.com.au/goto/971001"
              url="https://www.lenovo.com/au/en/p/legion-pro-5" votes-pos="12" />
    <pubDate>{recent}</pubDate>
    <dc:creator>somebody</dc:creator>
    <guid isPermaLink="false">971001 at https://www.ozbargain.com.au</guid>
  </item>
  <item>
    <title>[PC, Steam] Lossless Scaling $5.12 @ Steam</title>
    <link>https://www.ozbargain.com.au/node/971708</link>
    <description><![CDATA[<p>Frame generation tool.</p>]]></description>
    <category domain="https://www.ozbargain.com.au/cat/computing">Computing</category>
    <ozb:meta url="https://store.steampowered.com/app/993090/" />
    <pubDate>{recent}</pubDate>
    <guid isPermaLink="false">971708 at https://www.ozbargain.com.au</guid>
  </item>
  <item>
    <title>Gaming Laptop RTX 5070 Ti $2,499 @ Scorptec (ancient)</title>
    <link>https://www.ozbargain.com.au/node/900000</link>
    <description><![CDATA[<p>Old deal.</p>]]></description>
    <category>Computing</category>
    <pubDate>Mon, 03 Feb 2025 09:00:00 +1100</pubDate>
    <guid isPermaLink="false">900000 at https://www.ozbargain.com.au</guid>
  </item>
</channel>
</rss>
"""

# ---------------------------------------------------------------------------
# HotUKDeals — Pepper platform, with the merchant element
# ---------------------------------------------------------------------------

HOTUKDEALS_FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
     xmlns:pepper="https://www.pepper.com/xmlns/pepper"
     xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title>hotukdeals</title>
  <item>
    <category><![CDATA[Laptops]]></category>
    <pepper:merchant name="Currys" price="£1,150"/>
    <title><![CDATA[312° - Lenovo Legion 7i 16 2.5K OLED 165Hz Ultra 7 255HX RTX 5060 32GB 1TB]]></title>
    <description><![CDATA[<strong>£1,150 - Currys</strong><br /><p>Cheapest this has been.</p>]]></description>
    <link>https://www.hotukdeals.com/deals/lenovo-legion-7i-4959530</link>
    <pubDate>{recent}</pubDate>
    <guid>https://www.hotukdeals.com/deals/lenovo-legion-7i-4959530</guid>
  </item>
  <item>
    <category><![CDATA[Sports &amp; Outdoors]]></category>
    <pepper:merchant name="Argos" price="£65"/>
    <title><![CDATA[108° - Luke Littler World Champion Edition TOR Dartboard &amp; Surround]]></title>
    <description><![CDATA[<strong>£65 - Argos</strong>]]></description>
    <link>https://www.hotukdeals.com/deals/dartboard-4959529</link>
    <pubDate>{recent}</pubDate>
    <guid>https://www.hotukdeals.com/deals/dartboard-4959529</guid>
  </item>
</channel>
</rss>
"""

# An Atom feed, to prove the parser isn't RSS-only.
ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Some Tracker</title>
  <entry>
    <title>Acer Predator Helios Neo 16S AI OLED RTX 5070 Ti 32GB 1TB $1,184</title>
    <link href="https://example.test/deal/1"/>
    <id>tag:example.test,2026:deal/1</id>
    <summary>Open box excellent.</summary>
    <updated>{recent_iso}</updated>
  </entry>
</feed>
"""


def _rfc822_now() -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ozbargain_feed() -> bytes:
    return OZBARGAIN_FEED.format(recent=_rfc822_now()).encode("utf-8")


def hotukdeals_feed() -> bytes:
    return HOTUKDEALS_FEED.format(recent=_rfc822_now()).encode("utf-8")


def atom_feed() -> bytes:
    return ATOM_FEED.format(recent_iso=_iso_now()).encode("utf-8")


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------


def reddit_post(
    post_id: str = "abc123",
    title: str = "[$1,184] Acer Predator Helios Neo 16S AI OLED RTX 5070 Ti 32GB 1TB",
    flair: str | None = "$1000-$1200",
    url: str = "https://www.bestbuy.com/site/6604821",
    is_self: bool = False,
    stickied: bool = False,
    age_hours: float = 1.0,
    selftext: str = "",
) -> dict[str, Any]:
    return {
        "kind": "t3",
        "data": {
            "id": post_id,
            "title": title,
            "url": url,
            "url_overridden_by_dest": None if is_self else url,
            "permalink": f"/r/LaptopDeals/comments/{post_id}/deal/",
            "link_flair_text": flair,
            "created_utc": time.time() - age_hours * 3600,
            "is_self": is_self,
            "stickied": stickied,
            "over_18": False,
            "selftext": selftext,
            "score": 42,
            "num_comments": 7,
        },
    }


def reddit_listing(posts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "Listing", "data": {"after": None, "children": posts}}


# ---------------------------------------------------------------------------
# Best Buy
# ---------------------------------------------------------------------------


def bestbuy_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    return {"from": 1, "to": len(products), "total": len(products),
            "products": products}


def bestbuy_product(
    sku: str = "6604821",
    name: str = "Acer - Predator Helios Neo 16S AI 16\" OLED RTX 5070 Ti 32GB 1TB",
    sale_price: float = 1299.99,
) -> dict[str, Any]:
    return {
        "sku": sku,
        "name": name,
        "salePrice": sale_price,
        "regularPrice": 1699.99,
        "url": f"https://www.bestbuy.com/site/{sku}.p",
        "onlineAvailability": True,
        "manufacturer": "Acer",
        "modelNumber": "PHN16S-71-99XM",
    }


def bestbuy_open_box(sku: str = "6604821") -> dict[str, Any]:
    """The beta open-box endpoint's shape: one product, several condition tiers."""
    return {
        "results": [{
            "sku": int(sku),
            "names": {"title": "Acer Predator Helios Neo 16S AI 16\" OLED RTX 5070 Ti"},
            "links": {"web": f"https://www.bestbuy.com/site/{sku}.p"},
            "offers": [
                {"condition": "Excellent", "prices": {"current": 1184.00}},
                {"condition": "Good", "prices": {"current": 1129.00}},
                {"condition": "Fair", "prices": {"current": 1049.00}},
            ],
        }]
    }


# ---------------------------------------------------------------------------
# A fake session shared by the feed/API source tests
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, content=b"", text=None, payload=None,
                 headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", "replace")
        self._payload = payload
        # Default to JSON so payload-based fixtures pass the content-type check
        # the Reddit source uses to spot HTML interstitials.
        self.headers = headers if headers is not None else (
            {"content-type": "application/json"} if payload is not None else {}
        )

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class FakeFeedSession:
    """Maps URL substrings to responses. Records every request."""

    def __init__(self, routes: dict[str, FakeResponse] | None = None,
                 default: FakeResponse | None = None):
        self.routes = routes or {}
        self.default = default or FakeResponse(404, b"not found")
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return self.default

    def post(self, url, **kwargs):
        # Routed the same way as get(), so OAuth token endpoints can be stubbed.
        self.calls.append({"url": url, **kwargs})
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return FakeResponse(200, b"{}")


ROBOTS_ALLOW_ALL = FakeResponse(200, b"User-agent: *\nAllow: /\n")
