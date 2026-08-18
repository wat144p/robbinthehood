"""
Source discovery — finding new deal sites, monthly.

The configured source list will go stale. Communities die, trackers launch,
sites move behind bot walls. On the first run of each month this does a
discovery pass and writes what it found to `discovered_sources.yaml`.

**Nothing here ever auto-enables a scraper.** Candidates are written to the
file with a confidence score and surfaced in the next digest for you to
approve. A scraper that turns itself on is a scraper that gets you IP-banned
by a site you never chose to visit.

What it actually checks, per candidate:

    1. Does it respond at all, and does robots.txt permit us?
    2. Does it publish an RSS/Atom feed? (that's the cheap, stable path)
    3. Does the page contain structured listings with prices?
    4. Does it cover any of our seven regions?

The candidate list itself comes from two places: the subreddit sidebars and
wikis of communities we already read, and a seed list in config. Following
links out of communities you already trust is a better signal than a search
engine, because those links were curated by people buying the same hardware.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import Config
from .models import Region
from .robots import RobotsCache
from .sources.base import HttpPolicy

log = logging.getLogger(__name__)

# Signals that a page is about the hardware we care about.
RELEVANCE_MARKERS = (
    "gaming laptop", "laptop deal", "rtx 50", "rtx 40", "legion", "predator",
    "notebook", "rog strix", "tuf gaming", "omen", "nitro",
)

# Region hints, so a candidate can be tagged without us reading it ourselves.
REGION_MARKERS: dict[Region, tuple[str, ...]] = {
    Region.US: ("usd", "$", "best buy", "newegg", "b&h", "micro center"),
    Region.CA: ("cad", "c$", "canada computers", "memory express", ".ca"),
    Region.GB: ("gbp", "£", "currys", "scan.co.uk", "overclockers", ".co.uk"),
    Region.DE: ("eur", "€", "notebooksbilliger", "alternate", "mindfactory", ".de"),
    Region.BE: ("belgi", ".be"),
    Region.SE: ("sek", " kr", "webhallen", "inet.se", "komplett", ".se"),
    Region.AU: ("aud", "a$", "pccasegear", "scorptec", ".com.au"),
}

FEED_LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# Must tolerate thousands separators: a naive \d{2,5} fails on "$1,199"
# because the comma breaks the digit run, and then every candidate looks
# price-free. The character class absorbs separators mid-number.
PRICE_RE = re.compile(
    r"[$£€]\s?\d[\d.,\s]{0,9}\d"
    r"|\d[\d.,\s]{0,9}\d\s?(?:kr|EUR|USD|GBP|CAD|AUD|SEK)\b"
)


@dataclass
class Candidate:
    """A possible new source, awaiting your sign-off."""

    url: str
    name: str = ""
    discovered_from: str = ""
    confidence: float = 0.0
    regions: list[str] = field(default_factory=list)
    feed_url: str | None = None
    robots_allows: bool = False
    has_prices: bool = False
    notes: str = ""
    first_seen: str = ""
    status: str = "pending"     # pending | approved | rejected — you set this

    @property
    def is_unreachable(self) -> bool:
        """True when this was never actually evaluated — a network failure,
        not a genuine 'this site isn't worth adding' assessment."""
        return self.notes.startswith("unreachable")

    def summary(self) -> str:
        if self.is_unreachable:
            return f"{self.url} — could not reach it (network issue, not evaluated)"
        regions = ",".join(self.regions) or "region unclear"
        feed = " (has RSS)" if self.feed_url else ""
        return f"{self.name or self.url} — {regions}, confidence {self.confidence:.2f}{feed}"


class SourceDiscovery:
    """Runs the monthly discovery pass."""

    def __init__(self, config: Config, session=None, sleep=time.sleep):
        self.config = config
        self.settings = (config.raw_sources or {}).get("discovery") or {}
        self.policy = HttpPolicy.from_config(config)
        self._session = session
        self._sleep = sleep
        self.robots = RobotsCache(user_agent=self.policy.user_agent, session=session)

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    @property
    def output_path(self) -> Path:
        from .config import PROJECT_ROOT

        path = Path(self.settings.get("output_path", "discovered_sources.yaml"))
        return path if path.is_absolute() else PROJECT_ROOT / path

    # -- scheduling --------------------------------------------------------

    def should_run(self, now: datetime | None = None, last_run: datetime | None = None) -> bool:
        """True on the first run of a new month.

        Monthly, not every run: this makes a couple of dozen requests to sites
        we have no relationship with, and doing that eight times a day would be
        rude and would get us blocked.
        """
        if not self.settings.get("enabled", True):
            return False

        now = now or datetime.now(timezone.utc)
        if last_run is None:
            return True

        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)

        return (now.year, now.month) != (last_run.year, last_run.month)

    # -- the pass ----------------------------------------------------------

    def run(self) -> list[Candidate]:
        """Evaluate every candidate and merge the results into the YAML file."""
        candidates: dict[str, Candidate] = {}

        for url in self.settings.get("seed_candidates") or []:
            candidates[url] = Candidate(url=url, discovered_from="config seed")

        for subreddit in self.settings.get("harvest_subreddits") or []:
            for url, source in self._harvest_subreddit(subreddit):
                candidates.setdefault(url, Candidate(url=url, discovered_from=source))

        evaluated = []
        for candidate in candidates.values():
            if self._already_known(candidate.url):
                continue
            try:
                evaluated.append(self._evaluate(candidate))
            except Exception as exc:  # noqa: BLE001 — discovery is best-effort
                log.debug("Could not evaluate %s: %s", candidate.url, exc)

        merged = self._merge_and_save(evaluated)
        pending = [c for c in merged if c.status == "pending"]

        # If every single candidate this pass came back unreachable, the
        # network was down for the whole pass, not genuinely evaluated as
        # low-value. Surfacing three "new sources found!" lines in the digest
        # over a DNS outage would be actively misleading. Everything is still
        # saved above (still marked pending), so a healthy pass retries them
        # properly next time — this only affects what gets reported NOW.
        if evaluated and all(c.is_unreachable for c in evaluated):
            log.warning(
                "Source discovery could not reach any of %d candidate(s) this "
                "pass — looks like a network outage, not a real evaluation. "
                "Nothing surfaced for approval; will retry next scheduled pass.",
                len(evaluated),
            )
            return []

        # A partial outage still shouldn't surface the unreachable ones
        # individually as if they were assessed — just the genuine finds.
        return [c for c in pending if not c.is_unreachable]

    def _already_known(self, url: str) -> bool:
        """Skip anything we already ingest — no point rediscovering OzBargain.

        Note what is excluded from the comparison: the `discovery` block itself.
        Its `seed_candidates` are URLs we want to *evaluate*, so matching
        against them would make every seed look already-known and silently
        disable the entire seed list.
        """
        active_sources = {
            name: block
            for name, block in (self.config.raw_sources or {}).items()
            if name != "discovery"
        }
        blob = yaml.safe_dump(active_sources)
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        return host in blob

    def _harvest_subreddit(self, subreddit: str) -> list[tuple[str, str]]:
        """Pull outbound links from a subreddit's sidebar and wiki.

        Communities curate these lists, which makes them a much better signal
        than a search engine — the links were put there by people buying the
        same hardware in the same regions.
        """
        found: list[tuple[str, str]] = []

        for path in (f"/r/{subreddit}/about.json", f"/r/{subreddit}/wiki/index.json"):
            try:
                response = self.session.get(
                    f"https://www.reddit.com{path}",
                    headers={"User-Agent": self.policy.user_agent},
                    timeout=self.policy.timeout_seconds,
                )
                if response.status_code != 200:
                    continue
                blob = response.text
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not harvest r/%s%s: %s", subreddit, path, exc)
                continue

            for match in re.finditer(r"https?://[\w.-]+\.[a-z]{2,}(?:/[\w./-]*)?", blob):
                url = match.group(0).rstrip(").,")
                if "reddit.com" in url or "redd.it" in url:
                    continue
                found.append((url, f"r/{subreddit} sidebar/wiki"))

            self._sleep(self.policy.request_delay_seconds)

        return found

    def _evaluate(self, candidate: Candidate) -> Candidate:
        """Score one candidate on the four criteria from the brief."""
        candidate.first_seen = datetime.now(timezone.utc).date().isoformat()
        candidate.robots_allows = self.robots.is_allowed(candidate.url)

        if not candidate.robots_allows:
            # RobotsCache treats "could not fetch robots.txt at all" (DNS
            # down, host unreachable) and "fetched it, and it says no" as the
            # SAME conservative deny — correctly, for the purpose of deciding
            # whether to proceed. But those are very different things to
            # report back: one is a real finding, the other is "we could not
            # check." A candidate we could not even reach is not a genuine
            # evaluation and must not be surfaced as one — see run().
            if self._is_network_failure(candidate.url):
                candidate.confidence = 0.0
                candidate.notes = "unreachable: could not connect"
            else:
                candidate.confidence = 0.0
                candidate.notes = "robots.txt disallows crawling"
            return candidate

        try:
            response = self.session.get(
                candidate.url,
                headers={"User-Agent": self.policy.user_agent},
                timeout=self.policy.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            candidate.confidence = 0.0
            candidate.notes = f"unreachable: {type(exc).__name__}"
            return candidate

        if response.status_code != 200:
            candidate.confidence = 0.0
            candidate.notes = f"HTTP {response.status_code}"
            return candidate

        html = response.text
        lowered = html.lower()

        candidate.name = _page_title(html) or candidate.url
        candidate.has_prices = bool(PRICE_RE.search(html))
        candidate.regions = sorted(
            region.value
            for region, markers in REGION_MARKERS.items()
            if any(marker in lowered for marker in markers)
        )

        feed_match = FEED_LINK_RE.search(html)
        if feed_match:
            candidate.feed_url = feed_match.group(1)

        candidate.confidence = self._score(candidate, lowered)
        candidate.notes = self._notes(candidate)
        self._sleep(self.policy.request_delay_seconds)
        return candidate

    def _is_network_failure(self, url: str) -> bool:
        """Was robots.txt actually fetched and found to say no, or could we
        just not reach the host at all?

        RobotsCache does not expose this distinction — by design, both cases
        deny conservatively. This does one extra GET, only for candidates
        that were denied, which is a small minority of a monthly pass, purely
        to report the right reason back rather than to change any decision.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            self.session.get(
                robots_url,
                headers={"User-Agent": self.policy.user_agent},
                timeout=self.policy.timeout_seconds,
            )
            return False    # got SOME response — a real disallow, not a network failure
        except Exception:  # noqa: BLE001
            return True

    def _score(self, candidate: Candidate, lowered: str) -> float:
        """Confidence, 0-1. Weighted toward things that make ingestion cheap."""
        score = 0.0

        relevance = sum(1 for marker in RELEVANCE_MARKERS if marker in lowered)
        score += min(0.35, relevance * 0.07)      # is it about our hardware?

        if candidate.has_prices:
            score += 0.25                          # structured listings with prices
        if candidate.feed_url:
            score += 0.25                          # an RSS feed is the cheap path
        our_regions = {r.value for r in self.config.enabled_regions()}
        if set(candidate.regions) & our_regions:
            score += 0.15                          # covers a region we can use

        return round(min(1.0, score), 2)

    def _notes(self, candidate: Candidate) -> str:
        notes = []
        if candidate.feed_url:
            notes.append("has an RSS feed — can be added as an rss source")
        elif candidate.has_prices:
            notes.append("prices present but no feed — needs an html source with selectors")
        else:
            notes.append("no prices found in the server-rendered HTML; may be JS-rendered")
        if not candidate.regions:
            notes.append("could not determine region")
        return "; ".join(notes)

    # -- persistence -------------------------------------------------------

    def _merge_and_save(self, found: list[Candidate]) -> list[Candidate]:
        """Merge into discovered_sources.yaml, preserving your decisions.

        A candidate you already rejected stays rejected — rediscovering it next
        month must not put it back in front of you.
        """
        existing = self.load_existing()
        by_url = {c.url: c for c in existing}

        for candidate in found:
            previous = by_url.get(candidate.url)
            if previous is not None:
                # Refresh the facts, keep your verdict and the original date.
                candidate.status = previous.status
                candidate.first_seen = previous.first_seen or candidate.first_seen
            by_url[candidate.url] = candidate

        merged = sorted(by_url.values(), key=lambda c: -c.confidence)
        self.save(merged)
        return merged

    def load_existing(self) -> list[Candidate]:
        if not self.output_path.exists():
            return []
        try:
            with open(self.output_path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self.output_path, exc)
            return []

        return [Candidate(**entry) for entry in (data.get("candidates") or [])]

    def save(self, candidates: list[Candidate]) -> None:
        payload = {
            "_comment": (
                "Discovered by the monthly source-discovery pass. Nothing here is "
                "active. To enable one, set its status to 'approved', then add a "
                "matching entry under sources.rss.feeds or sources.html.sites in "
                "config.yaml. Setting status to 'rejected' stops it being "
                "resurfaced next month."
            ),
            "last_run": datetime.now(timezone.utc).isoformat(),
            "candidates": [asdict(c) for c in candidates],
        }

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)

        log.info("Wrote %d candidates to %s", len(candidates), self.output_path)


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _page_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    return " ".join(match.group(1).split())[:120] if match else ""
