"""
Email channel, over plain SMTP.

Uses only the standard library — no new dependency, and no third-party service
holding your credentials. Named `email_channel` rather than `email` because a
module called `email.py` inside a package shadows the stdlib `email` package
that this very file imports, which produces a genuinely baffling ImportError.

Setup with Gmail (the common case):

    1. Enable 2-factor auth on the account (Google requires it for step 2).
    2. myaccount.google.com/apppasswords -> generate a 16-character app password.
       Your normal Google password will NOT work; Google blocks it for SMTP.
    3. Set:
         SMTP_HOST=smtp.gmail.com
         SMTP_PORT=587
         SMTP_USER=you@gmail.com
         SMTP_PASSWORD=<the 16-character app password, spaces optional>
         EMAIL_TO=you@gmail.com

Any other provider works the same way; only the host and port change:

    Outlook / Hotmail   smtp-mail.outlook.com   587
    Fastmail            smtp.fastmail.com       465   (implicit TLS)
    Proton (via Bridge) 127.0.0.1               1025

Port 465 uses implicit TLS and 587 uses STARTTLS; this module picks the right
one from the port number, which is the part people usually get wrong.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .base import Digest, Notifier
from .render import AlertContent

log = logging.getLogger(__name__)

# Ports that speak TLS from the first byte, rather than upgrading via STARTTLS.
IMPLICIT_TLS_PORTS = {465, 993}


class EmailNotifier(Notifier):
    name = "email"

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        recipients: str | list[str] | None = None,
        sender: str | None = None,
        *,
        smtp_factory=None,
    ):
        """`smtp_factory` is injectable so the tests exercise the real message
        construction without opening a socket."""
        self.host = host or os.environ.get("SMTP_HOST", "")
        self.port = int(port or os.environ.get("SMTP_PORT", 587))
        self.user = user or os.environ.get("SMTP_USER", "")
        # Gmail displays app passwords in groups of four; people paste the
        # spaces along with them and then wonder why authentication fails.
        self.password = (password or os.environ.get("SMTP_PASSWORD", "")).replace(" ", "")

        raw_recipients = recipients if recipients is not None else os.environ.get("EMAIL_TO", "")
        if isinstance(raw_recipients, str):
            raw_recipients = [r.strip() for r in raw_recipients.split(",") if r.strip()]
        self.recipients: list[str] = list(raw_recipients)

        self.sender = sender or os.environ.get("EMAIL_FROM") or self.user
        self._smtp_factory = smtp_factory

    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.recipients)

    # -- transport ---------------------------------------------------------

    def _send(self, message: EmailMessage) -> None:
        if self._smtp_factory is not None:
            client = self._smtp_factory()
            try:
                client.send_message(message)
            finally:
                closer = getattr(client, "quit", None)
                if callable(closer):
                    closer()
            return

        context = ssl.create_default_context()

        if self.port in IMPLICIT_TLS_PORTS:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as client:
                client.login(self.user, self.password)
                client.send_message(message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as client:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
                client.login(self.user, self.password)
                client.send_message(message)

    def _message(self, subject: str, text: str, html: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(("robbin-the-hood", self.sender))
        message["To"] = ", ".join(self.recipients)
        # Multipart: the plain-text part is what a watch or a text-only client
        # shows, so it has to stand on its own rather than say "view in HTML".
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        return message

    # -- alerts ------------------------------------------------------------

    def send_alert(self, alert: AlertContent) -> None:
        """One email per listing that cleared the immediate-alert bar.

        The subject line carries the decision: price, region and score, so the
        notification preview alone often tells you whether to open it.
        """
        prefix = "PRIORITY" if alert.is_priority else f"{alert.score:g}/100"
        subject = (
            f"[{prefix}] ${alert.landed_usd:,.0f} landed · {alert.region_display} · "
            f"{alert.title[:60]}"
        )
        self._send(self._message(subject, alert.plain_text(), _alert_html(alert)))

    def send_digest(self, digest: Digest) -> None:
        subject = f"Daily digest — {len(digest.top_picks)} picks"
        if digest.price_drops:
            subject += f", {len(digest.price_drops)} price drop(s)"
        self._send(self._message(subject, _digest_text(digest), _digest_html(digest)))


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
# Written by hand rather than with a template engine so the agent keeps zero
# rendering dependencies. Email HTML is also its own world: inline styles only,
# tables for layout, and no flexbox — Outlook still uses the Word engine.
# ---------------------------------------------------------------------------

_FONT = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _alert_html(alert: AlertContent) -> str:
    banner = ""
    if alert.headline_tag:
        lines = "<br>".join(_escape(line) for line in alert.headline_tag.splitlines())
        banner = (
            f'<div style="background:#2d1b4e;color:#d9c2ff;padding:12px 16px;'
            f'border-radius:8px;margin-bottom:16px;font-weight:600">{lines}</div>'
        )

    warnings = ""
    if alert.warnings:
        items = "".join(
            f'<li style="margin:4px 0">{_escape(w)}</li>' for w in alert.warnings
        )
        warnings = (
            f'<div style="background:#3a2416;color:#ffcf9e;padding:12px 16px;'
            f'border-radius:8px;margin:16px 0"><strong>Flags</strong>'
            f'<ul style="margin:8px 0 0;padding-left:20px">{items}</ul></div>'
        )

    advantage = ""
    if alert.regional_advantage:
        advantage = (
            f'<p style="background:#12301f;color:#9ae6b4;padding:12px 16px;'
            f'border-radius:8px;margin:16px 0">{_escape(alert.regional_advantage)}</p>'
        )

    def row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:7px 12px 7px 0;color:#8d95a5;'
            f'vertical-align:top;white-space:nowrap">{_escape(label)}</td>'
            f'<td style="padding:7px 0;color:#e6e9ef">{_escape(value)}</td></tr>'
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#0f1115;{_FONT}">
<div style="max-width:640px;margin:0 auto;background:#171a21;border-radius:12px;
            padding:24px;color:#e6e9ef">

  {banner}

  <div style="font-size:13px;color:#8d95a5">
    {alert.region_flag} {_escape(alert.region_display)}
  </div>
  <h1 style="font-size:18px;margin:6px 0 16px;line-height:1.35">
    <a href="{_escape(alert.url)}" style="color:#e6e9ef;text-decoration:none">
      {_escape(alert.title)}</a>
  </h1>

  <div style="background:#1d212a;border-radius:8px;padding:16px;margin-bottom:16px">
    <div style="font-size:30px;font-weight:700;color:#4da3ff">
      ${alert.landed_usd:,.0f}
      <span style="font-size:12px;color:#616a7c;font-weight:400">LANDED USD</span>
    </div>
    <div style="font-size:14px;color:#8d95a5;margin-top:4px">
      {_escape(alert.price_line)}
    </div>
    <div style="font-size:13px;color:#8d95a5;margin-top:8px">
      {_escape(alert.floor_line)}
    </div>
  </div>

  {advantage}

  <table style="width:100%;border-collapse:collapse;font-size:14px">
    {row("Spec", alert.spec_line)}
    {row("Keyboard", alert.keyboard_line)}
    {row("Condition", alert.condition_line)}
    {row(f"Score {alert.score:g}/100", alert.score_line)}
    {row("Warranty", alert.warranty_line)}
  </table>

  {warnings}

  <div style="background:#0f1115;border-radius:8px;padding:12px;margin:16px 0">
    <div style="font-size:11px;color:#616a7c;text-transform:uppercase;
                letter-spacing:.06em;margin-bottom:6px">How the price was worked out</div>
    <pre style="margin:0;font-size:12px;color:#8d95a5;white-space:pre-wrap">{
      _escape(alert.landed_breakdown)}</pre>
  </div>

  <a href="{_escape(alert.url)}"
     style="display:inline-block;background:#4da3ff;color:#04101f;font-weight:700;
            padding:12px 22px;border-radius:8px;text-decoration:none">
    Open the listing →</a>

  <p style="font-size:11px;color:#616a7c;margin-top:24px;line-height:1.5">
    Landed USD is the full cost to get this into your contact's hands — tax,
    shipping, FX and the regional risk premium included. Sticker prices across
    currencies are not comparable.
  </p>
</div></body></html>"""


def _digest_text(digest: Digest) -> str:
    lines = ["Daily digest", ""]

    for index, pick in enumerate(digest.top_picks, start=1):
        lines.append(f"{index}. ${pick.landed_usd:,.0f} [{pick.region_display}] {pick.title[:65]}")
        lines.append(f"   {pick.price_line} · score {pick.score:g}")
        lines.append(f"   {pick.url}")

    for heading, entries in (
        ("Price drops", digest.price_drops),
        ("Price rises", digest.price_rises),
        ("Gone", digest.gone),
        ("New sources awaiting approval", digest.pending_source_approvals),
        ("Sources that failed", digest.failed_sources),
    ):
        if entries:
            lines += ["", f"{heading}:"] + [f"  - {e}" for e in entries[:10]]

    if digest.fx_note:
        lines += ["", digest.fx_note]

    lines += ["", f"{digest.listings_seen} listings seen, "
                  f"{digest.listings_rejected} rejected by hard filters."]
    return "\n".join(lines)


def _digest_html(digest: Digest) -> str:
    picks = ""
    for index, pick in enumerate(digest.top_picks, start=1):
        zone = (' <span style="color:#35c46f;font-size:11px">target zone</span>'
                if pick.in_target_zone else "")
        picks += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #262b36;vertical-align:top">
            <div style="font-size:17px;font-weight:700;color:#4da3ff">
              ${pick.landed_usd:,.0f}</div>
            <div style="font-size:11px;color:#616a7c">score {pick.score:g}{zone}</div>
          </td>
          <td style="padding:10px 0 10px 14px;border-bottom:1px solid #262b36">
            <a href="{_escape(pick.url)}" style="color:#e6e9ef;text-decoration:none;
               font-size:14px">{pick.region_flag} {_escape(pick.title[:80])}</a>
            <div style="font-size:12px;color:#8d95a5;margin-top:3px">
              {_escape(pick.price_line)}</div>
          </td>
        </tr>"""

    sections = ""
    for heading, entries, colour in (
        ("📉 Price drops", digest.price_drops, "#35c46f"),
        ("📈 Price rises", digest.price_rises, "#ef5f5f"),
        ("🚫 Gone", digest.gone, "#616a7c"),
        ("🔎 New sources awaiting your approval", digest.pending_source_approvals, "#4da3ff"),
        ("⚠ Sources that failed", digest.failed_sources, "#e8b339"),
    ):
        if not entries:
            continue
        items = "".join(
            f'<li style="margin:5px 0;color:#8d95a5">{_escape(e)}</li>'
            for e in entries[:10]
        )
        sections += (
            f'<div style="margin-top:22px"><div style="color:{colour};font-weight:650;'
            f'font-size:14px;margin-bottom:6px">{heading}</div>'
            f'<ul style="margin:0;padding-left:20px;font-size:13px">{items}</ul></div>'
        )

    fx = (f'<p style="color:#e8b339;font-size:12px;margin-top:18px">'
          f'{_escape(digest.fx_note)}</p>' if digest.fx_note else "")

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#0f1115;{_FONT}">
<div style="max-width:680px;margin:0 auto;background:#171a21;border-radius:12px;
            padding:24px;color:#e6e9ef">
  <h1 style="font-size:20px;margin:0 0 4px">Daily digest</h1>
  <p style="color:#616a7c;font-size:12px;margin:0 0 20px">
    Ranked by score. Every figure is landed USD.</p>

  <table style="width:100%;border-collapse:collapse">{picks}</table>
  {sections}
  {fx}

  <p style="font-size:11px;color:#616a7c;margin-top:24px">
    {digest.listings_seen} listings seen, {digest.listings_rejected} rejected by
    hard filters.
  </p>
</div></body></html>"""
