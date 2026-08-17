"""
The common source interface.

Every source — eBay, Best Buy, a Reddit JSON feed, an RSS bridge — implements
`Source` and returns plain `Listing` objects. Nothing downstream knows or cares
where a listing came from.

Three rules that every source must honour:

1. **Failures are never fatal.** A broken source must not take the run down.
   `run_sources` catches everything, records it, and carries on with the rest.
   The digest reports what failed.
2. **Be a polite client.** Descriptive User-Agent, a delay between requests,
   exponential backoff on 429, and a hard request budget per run.
3. **Report prices in the region's own currency.** Never pre-convert. The
   landed-cost maths applies each region's tax rules to its own currency, and
   a helpfully-converted price silently breaks that.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Config
from ..models import Listing, Region

log = logging.getLogger(__name__)


class SourceError(Exception):
    """A source failed in a way that should be reported, not raised further."""


class SourceBlocked(SourceError):
    """The source is refusing us: captcha, 403, or a rate-limit ban.

    Raising this auto-disables the source for the remainder of the run. Never
    retry into a ban — log it, report it in the digest, and move on.
    """


class SourceAuthError(SourceError):
    """Credentials are missing or rejected. Almost always a setup problem."""


@dataclass
class SourceResult:
    """What one source produced, including how it went wrong."""

    name: str
    listings: list[Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked: bool = False
    skipped_reason: str | None = None
    requests_made: int = 0
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.blocked

    def summary(self) -> str:
        if self.skipped_reason:
            return f"{self.name}: skipped ({self.skipped_reason})"
        if self.blocked:
            return f"{self.name}: BLOCKED — {'; '.join(self.errors)}"
        if self.errors:
            return (
                f"{self.name}: {len(self.listings)} listings, "
                f"{len(self.errors)} error(s) — {'; '.join(self.errors[:3])}"
            )
        return (
            f"{self.name}: {len(self.listings)} listings "
            f"({self.requests_made} requests, {self.duration_seconds:.1f}s)"
        )


class Source(ABC):
    """Base class for every source module.

    Subclasses implement `fetch()`. They may raise anything — the runner
    converts exceptions into a `SourceResult` with the error recorded.
    """

    #: Stable identifier, used as the `source` field on every Listing and as
    #: the first half of its dedup fingerprint. Never rename it casually.
    name: str = "unnamed"

    #: Regions this source can produce listings for. Informational; used by the
    #: runner to skip a source entirely when all its regions are disabled.
    regions: tuple[Region, ...] = ()

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def fetch(self) -> list[Listing]:
        """Return every listing this source can see right now.

        Deduplication, scoring and notification all happen downstream. A source
        should return everything plausible and let the filters do their job.
        """

    def is_enabled(self) -> bool:
        """Whether the config has this source turned on."""
        block = (self.config.raw_sources or {}).get(self.name) or {}
        return bool(block.get("enabled", True))

    def active_regions(self) -> list[Region]:
        """This source's regions, minus any disabled in config."""
        enabled = set(self.config.enabled_regions())
        return [region for region in self.regions if region in enabled]


# ---------------------------------------------------------------------------
# Polite HTTP helper
# ---------------------------------------------------------------------------


@dataclass
class HttpPolicy:
    """Shared HTTP manners, loaded from `sources.http` in config.yaml."""

    user_agent: str
    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_seconds: float = 2.0
    request_delay_seconds: float = 0.5
    max_requests_per_run: int = 200

    @classmethod
    def from_config(cls, config: Config) -> "HttpPolicy":
        block = (config.raw_sources or {}).get("http") or {}
        return cls(
            user_agent=block.get(
                "user_agent",
                "robbin-the-hood/0.1 (personal laptop price tracker)",
            ),
            timeout_seconds=float(block.get("timeout_seconds", 20)),
            max_retries=int(block.get("max_retries", 3)),
            backoff_seconds=float(block.get("backoff_seconds", 2.0)),
            request_delay_seconds=float(block.get("request_delay_seconds", 0.5)),
            max_requests_per_run=int(block.get("max_requests_per_run", 200)),
        )


class RequestBudget:
    """Counts requests and enforces a delay between them.

    A source that hits its budget stops early rather than continuing — better
    to return partial results than to get the API key throttled.
    """

    def __init__(self, policy: HttpPolicy, sleep=time.sleep):
        self.policy = policy
        self.count = 0
        self._sleep = sleep
        self._last_request_at: float | None = None

    @property
    def exhausted(self) -> bool:
        return self.count >= self.policy.max_requests_per_run

    def wait_turn(self) -> None:
        """Sleep just long enough to honour the configured request delay."""
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.policy.request_delay_seconds - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = time.monotonic()
        self.count += 1


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_sources(sources: list[Source]) -> list[SourceResult]:
    """Run every source, isolating failures.

    This is the "fail loudly, but keep going" contract from the brief: one
    source breaking must never cost you the other six, and every failure has to
    end up somewhere you'll actually see it — the digest.
    """
    results: list[SourceResult] = []

    for source in sources:
        result = SourceResult(name=source.name)
        started = time.monotonic()

        if not source.is_enabled():
            result.skipped_reason = "disabled in config"
            results.append(result)
            continue

        if source.regions and not source.active_regions():
            result.skipped_reason = "all of its regions are disabled"
            results.append(result)
            continue

        try:
            result.listings = source.fetch()
        except SourceBlocked as exc:
            # Captcha or ban. Disable and report; do not retry into it.
            result.blocked = True
            result.errors.append(str(exc))
            log.error("Source %s is blocked and has been disabled: %s", source.name, exc)
        except SourceAuthError as exc:
            result.errors.append(f"auth: {exc}")
            log.error("Source %s auth failure: %s", source.name, exc)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            result.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("Source %s failed", source.name)

        result.requests_made = getattr(source, "requests_made", 0)
        result.duration_seconds = time.monotonic() - started
        results.append(result)
        log.info("%s", result.summary())

    return results


def collect_listings(results: list[SourceResult]) -> list[Listing]:
    """Flatten source results into one list, de-duplicated by fingerprint.

    The same machine genuinely does appear on two sources (an eBay listing that
    also got posted to r/LaptopDeals). First one wins; stage 5's database does
    the durable cross-run deduplication.
    """
    seen: set[str] = set()
    listings: list[Listing] = []

    for result in results:
        for listing in result.listings:
            fingerprint = listing.fingerprint()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            listings.append(listing)

    return listings


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
