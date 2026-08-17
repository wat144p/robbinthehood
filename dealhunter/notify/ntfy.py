"""
ntfy.sh channel — secondary, for phone push.

Free, no account, no auth: pick an unguessable topic name, subscribe to it in
the ntfy app, and POST plain text to `https://ntfy.sh/<topic>`.

Security note worth knowing: **anyone who knows the topic name can read it, and
publish to it.** Treat the topic as a shared secret — use a long random string,
not "laptop-deals". It is set via `NTFY_TOPIC` and never committed.

Formatting is deliberately terse. This is the channel that buzzes your phone,
so it carries the decision-relevant facts — price, landed cost, score, the loud
warnings — and links out to Discord or the listing for the rest.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from .base import Digest, Notifier
from .render import AlertContent, truncate

log = logging.getLogger(__name__)

# ntfy passes headers through HTTP, so they must be latin-1 encodable. Emoji in
# a Title header will raise; ntfy's own `Tags` header is the way to get icons.
MAX_TITLE = 200
MAX_BODY = 4000


class NtfyNotifier(Notifier):
    name = "ntfy"

    def __init__(
        self,
        topic: str | None = None,
        server: str = "https://ntfy.sh",
        session: Any = None,
        topic_env: str = "NTFY_TOPIC",
        timeout: float = 15.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ):
        self.topic = topic or os.environ.get(topic_env, "")
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session
        self._sleep = sleep

    def is_configured(self) -> bool:
        return bool(self.topic)

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    @property
    def url(self) -> str:
        return f"{self.server}/{self.topic}"

    def _post(self, body: str, headers: dict[str, str]) -> None:
        for attempt in range(self.max_retries):
            response = self.session.post(
                self.url,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return
            if response.status_code == 429 or response.status_code >= 500:
                self._sleep(2.0 * (attempt + 1))
                continue
            raise RuntimeError(f"ntfy returned HTTP {response.status_code}")

        raise RuntimeError("ntfy rejected the message after retries")

    # -- alerts ------------------------------------------------------------

    def send_alert(self, alert: AlertContent) -> None:
        """One push per listing, priority-tagged by how much it matters."""
        priority = "urgent" if alert.is_priority else "high"

        tags = ["computer"]
        if alert.is_priority:
            tags.append("rotating_light")
        if "RECORD LOW" in alert.headline_tag:
            tags.append("trophy")
        if any("HIGH RISK" in warning for warning in alert.warnings):
            tags.append("warning")

        # Header values must be latin-1 safe, so the title is ASCII only.
        title = _ascii_only(
            f"${alert.landed_usd:,.0f} landed · {alert.region_display} · "
            f"score {alert.score:g}"
        )

        body_lines = [alert.title, "", alert.price_line]
        if alert.headline_tag:
            body_lines.insert(0, _strip_emoji(alert.headline_tag))
        if alert.regional_advantage:
            body_lines.append(alert.regional_advantage)
        body_lines.append(alert.floor_line)
        body_lines.append(alert.spec_line)
        body_lines.append(f"keyboard: {alert.keyboard_line}")
        body_lines.append(f"condition: {alert.condition_line}")
        for warning in alert.warnings:
            body_lines.append(f"! {warning}")
        if alert.warranty_line:
            body_lines.append(f"warranty: {alert.warranty_line}")

        self._post(
            truncate("\n".join(body_lines), MAX_BODY),
            {
                "Title": truncate(title, MAX_TITLE),
                "Priority": priority,
                "Tags": ",".join(tags),
                "Click": alert.url,     # tapping the push opens the listing
            },
        )

    # -- digest ------------------------------------------------------------

    def send_digest(self, digest: Digest) -> None:
        lines = []

        for index, pick in enumerate(digest.top_picks, start=1):
            lines.append(
                f"{index}. ${pick.landed_usd:,.0f} [{pick.region_display}] "
                f"{truncate(pick.title, 60)}"
            )
            lines.append(f"   {pick.price_line} · score {pick.score:g}")

        for heading, entries in (
            ("Price drops", digest.price_drops),
            ("Price rises", digest.price_rises),
            ("Gone", digest.gone),
            ("New sources awaiting approval", digest.pending_source_approvals),
            ("Failed sources", digest.failed_sources),
        ):
            if entries:
                lines.append(f"\n{heading}:")
                lines.extend(f"- {entry}" for entry in entries[:8])

        if digest.fx_note:
            lines.append(f"\n{digest.fx_note}")

        self._post(
            truncate("\n".join(lines) or "Nothing to report.", MAX_BODY),
            {
                "Title": _ascii_only(
                    f"Daily digest - {len(digest.top_picks)} picks"
                ),
                "Priority": "default",
                "Tags": "newspaper",
            },
        )


def _ascii_only(text: str) -> str:
    """HTTP headers must be latin-1 encodable; ntfy titles are headers."""
    return text.encode("ascii", "replace").decode("ascii")


def _strip_emoji(text: str) -> str:
    """Body text is UTF-8 and fine, but the banners read better without icons."""
    return "".join(ch for ch in text if ch.isascii()).strip()
