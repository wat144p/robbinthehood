"""
Notification channels.

Discord is the primary channel and ntfy.sh the secondary; both are optional and
driven entirely by environment variables. `--dry-run` swaps both for the
console channel, which prints exactly what would have been sent.

    DISCORD_WEBHOOK_URL   channel settings -> Integrations -> New Webhook
    NTFY_TOPIC            any long random string; subscribe to it in the app
"""

from __future__ import annotations

import os

from ..config import Config
from .base import Digest, Notifier, NotifyResult, dispatch
from .console import ConsoleNotifier
from .discord import DiscordNotifier
from .email_channel import EmailNotifier
from .ntfy import NtfyNotifier
from .render import AlertContent, build_alert, explain_regional_advantage
from .router import RoutingDecision, route, should_send_digest

__all__ = [
    "AlertContent",
    "ConsoleNotifier",
    "Digest",
    "DiscordNotifier",
    "EmailNotifier",
    "NotifyResult",
    "Notifier",
    "NtfyNotifier",
    "RoutingDecision",
    "build_alert",
    "build_notifiers",
    "dispatch",
    "explain_regional_advantage",
    "route",
    "should_send_digest",
]


def build_notifiers(config: Config, dry_run: bool = False) -> list[Notifier]:
    """Instantiate the channels this run should use.

    `--dry-run` means stdout and nothing else — no partial sending, no "well,
    ntfy was configured so I sent that one". Dry means dry.
    """
    if dry_run:
        return [ConsoleNotifier()]

    channels = (config.notification.get("channels") or {})
    notifiers: list[Notifier] = []

    discord_cfg = channels.get("discord") or {}
    if discord_cfg.get("enabled", True):
        notifiers.append(
            DiscordNotifier(env_var=discord_cfg.get("webhook_env", "DISCORD_WEBHOOK_URL"))
        )

    ntfy_cfg = channels.get("ntfy") or {}
    if ntfy_cfg.get("enabled", True):
        notifiers.append(
            NtfyNotifier(
                server=ntfy_cfg.get("server", "https://ntfy.sh"),
                topic_env=ntfy_cfg.get("topic_env", "NTFY_TOPIC"),
            )
        )

    email_cfg = channels.get("email") or {}
    if email_cfg.get("enabled", True):
        notifiers.append(EmailNotifier())

    # If nothing is actually configured, fall back to stdout rather than doing
    # the work and silently discarding the results.
    if not any(notifier.is_configured() for notifier in notifiers):
        return [ConsoleNotifier()]

    return notifiers


def configured_channel_names(notifiers: list[Notifier]) -> list[str]:
    return [n.name for n in notifiers if n.is_configured()]
