"""
The notifier interface.

Same contract as sources: a channel that fails must never take the run down.
If Discord is having an outage, ntfy should still fire and the console should
still print. `dispatch()` collects the failures and reports them.

Channels are configured entirely by environment variable. A channel whose
variable is unset reports itself as unconfigured and is skipped silently —
that is what makes `--dry-run` on a laptop with no secrets work.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .render import AlertContent

log = logging.getLogger(__name__)


@dataclass
class Digest:
    """The once-a-day summary.

    Price movements and gone-away listings need the database, which arrives in
    stage 5; the fields exist now so the renderers are already written for them
    and stage 5 only has to supply data.
    """

    top_picks: list[AlertContent] = field(default_factory=list)
    price_drops: list[str] = field(default_factory=list)
    price_rises: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)
    pending_source_approvals: list[str] = field(default_factory=list)
    listings_seen: int = 0
    listings_rejected: int = 0
    fx_note: str = ""

    @property
    def is_empty(self) -> bool:
        """A digest with nothing but counters isn't worth sending."""
        return not (
            self.top_picks
            or self.price_drops
            or self.price_rises
            or self.gone
            or self.failed_sources
            or self.pending_source_approvals
        )


@dataclass
class NotifyResult:
    channel: str
    sent: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.skipped_reason:
            return f"{self.channel}: skipped ({self.skipped_reason})"
        if self.errors:
            return f"{self.channel}: {self.sent} sent, {len(self.errors)} failed — " + (
                "; ".join(self.errors[:2])
            )
        return f"{self.channel}: {self.sent} message(s) sent"


class Notifier(ABC):
    """Base class for a notification channel."""

    name: str = "unnamed"

    @abstractmethod
    def is_configured(self) -> bool:
        """False when the channel's env var is unset. Skipped, not an error."""

    @abstractmethod
    def send_alert(self, alert: AlertContent) -> None:
        """Send one immediate alert. Raise on failure."""

    @abstractmethod
    def send_digest(self, digest: Digest) -> None:
        """Send the daily digest. Raise on failure."""


def dispatch(
    notifiers: list[Notifier],
    alerts: list[AlertContent],
    digest: Digest | None,
) -> list[NotifyResult]:
    """Send everything through every configured channel, isolating failures.

    Silence is a feature: with no alerts and no digest, this sends nothing at
    all rather than a "nothing found" message. You should only hear from this
    system when there is something worth seeing.
    """
    results: list[NotifyResult] = []

    for notifier in notifiers:
        result = NotifyResult(channel=notifier.name)

        if not notifier.is_configured():
            result.skipped_reason = "not configured"
            results.append(result)
            continue

        for alert in alerts:
            try:
                notifier.send_alert(alert)
                result.sent += 1
            except Exception as exc:  # noqa: BLE001 - one channel must not kill the rest
                result.errors.append(f"alert: {type(exc).__name__}: {exc}")
                log.exception("Channel %s failed sending an alert", notifier.name)

        if digest is not None and not digest.is_empty:
            try:
                notifier.send_digest(digest)
                result.sent += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"digest: {type(exc).__name__}: {exc}")
                log.exception("Channel %s failed sending the digest", notifier.name)

        results.append(result)
        log.info("%s", result.summary())

    return results
