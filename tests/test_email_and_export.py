"""
The email channel and the static site export.

The email tests drive the real message construction through a fake SMTP client,
so the multipart assembly, headers and HTML are genuinely exercised without
opening a socket. The export tests run the real Flask app through its test
client, so they'd catch a template break.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from dealhunter.evaluate import evaluate
from dealhunter.models import Condition, Region
from dealhunter.notify import EmailNotifier, build_alert, build_notifiers, dispatch
from dealhunter.notify.base import Digest
from tests.fixtures import make_listing


class FakeSMTP:
    """Records the messages that would have been sent."""

    sent: list[EmailMessage] = []

    def __init__(self):
        FakeSMTP.sent = []
        self.quit_called = False

    def send_message(self, message: EmailMessage) -> None:
        FakeSMTP.sent.append(message)

    def quit(self) -> None:
        self.quit_called = True


def notifier(**kwargs) -> EmailNotifier:
    defaults = dict(
        host="smtp.example.test", port=587, user="me@example.test",
        password="app password here", recipients="me@example.test",
        smtp_factory=FakeSMTP,
    )
    defaults.update(kwargs)
    return EmailNotifier(**defaults)


@pytest.fixture
def alert(config, rates):
    evaluated = evaluate(
        make_listing("helios_neo_16s_openbox", price=1184.0, jurisdiction="OR",
                     condition=Condition.OPEN_BOX_EXCELLENT, seller_name="Best Buy"),
        config, rates,
    )
    assert not evaluated.rejected
    return build_alert(evaluated, config)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestEmailConfiguration:
    def test_unconfigured_without_credentials(self, monkeypatch):
        for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
            monkeypatch.delenv(name, raising=False)
        assert EmailNotifier().is_configured() is False

    def test_configured_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
        monkeypatch.setenv("SMTP_USER", "me@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
        monkeypatch.setenv("EMAIL_TO", "me@gmail.com")

        channel = EmailNotifier()
        assert channel.is_configured() is True
        # Gmail shows app passwords in groups of four; people paste the spaces.
        assert channel.password == "abcdefghijklmnop"

    def test_multiple_recipients(self):
        channel = notifier(recipients="a@x.test, b@x.test ,c@x.test")
        assert channel.recipients == ["a@x.test", "b@x.test", "c@x.test"]

    def test_sender_defaults_to_the_smtp_user(self):
        assert notifier().sender == "me@example.test"

    def test_partial_credentials_count_as_unconfigured(self):
        """A half-set-up channel must be skipped, not attempted and failed."""
        assert notifier(password="").is_configured() is False
        assert notifier(recipients="").is_configured() is False


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TestAlertEmail:
    def test_subject_carries_the_decision(self, alert):
        notifier().send_alert(alert)
        subject = FakeSMTP.sent[0]["Subject"]

        # The notification preview alone should often be enough.
        assert "PRIORITY" in subject
        assert "$1,184" in subject
        assert "United States" in subject

    def test_non_priority_subject_shows_the_score(self, config, rates):
        evaluated = evaluate(
            make_listing("ambiguous_5070", price=1150.0, jurisdiction="OR",
                         condition=Condition.OPEN_BOX_GOOD, seller_name="someone",
                         feedback=2000, percent=98.5),
            config, rates,
        )
        notifier().send_alert(build_alert(evaluated, config))
        assert "/100" in FakeSMTP.sent[0]["Subject"]

    def test_message_is_multipart_with_a_usable_plain_text_part(self, alert):
        """The text part is what a watch or a text-only client shows, so it has
        to stand on its own rather than say 'view in HTML'."""
        notifier().send_alert(alert)
        message = FakeSMTP.sent[0]

        assert message.is_multipart()
        text = message.get_body(preferencelist=("plain",)).get_content()
        assert "$1,184.00 landed" in text
        assert "keyboard:" in text
        assert "view in html" not in text.lower()

    def test_html_part_shows_both_prices(self, config, rates):
        """The ranking rule survives into email: never a converted number alone."""
        evaluated = evaluate(
            make_listing("legion_pro_5_oled", region=Region.CA, price=1499.0,
                         jurisdiction="AB", condition=Condition.OPEN_BOX_EXCELLENT,
                         seller_name="Best Buy Canada"),
            config, rates,
        )
        notifier().send_alert(build_alert(evaluated, config))

        html = FakeSMTP.sent[0].get_body(preferencelist=("html",)).get_content()
        assert "C$1,499.00" in html
        assert "$1,148" in html

    def test_headers_are_addressed_correctly(self, alert):
        notifier(recipients="a@x.test,b@x.test").send_alert(alert)
        message = FakeSMTP.sent[0]

        assert message["To"] == "a@x.test, b@x.test"
        assert "robbin-the-hood" in message["From"]

    def test_html_is_escaped(self, config, rates):
        """A seller-controlled title must not be able to inject markup."""
        evaluated = evaluate(
            make_listing(
                title='Acer Predator Helios Neo 16S AI <script>x</script> '
                      '2560x1600 OLED RTX 5070 Ti 12GB 32GB 1TB',
                price=1184.0, jurisdiction="OR", seller_name="Best Buy"),
            config, rates,
        )
        notifier().send_alert(build_alert(evaluated, config))

        html = FakeSMTP.sent[0].get_body(preferencelist=("html",)).get_content()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_warnings_reach_the_email(self, config, rates):
        evaluated = evaluate(
            make_listing("ambiguous_5070", price=1150.0, jurisdiction="OR",
                         condition=Condition.USED, seller_name="newbie",
                         feedback=6, percent=100.0),
            config, rates,
        )
        notifier().send_alert(build_alert(evaluated, config))
        html = FakeSMTP.sent[0].get_body(preferencelist=("html",)).get_content()
        assert "HIGH RISK" in html


class TestDigestEmail:
    def test_digest_lists_the_picks(self, alert):
        notifier().send_digest(Digest(top_picks=[alert], listings_seen=42,
                                      listings_rejected=17))
        message = FakeSMTP.sent[0]

        assert "Daily digest" in message["Subject"]
        html = message.get_body(preferencelist=("html",)).get_content()
        assert "$1,184" in html
        assert "42 listings seen" in html

    def test_subject_mentions_price_drops(self, alert):
        notifier().send_digest(Digest(top_picks=[alert],
                                      price_drops=["something got cheaper"]))
        assert "price drop" in FakeSMTP.sent[0]["Subject"]

    def test_failed_sources_are_reported(self):
        notifier().send_digest(Digest(failed_sources=["ebay: BLOCKED — HTTP 403"]))
        html = FakeSMTP.sent[0].get_body(preferencelist=("html",)).get_content()
        assert "403" in html


class TestEmailInDispatch:
    def test_email_joins_the_other_channels(self, config, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.x.test")
        monkeypatch.setenv("SMTP_USER", "me@x.test")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        monkeypatch.setenv("EMAIL_TO", "me@x.test")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")

        names = [n.name for n in build_notifiers(config, dry_run=False)]
        assert "email" in names
        assert "discord" in names

    def test_dry_run_still_sends_nothing(self, config, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.x.test")
        monkeypatch.setenv("SMTP_USER", "me@x.test")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        monkeypatch.setenv("EMAIL_TO", "me@x.test")

        assert [n.name for n in build_notifiers(config, dry_run=True)] == ["console"]

    def test_a_broken_smtp_server_does_not_stop_other_channels(self, alert):
        class ExplodingSMTP:
            def send_message(self, message):
                raise ConnectionRefusedError("no SMTP here")

        results = dispatch([notifier(smtp_factory=ExplodingSMTP)], [alert], None)
        assert results[0].ok is False
        assert "ConnectionRefusedError" in results[0].errors[0]


# ---------------------------------------------------------------------------
# Static export
# ---------------------------------------------------------------------------


@pytest.fixture
def exported(tmp_path, config, rates):
    """Build a small database and export it to a temporary directory."""
    import export_site
    from dealhunter.evaluate import evaluate_all
    from dealhunter.store import Store

    db_path = tmp_path / "export.db"
    with Store(db_path) as store:
        store.seed_floors(config)
        run_id = store.start_run()
        evaluated = evaluate_all(
            [
                make_listing("helios_neo_16s_openbox", price=1184.0,
                             jurisdiction="OR", seller_name="Best Buy",
                             listing_id="us-1"),
                make_listing("legion_pro_5_oled", region=Region.CA, price=1499.0,
                             jurisdiction="AB", seller_name="Best Buy Canada",
                             listing_id="ca-1"),
            ],
            config, rates, floors=store.floors(),
        )
        store.record_listings(evaluated, config)
        store.finish_run(run_id, listings_seen=2)

    out = tmp_path / "site"
    export_site.export(str(db_path), str(out))
    return out


class TestStaticExport:
    def test_every_page_is_written(self, exported):
        for name in ("index.html", "cheapest.html", "newest.html", "moved.html",
                     "all.html", "changes.html", "models.html", "health.html"):
            assert (exported / name).exists(), f"{name} missing"

    def test_nojekyll_is_present(self, exported):
        """Without it, Pages runs the output through Jekyll and drops files
        beginning with an underscore."""
        assert (exported / ".nojekyll").exists()

    def test_stylesheet_is_flattened_alongside_the_pages(self, exported):
        assert (exported / "style.css").exists()
        html = (exported / "index.html").read_text(encoding="utf-8")
        assert 'href="style.css"' in html

    def test_no_absolute_app_urls_survive(self, exported):
        """A link to "/changes" 404s on a Pages project subpath."""
        import re

        for page in exported.glob("*.html"):
            html = page.read_text(encoding="utf-8")
            leftovers = re.findall(r'href="(/[^"]*)"', html)
            assert not leftovers, f"{page.name} still has absolute links: {leftovers}"

    def test_deal_pages_are_generated_and_linked(self, exported):
        deal_pages = list(exported.glob("deal-*.html"))
        assert deal_pages

        index = (exported / "index.html").read_text(encoding="utf-8")
        assert deal_pages[0].name in index

    def test_filter_form_is_replaced_by_links(self, exported):
        """A form has nowhere to submit on a static host."""
        html = (exported / "index.html").read_text(encoding="utf-8")
        assert 'class="staticnav"' in html
        assert '<form class="filters"' not in html

    def test_pages_are_marked_noindex(self, exported):
        html = (exported / "index.html").read_text(encoding="utf-8")
        assert 'name="robots"' in html and "noindex" in html

    def test_timestamps_are_machine_readable_for_the_live_time_script(self, exported):
        """Otherwise a snapshot read the next morning still claims '2h ago'."""
        html = (exported / "index.html").read_text(encoding="utf-8")
        assert "<time datetime=" in html
        assert "function relative(" in html

    def test_the_build_stamp_is_shown(self, exported):
        html = (exported / "index.html").read_text(encoding="utf-8")
        assert "Snapshot built" in html

    def test_both_prices_survive_the_export(self, exported):
        """The ranking rule holds in the static snapshot too."""
        html = (exported / "index.html").read_text(encoding="utf-8")
        assert "C$1,499.00" in html
        assert "$1,148" in html

    def test_auto_refresh_is_absent(self, exported):
        """There is nothing to refresh into on a static host."""
        html = (exported / "index.html").read_text(encoding="utf-8")
        assert 'id="autorefresh"' not in html
