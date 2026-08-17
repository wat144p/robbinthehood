"""
Discord webhook channel — the primary notification path.

No auth, no app registration: create a webhook in a channel's settings, copy
the URL into `DISCORD_WEBHOOK_URL`, done. It renders rich embeds well, which
matters here because an alert carries a dozen distinct facts and a wall of
plain text buries the important ones.

Discord's documented limits, all enforced below because exceeding any of them
returns a 400 and silently loses the message:

    embed title           256 characters
    embed description   4,096
    field name            256
    field value         1,024
    fields per embed       25
    embeds per message     10
    total per message   6,000 characters across all embeds

Rate limit: roughly 5 requests per 2 seconds per webhook. We honour the
`retry_after` value Discord returns on a 429 rather than guessing.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from .base import Digest, Notifier
from .render import AlertContent, truncate

log = logging.getLogger(__name__)

# Documented Discord limits.
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024
MAX_FIELDS = 25
MAX_EMBEDS = 10
MAX_TOTAL_CHARS = 6000

# Embed colours, chosen so the bucket is readable at a glance in the sidebar.
COLOUR_PRIORITY = 0xE01E37      # red — drop everything
COLOUR_RECORD_LOW = 0xF2A900    # amber — new record low
COLOUR_ALERT = 0x2ECC71         # green — cleared the alert threshold
COLOUR_DIGEST = 0x3498DB        # blue — daily summary


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(
        self,
        webhook_url: str | None = None,
        session: Any = None,
        env_var: str = "DISCORD_WEBHOOK_URL",
        timeout: float = 15.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ):
        self.webhook_url = webhook_url or os.environ.get(env_var, "")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session
        self._sleep = sleep

    # -- plumbing ----------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _post(self, payload: dict[str, Any]) -> None:
        """POST with Discord's own rate-limit handling."""
        for attempt in range(self.max_retries):
            response = self.session.post(
                self.webhook_url, json=payload, timeout=self.timeout
            )

            # 204 is the documented success for a webhook execute.
            if response.status_code in (200, 204):
                return

            if response.status_code == 429:
                # Discord tells us exactly how long to wait; don't guess.
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except Exception:  # noqa: BLE001 - malformed body, use a default
                    retry_after = 1.0
                log.warning("Discord rate limited; waiting %.2fs", retry_after)
                self._sleep(retry_after)
                continue

            if response.status_code >= 500:
                self._sleep(2.0 * (attempt + 1))
                continue

            # 400 almost always means we exceeded a limit. Include the body,
            # because Discord's error tells you exactly which field was wrong.
            body = ""
            try:
                body = str(response.json())[:400]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"Discord returned HTTP {response.status_code}: {body}")

        raise RuntimeError("Discord rejected the message after retries")

    # -- alerts ------------------------------------------------------------

    def send_alert(self, alert: AlertContent) -> None:
        self._post({"embeds": [self._alert_embed(alert)]})

    def _alert_embed(self, alert: AlertContent) -> dict[str, Any]:
        """One listing as a Discord embed.

        The price line goes in the description rather than a field so it is the
        first thing rendered under the title — the local sticker and the landed
        USD figure sit together, which is the whole point.
        """
        description_parts = [f"**{alert.price_line}**"]

        if alert.in_target_zone:
            description_parts.append("_inside the $1,000–1,200 target zone_")
        if alert.regional_advantage:
            description_parts.append(alert.regional_advantage)
        description_parts.append(alert.floor_line)

        if alert.warnings:
            description_parts.append(
                "\n".join(f"⚠ {warning}" for warning in alert.warnings)
            )

        fields = [
            {
                "name": "Spec",
                "value": truncate(alert.spec_line, MAX_FIELD_VALUE),
                "inline": False,
            },
            {
                "name": "Landed cost",
                "value": truncate(f"```{alert.landed_breakdown}```", MAX_FIELD_VALUE),
                "inline": False,
            },
            {
                "name": "Keyboard",
                "value": truncate(alert.keyboard_line, MAX_FIELD_VALUE),
                "inline": False,
            },
            {
                "name": "Condition & seller",
                "value": truncate(alert.condition_line, MAX_FIELD_VALUE),
                "inline": False,
            },
            {
                "name": f"Score {alert.score:g}/100",
                "value": truncate(alert.score_line, MAX_FIELD_VALUE),
                "inline": False,
            },
        ]

        if alert.warranty_line:
            fields.append({
                "name": "Warranty",
                "value": truncate(alert.warranty_line, MAX_FIELD_VALUE),
                "inline": False,
            })

        embed = {
            "title": truncate(
                f"{alert.region_flag} {alert.title}", MAX_TITLE
            ),
            "url": alert.url,
            "color": self._colour_for(alert),
            "description": truncate("\n".join(description_parts), MAX_DESCRIPTION),
            "fields": fields[:MAX_FIELDS],
            "footer": {"text": f"{alert.region_display} · robbin-the-hood"},
        }

        if alert.headline_tag:
            # The loud banner goes above the embed as message content, so it
            # shows up in the notification preview on a phone.
            embed["author"] = {"name": truncate(alert.headline_tag, MAX_TITLE)}

        return embed

    def _colour_for(self, alert: AlertContent) -> int:
        if alert.is_priority:
            return COLOUR_PRIORITY
        if "RECORD LOW" in alert.headline_tag:
            return COLOUR_RECORD_LOW
        return COLOUR_ALERT

    # -- digest ------------------------------------------------------------

    def send_digest(self, digest: Digest) -> None:
        """The daily summary, as a single embed.

        Deliberately one embed rather than one per pick: the digest is for
        skimming, and ten full embeds is a wall. Anything that deserves detail
        already fired as an immediate alert.
        """
        lines = []

        if digest.top_picks:
            lines.append("**Top picks, ranked by score**")
            for index, pick in enumerate(digest.top_picks, start=1):
                zone = " ⭐" if pick.in_target_zone else ""
                lines.append(
                    f"`{index:>2}.` **${pick.landed_usd:,.0f}**{zone} "
                    f"{pick.region_flag} [{truncate(pick.title, 70)}]({pick.url})"
                )
                lines.append(f"       {pick.price_line} · score {pick.score:g}")

        for heading, entries in (
            ("📉 Price drops", digest.price_drops),
            ("📈 Price rises", digest.price_rises),
            ("🚫 Gone (not seen in 7 days)", digest.gone),
        ):
            if entries:
                lines.append(f"\n**{heading}**")
                lines.extend(f"• {entry}" for entry in entries[:10])

        if digest.pending_source_approvals:
            lines.append("\n**🔎 New sources found — awaiting your approval**")
            lines.extend(f"• {entry}" for entry in digest.pending_source_approvals[:10])

        if digest.failed_sources:
            lines.append("\n**⚠ Sources that failed this run**")
            lines.extend(f"• {entry}" for entry in digest.failed_sources[:10])

        if digest.fx_note:
            lines.append(f"\n_{digest.fx_note}_")

        lines.append(
            f"\n_{digest.listings_seen} listings seen, "
            f"{digest.listings_rejected} rejected by hard filters._"
        )

        self._post({
            "embeds": [{
                "title": "Daily digest",
                "color": COLOUR_DIGEST,
                "description": truncate("\n".join(lines), MAX_DESCRIPTION),
                "footer": {"text": "robbin-the-hood · 09:00 PKT"},
            }]
        })
