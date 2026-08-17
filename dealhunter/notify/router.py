"""
Routing: deciding what is worth sending, and when.

Three buckets, from the brief:

    score >= 75  ->  immediate alert, one message per listing
    score 55-74  ->  batched into the next digest
    score  < 55  ->  database only, stay silent

Plus two overrides that jump the queue regardless of score:

    * a standing priority rule (Helios Neo 16S AI under its trigger price,
      or a confirmed RTX 5070 12 GB)
    * a price drop of more than 5% on something already notified

**Silence is the default.** If nothing clears the bar, this returns no alerts
and no digest, and `dispatch()` sends nothing at all. A system that pings you
to say it found nothing is a system you will mute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from ..config import Config
from ..models import EvaluatedListing, Flag
from .base import Digest
from .render import AlertContent, build_alert

# Pakistan Standard Time is UTC+5 year-round — no daylight saving.
PKT = timezone(timedelta(hours=5))


@dataclass
class RoutingDecision:
    """What this run should send."""

    alerts: list[AlertContent] = field(default_factory=list)
    digest: Digest | None = None
    suppressed: list[str] = field(default_factory=list)   # fingerprints, for logging

    @property
    def is_silent(self) -> bool:
        return not self.alerts and (self.digest is None or self.digest.is_empty)


def route(
    evaluated: list[EvaluatedListing],
    config: Config,
    *,
    already_notified: dict[str, float] | None = None,
    send_digest: bool = False,
    failed_sources: list[str] | None = None,
    pending_source_approvals: list[str] | None = None,
    fx_note: str = "",
) -> RoutingDecision:
    """Sort this run's results into alerts, a digest, and silence.

    `already_notified` maps a fingerprint to the landed USD price we last told
    you about. It is what stops the same listing alerting on every run — and
    what lets a genuine price drop re-alert. Stage 5 populates it from SQLite;
    passing nothing means "treat everything as new", which is correct for a
    first run.
    """
    already_notified = already_notified or {}
    kept = [item for item in evaluated if not item.rejected and item.score]

    immediate_threshold = config.notification["immediate_alert_score"]
    digest_threshold = config.notification["digest_score"]
    drop_percent = float(config.notification["price_drop_repeat_percent"])

    decision = RoutingDecision()
    digest_candidates: list[EvaluatedListing] = []

    for item in kept:
        fingerprint = item.fingerprint
        score = item.score.total
        qualifies = score >= immediate_threshold or item.score.priority

        if not qualifies:
            if score >= digest_threshold:
                digest_candidates.append(item)
            # Below the digest threshold: logged to the database, never sent.
            continue

        previous_price = already_notified.get(fingerprint)
        if previous_price is not None:
            drop = _percent_drop(previous_price, item.landed.landed_usd)
            if drop <= drop_percent:
                # Already told you about this one and it hasn't moved enough.
                decision.suppressed.append(fingerprint)
                continue

            alert = build_alert(item, config, peers=kept)
            alert.headline_tag = _prepend(
                alert.headline_tag,
                f"📉 PRICE DROP — down {drop:.1f}% from "
                f"${previous_price:,.0f} since we last told you",
            )
            decision.alerts.append(alert)
            continue

        decision.alerts.append(build_alert(item, config, peers=kept))

    # Highest landed-value first: priority picks, then score.
    decision.alerts.sort(key=lambda a: (not a.is_priority, -a.score))

    if send_digest:
        decision.digest = build_digest(
            digest_candidates,
            evaluated,
            config,
            failed_sources=failed_sources or [],
            pending_source_approvals=pending_source_approvals or [],
            fx_note=fx_note,
        )

    return decision


def build_digest(
    digest_candidates: list[EvaluatedListing],
    all_evaluated: list[EvaluatedListing],
    config: Config,
    *,
    failed_sources: list[str],
    pending_source_approvals: list[str],
    price_drops: list[str] | None = None,
    price_rises: list[str] | None = None,
    gone: list[str] | None = None,
    fx_note: str = "",
) -> Digest:
    """Assemble the daily summary.

    The top-N list is drawn from everything at or above the digest threshold,
    not just the 55-74 band — a listing that fired an immediate alert this
    morning still belongs in the day's ranking.

    Anything below the digest threshold is excluded entirely, even when it is
    the only thing we found. "Score < 55 -> logged to the database only" means
    exactly that: a thin day produces a short digest, not a padded one.
    """
    kept = [item for item in all_evaluated if not item.rejected and item.score]
    threshold = config.notification["digest_score"]
    worth_showing = [item for item in kept if item.score.total >= threshold]
    ranked = sorted(worth_showing, key=lambda item: -item.score.total)
    top_n = int(config.notification["digest_top_n"])

    return Digest(
        top_picks=[build_alert(item, config, peers=kept) for item in ranked[:top_n]],
        price_drops=price_drops or [],
        price_rises=price_rises or [],
        gone=gone or [],
        failed_sources=failed_sources,
        pending_source_approvals=pending_source_approvals,
        listings_seen=len(all_evaluated),
        listings_rejected=sum(1 for item in all_evaluated if item.rejected),
        fx_note=fx_note,
    )


# ---------------------------------------------------------------------------
# Digest scheduling
# ---------------------------------------------------------------------------


def should_send_digest(
    now_utc: datetime,
    last_sent_utc: datetime | None,
    config: Config,
) -> bool:
    """True when this run should carry the daily digest.

    The run cron is every 8 hours, which does not line up with 09:00 PKT, and
    GitHub Actions cron drifts by several minutes under load. So rather than
    matching a clock time, we send on the **first run after the digest hour has
    passed for a given PKT day** — which is robust to drift, to a missed run,
    and to the schedule changing.

    `last_sent_utc` comes from the database in stage 5. With no record, the
    first run past today's digest hour sends one.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    hour, minute = _parse_hhmm(config.notification["digest_time_pkt"])
    now_pkt = now_utc.astimezone(PKT)
    digest_moment_today = now_pkt.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )

    if now_pkt < digest_moment_today:
        # Today's digest hour hasn't arrived yet.
        return False

    if last_sent_utc is None:
        return True

    if last_sent_utc.tzinfo is None:
        last_sent_utc = last_sent_utc.replace(tzinfo=timezone.utc)

    # Already sent one since today's digest moment? Then we're done for today.
    return last_sent_utc.astimezone(PKT) < digest_moment_today


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse "09:00" into (9, 0)."""
    try:
        parsed = time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(
            f"notification.digest_time_pkt must be HH:MM, got {value!r}"
        ) from exc
    return parsed.hour, parsed.minute


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percent_drop(previous: float, current: float) -> float:
    """How far the price fell, as a percentage. Negative means it went up."""
    if previous <= 0:
        return 0.0
    return (previous - current) / previous * 100.0


def _prepend(existing: str, line: str) -> str:
    return f"{line}\n{existing}" if existing else line


def summarise_flags(item: EvaluatedListing) -> str:
    """Comma-separated flag names, for log lines and the database."""
    return ", ".join(flag.value for flag in item.all_flags)


def is_high_risk(item: EvaluatedListing) -> bool:
    return Flag.HIGH_RISK in item.all_flags
