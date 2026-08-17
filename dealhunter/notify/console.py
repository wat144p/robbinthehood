"""
Console channel — what `--dry-run` uses.

Always "configured", never fails, prints to stdout. Its job is to let you see
exactly what would have been sent before you point the thing at a real webhook.
"""

from __future__ import annotations

import sys
from typing import TextIO

from .base import Digest, Notifier
from .render import AlertContent


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, stream: TextIO | None = None):
        self.stream = stream or sys.stdout

    def is_configured(self) -> bool:
        return True

    def send_alert(self, alert: AlertContent) -> None:
        print("─" * 78, file=self.stream)
        print("IMMEDIATE ALERT", file=self.stream)
        print("─" * 78, file=self.stream)
        print(alert.plain_text(), file=self.stream)
        print(file=self.stream)
        # The full landed derivation is verbose, so it goes below the summary
        # rather than inline — but it is always available, because "why is this
        # $1,148?" is the first question you'll ask.
        print("  landed cost derivation:", file=self.stream)
        for line in alert.landed_breakdown.splitlines():
            print(f"    {line}", file=self.stream)
        print(file=self.stream)

    def send_digest(self, digest: Digest) -> None:
        print("═" * 78, file=self.stream)
        print("DAILY DIGEST", file=self.stream)
        print("═" * 78, file=self.stream)

        if digest.top_picks:
            print("\nTop picks, ranked by score:", file=self.stream)
            for index, pick in enumerate(digest.top_picks, start=1):
                zone = " [target zone]" if pick.in_target_zone else ""
                print(
                    f"{index:>3}. ${pick.landed_usd:>8,.0f}{zone}  "
                    f"{pick.region_flag} {pick.title[:64]}",
                    file=self.stream,
                )
                print(
                    f"      {pick.price_line} · score {pick.score:g}",
                    file=self.stream,
                )
                print(f"      {pick.url}", file=self.stream)

        for heading, entries in (
            ("Price drops", digest.price_drops),
            ("Price rises", digest.price_rises),
            ("Gone (not seen in 7 days)", digest.gone),
            ("New sources found — awaiting your approval", digest.pending_source_approvals),
            ("Sources that failed", digest.failed_sources),
        ):
            if entries:
                print(f"\n{heading}:", file=self.stream)
                for entry in entries:
                    print(f"  • {entry}", file=self.stream)

        if digest.fx_note:
            print(f"\n{digest.fx_note}", file=self.stream)

        print(
            f"\n{digest.listings_seen} listings seen, "
            f"{digest.listings_rejected} rejected by hard filters.",
            file=self.stream,
        )
        print(file=self.stream)
