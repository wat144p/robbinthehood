"""
Reddit source.

r/LaptopDeals is the highest-signal community source: posts are flaired by
price bracket, and it historically surfaces Best Buy open-box and eBay
certified-refurb listings before the aggregator sites notice them.

**The unauthenticated `.json` trick no longer works.** Verified 2026-08-17:

    www.reddit.com/r/X/new.json   -> HTTP 403, regardless of User-Agent
                                     (tested with both a descriptive agent and
                                      a browser one — it is a blanket block)
    old.reddit.com/r/X/new.json   -> HTTP 200 but serves an HTML interstitial,
                                     content-type text/html, not JSON

So read-only polling now needs OAuth. It is still free and still has no
approval process:

    1. reddit.com/prefs/apps -> "create another app" -> type **script**
    2. Redirect URI can be http://localhost — it is unused by this grant
    3. Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET

That gets you a client-credentials token against oauth.reddit.com, good for
100 queries per minute. We make one request per subreddit per run.

Without credentials the source falls back to the public endpoint, reports the
403 honestly, and tells you how to fix it — rather than silently returning
nothing and looking like a quiet day.

Everything from Reddit carries `UNVERIFIED_SOURCE`. A post is somebody's claim
about a price, and the affiliate-bias rule says we verify against our own
history rather than believing the headline.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..models import Condition, Flag, Listing, Region
from ..pricing import extract_price
from .base import HttpPolicy, RequestBudget, Source, SourceBlocked, SourceError

log = logging.getLogger(__name__)

REDDIT_BASE = "https://www.reddit.com"
REDDIT_OAUTH_BASE = "https://oauth.reddit.com"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


@dataclass
class SubredditConfig:
    name: str                       # e.g. "LaptopDeals"
    region: Region
    currency: str
    enabled: bool = True
    listing: str = "new"            # new | hot | rising
    limit: int = 100
    max_age_hours: int = 24
    keyword_pattern: str | None = None
    #: Regexes matched against a post's flair. Empty means accept every flair.
    flair_include: list[str] | None = None
    #: Self-posts are questions, not deals — except on advice subs.
    require_link: bool = True

    @classmethod
    def from_dict(cls, name: str, block: dict) -> "SubredditConfig":
        return cls(
            name=name,
            region=Region(block["region"]),
            currency=block["currency"],
            enabled=bool(block.get("enabled", True)),
            listing=block.get("listing", "new"),
            limit=int(block.get("limit", 100)),
            max_age_hours=int(block.get("max_age_hours", 24)),
            keyword_pattern=block.get("keyword_pattern"),
            flair_include=block.get("flair_include"),
            require_link=bool(block.get("require_link", True)),
        )


class RedditSource(Source):
    name = "reddit"

    def __init__(
        self,
        config: Config,
        session=None,
        sleep=time.sleep,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        super().__init__(config)
        self.settings = (config.raw_sources or {}).get("reddit") or {}
        self.policy = HttpPolicy.from_config(config)
        self.budget = RequestBudget(self.policy, sleep=sleep)
        self.requests_made = 0
        self._session = session
        self.subreddit_errors: list[str] = []

        self.client_id = client_id or os.environ.get("REDDIT_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET", "")
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def authenticated(self) -> bool:
        """OAuth is the only reliable path now, but stay usable without it."""
        return bool(self.client_id and self.client_secret)

    def _get_token(self) -> str | None:
        """Client-credentials application token, cached until it expires.

        Returns None when no credentials are configured, which makes the
        caller fall back to the (now usually blocked) public endpoint.
        """
        if not self.authenticated:
            return None
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        self.budget.wait_turn()
        self.requests_made += 1
        response = self.session.post(
            REDDIT_TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "User-Agent": self.policy.user_agent,
            },
            data={"grant_type": "client_credentials"},
            timeout=self.policy.timeout_seconds,
        )

        if response.status_code in (400, 401):
            raise SourceBlocked(
                "Reddit rejected the credentials. Check REDDIT_CLIENT_ID / "
                "REDDIT_CLIENT_SECRET, and that the app type is 'script'."
            )
        if response.status_code != 200:
            raise SourceError(f"Reddit OAuth returned HTTP {response.status_code}")

        payload = response.json()
        self._token = payload["access_token"]
        # Refresh a minute early so a long run can't expire mid-flight.
        self._token_expires_at = (
            time.monotonic() + float(payload.get("expires_in", 86400)) - 60
        )
        return self._token

    @property
    def regions(self) -> tuple[Region, ...]:  # type: ignore[override]
        return tuple({sub.region for sub in self._subreddits()})

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _subreddits(self) -> list[SubredditConfig]:
        return [
            SubredditConfig.from_dict(name, block)
            for name, block in (self.settings.get("subreddits") or {}).items()
        ]

    # -- entry point -------------------------------------------------------

    def fetch(self) -> list[Listing]:
        enabled_regions = set(self.config.enabled_regions())
        listings: list[Listing] = []

        for sub in self._subreddits():
            if not sub.enabled or sub.region not in enabled_regions:
                continue
            if self.budget.exhausted:
                log.warning("Reddit request budget exhausted; stopping early")
                break

            try:
                listings.extend(self._fetch_subreddit(sub))
            except SourceBlocked as exc:
                self.subreddit_errors.append(f"r/{sub.name}: {exc}")
                log.warning("r/%s is blocked: %s", sub.name, exc)
            except Exception as exc:  # noqa: BLE001
                self.subreddit_errors.append(f"r/{sub.name}: {type(exc).__name__}: {exc}")
                log.warning("r/%s failed: %s", sub.name, exc)

        return listings

    def _fetch_subreddit(self, sub: SubredditConfig) -> list[Listing]:
        token = self._get_token()

        if token:
            url = f"{REDDIT_OAUTH_BASE}/r/{sub.name}/{sub.listing}"
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": self.policy.user_agent,
            }
        else:
            # The public endpoint. Verified 403 as of 2026-08-17, but kept as a
            # fallback in case Reddit reopens it — the response shape is
            # identical either way.
            url = f"{REDDIT_BASE}/r/{sub.name}/{sub.listing}.json"
            headers = {"User-Agent": self.policy.user_agent}

        self.budget.wait_turn()
        self.requests_made += 1
        response = self.session.get(
            url,
            params={"limit": sub.limit, "raw_json": 1},
            headers=headers,
            timeout=self.policy.timeout_seconds,
        )

        if response.status_code in (401, 403, 429):
            if not self.authenticated:
                raise SourceBlocked(
                    f"HTTP {response.status_code} on the public JSON endpoint. "
                    f"Reddit now requires OAuth for programmatic reads. Set "
                    f"REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET — create a free "
                    f"'script' app at reddit.com/prefs/apps. See the module "
                    f"docstring."
                )
            raise SourceBlocked(
                f"HTTP {response.status_code} even with OAuth — the token may "
                f"have been revoked, or we are over the 100 queries/minute limit."
            )
        if response.status_code != 200:
            raise SourceError(f"HTTP {response.status_code}")

        # A 200 that isn't JSON means an interstitial, which old.reddit.com
        # serves instead of the API. Treat it as a block, not as zero results.
        content_type = ""
        try:
            content_type = response.headers.get("content-type", "")
        except AttributeError:
            pass
        if content_type and "json" not in content_type.lower():
            raise SourceBlocked(
                f"Reddit returned {content_type!r} instead of JSON — this is an "
                f"interstitial page, not the API. OAuth credentials are needed."
            )

        return self._parse(response.json(), sub)

    # -- parsing -----------------------------------------------------------

    def _parse(self, payload: dict, sub: SubredditConfig) -> list[Listing]:
        children = ((payload.get("data") or {}).get("children")) or []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=sub.max_age_hours)

        keyword_re = (
            re.compile(sub.keyword_pattern, re.IGNORECASE) if sub.keyword_pattern else None
        )
        flair_res = [re.compile(p, re.IGNORECASE) for p in (sub.flair_include or [])]

        listings = []
        for child in children:
            post = child.get("data") or {}

            if post.get("stickied") or post.get("over_18"):
                continue
            if sub.require_link and post.get("is_self"):
                continue

            title = post.get("title") or ""
            if not title:
                continue
            if keyword_re and not keyword_re.search(title):
                continue

            # Flair filtering. r/LaptopDeals brackets posts by price, and the
            # brief wants the $1000-1200 and $1200-1400 brackets specifically.
            if flair_res:
                flair = post.get("link_flair_text") or ""
                if not any(pattern.search(flair) for pattern in flair_res):
                    continue

            created = post.get("created_utc")
            if created:
                posted_at = datetime.fromtimestamp(float(created), tz=timezone.utc)
                if posted_at < cutoff:
                    continue

            listing = self._to_listing(post, sub)
            if listing is not None:
                listings.append(listing)

        log.info("r/%s -> %d listings", sub.name, len(listings))
        return listings

    def _to_listing(self, post: dict, sub: SubredditConfig) -> Listing | None:
        title = post["title"]
        body = post.get("selftext") or ""

        quote = extract_price(f"{title} {body}", sub.currency)
        if quote is None:
            # No price in the post. Not actionable, and guessing would be worse.
            return None

        expected = self.config.region(sub.region).currency
        if quote.currency != expected:
            return None

        # `url` is the outbound retailer link on a link post; fall back to the
        # Reddit thread so an alert always has somewhere to send you.
        permalink = f"{REDDIT_BASE}{post.get('permalink', '')}"
        destination = post.get("url_overridden_by_dest") or post.get("url") or permalink
        if destination.startswith("/"):
            destination = f"{REDDIT_BASE}{destination}"

        return Listing(
            source=f"reddit:{sub.name}",
            listing_id=post.get("id") or permalink,
            title=title,
            url=destination,
            region=sub.region,
            currency=quote.currency,
            sticker_price_local=quote.amount,
            description=body[:2000],
            condition=_condition_from_text(f"{title} {body}"),
            source_flags=[Flag.UNVERIFIED_SOURCE],
            raw={
                "subreddit": sub.name,
                "flair": post.get("link_flair_text"),
                "score": post.get("score"),
                "num_comments": post.get("num_comments"),
                "permalink": permalink,
                "claimed_discount_percent": quote.claimed_discount_percent,
            },
        )


# Deal posts do sometimes say the condition, and it is worth 12 points, so it
# is worth reading. Ordered most specific first — "open box excellent" has to
# be tested before "open box".
_CONDITION_PATTERNS: list[tuple[str, Condition]] = [
    (r"\bopen[- ]box\b.{0,20}\bexcellent\b", Condition.OPEN_BOX_EXCELLENT),
    (r"\bexcellent\b.{0,20}\bopen[- ]box\b", Condition.OPEN_BOX_EXCELLENT),
    (r"\bopen[- ]box\b.{0,20}\bgood\b", Condition.OPEN_BOX_GOOD),
    (r"\bopen[- ]box\b.{0,20}\bfair\b", Condition.OPEN_BOX_FAIR),
    (r"\bcertified\s+refurb", Condition.MFR_CERTIFIED_REFURB),
    (r"\bmanufacturer\s+refurb", Condition.MFR_CERTIFIED_REFURB),
    (r"\brefurb", Condition.EBAY_REFURBISHED),
    (r"\brenewed\b", Condition.EBAY_REFURBISHED),
    (r"\bopen[- ]box\b", Condition.OPEN_BOX_GOOD),
    (r"\bused\b|\bpre[- ]owned\b", Condition.USED),
    (r"\bbrand[- ]new\b|\bnew\b", Condition.NEW),
]


def _condition_from_text(text: str) -> Condition:
    for pattern, condition in _CONDITION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return condition
    return Condition.UNKNOWN
