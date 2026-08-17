"""
robots.txt compliance.

The brief asks for this explicitly, and it is also just self-interest: a source
that gets us banned stops producing deals. Every scraping source checks here
before it fetches anything.

Behaviour follows RFC 9309, including the part people usually get wrong:

    2xx  -> parse and obey the rules
    4xx  -> no restrictions published; crawling is allowed
    5xx  -> the site is unwell. Assume *complete disallow* rather than helping
            ourselves while it's down. This is the conservative reading and it
            is what the RFC actually specifies.

`Crawl-delay` is honoured where a site publishes one, taking precedence over
our own configured delay when it's longer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)

# How long a fetched robots.txt stays good for. A day is the usual convention.
CACHE_SECONDS = 24 * 3600


@dataclass
class _CachedRobots:
    parser: RobotFileParser | None
    fetched_at: float
    allow_all: bool = False
    deny_all: bool = False

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.fetched_at) > CACHE_SECONDS


@dataclass
class RobotsCache:
    """Per-host robots.txt rules, fetched once and cached.

    Constructed with the session and user-agent the caller will actually use,
    because a robots check made under a different identity is meaningless.
    """

    user_agent: str
    session: object = None
    timeout: float = 10.0
    _cache: dict[str, _CachedRobots] = field(default_factory=dict)

    def _get_session(self):
        if self.session is None:
            import requests

            self.session = requests.Session()
        return self.session

    # -- public ------------------------------------------------------------

    def is_allowed(self, url: str) -> bool:
        """May we fetch this URL under our user agent?"""
        rules = self._rules_for(url)

        if rules.deny_all:
            return False
        if rules.allow_all or rules.parser is None:
            return True

        return rules.parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        """The site's published Crawl-delay in seconds, if it has one."""
        rules = self._rules_for(url)
        if rules.parser is None:
            return None
        try:
            delay = rules.parser.crawl_delay(self.user_agent)
        except Exception:  # noqa: BLE001 - malformed robots.txt shouldn't crash us
            return None
        return float(delay) if delay is not None else None

    # -- internals ---------------------------------------------------------

    def _rules_for(self, url: str) -> _CachedRobots:
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"

        cached = self._cache.get(host_key)
        if cached is not None and not cached.expired:
            return cached

        rules = self._fetch(host_key)
        self._cache[host_key] = rules
        return rules

    def _fetch(self, host_key: str) -> _CachedRobots:
        robots_url = f"{host_key}/robots.txt"
        now = time.monotonic()

        try:
            response = self._get_session().get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - network trouble
            # We couldn't ask. Treat it like a 5xx and stay off the site.
            log.warning("Could not fetch %s (%s); assuming disallow", robots_url, exc)
            return _CachedRobots(parser=None, fetched_at=now, deny_all=True)

        status = response.status_code

        if 400 <= status < 500:
            # No robots.txt published. RFC 9309: crawling is unrestricted.
            log.debug("%s returned %d; no restrictions published", robots_url, status)
            return _CachedRobots(parser=None, fetched_at=now, allow_all=True)

        if status >= 500:
            # The site is having a bad day. Don't add to it.
            log.warning("%s returned %d; assuming complete disallow", robots_url, status)
            return _CachedRobots(parser=None, fetched_at=now, deny_all=True)

        parser = RobotFileParser()
        try:
            parser.parse(response.text.splitlines())
        except Exception as exc:  # noqa: BLE001 - unparseable robots.txt
            log.warning("Could not parse %s (%s); assuming disallow", robots_url, exc)
            return _CachedRobots(parser=None, fetched_at=now, deny_all=True)

        return _CachedRobots(parser=parser, fetched_at=now)


class RobotsDisallowed(Exception):
    """Raised when a source tries to fetch a path robots.txt forbids.

    This is not a failure to report as a bug — it's the system working. The
    source logs it and moves on to the next URL.
    """
