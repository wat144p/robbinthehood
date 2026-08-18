"""
Headless-browser fetching.

Playwright itself is not a test dependency (it pulls in a real browser
binary), so these tests cover two things without needing it installed:

  1. The "not available" path, genuinely exercised — this environment does
     not have Playwright installed, so `HeadlessBrowserUnavailable` is the
     real code path, not a simulation of it.
  2. The success and failure paths, driven through a fake `playwright.sync_api`
     module injected into `sys.modules`, so the browser-driving logic (goto,
     wait_for_selector, content, cleanup) is genuinely exercised without a
     real browser or network.
"""

from __future__ import annotations

import sys
import types

import pytest

from dealhunter.headless import (
    HeadlessBrowserUnavailable,
    HeadlessFetchFailed,
    fetch_rendered_html,
)


class TestUnavailable:
    def test_missing_playwright_raises_a_clear_actionable_error(self):
        """Playwright genuinely is not installed in this environment, so this
        exercises the real ImportError path, not a simulation of it."""
        assert "playwright" not in sys.modules or not _really_installed()

        with pytest.raises(HeadlessBrowserUnavailable, match="pip install"):
            fetch_rendered_html("https://example.test", user_agent="robbin/0.1")


def _really_installed() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# A fake playwright.sync_api, for exercising the browser-driving logic
# without a real browser installed.
# ---------------------------------------------------------------------------


class FakePage:
    def __init__(self, html: str, goto_error=None, selector_error=None):
        self._html = html
        self._goto_error = goto_error
        self._selector_error = selector_error
        self.calls: list[str] = []

    def set_default_timeout(self, ms):
        self.calls.append(f"set_default_timeout({ms})")

    def goto(self, url, wait_until=None):
        self.calls.append(f"goto({url}, wait_until={wait_until})")
        if self._goto_error:
            raise self._goto_error

    def wait_for_selector(self, selector, state=None):
        self.calls.append(f"wait_for_selector({selector}, state={state})")
        if self._selector_error:
            raise self._selector_error

    def wait_for_load_state(self, state):
        self.calls.append(f"wait_for_load_state({state})")

    def wait_for_timeout(self, ms):
        self.calls.append(f"wait_for_timeout({ms})")

    def content(self):
        return self._html


class FakeBrowser:
    def __init__(self, page: FakePage):
        self._page = page
        self.closed = False

    def new_page(self, user_agent=None):
        return self._page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser | None = None, launch_error=None):
        self._browser = browser
        self._launch_error = launch_error

    def launch(self, headless=True):
        if self._launch_error:
            raise self._launch_error
        return self._browser


class FakePlaywrightContext:
    def __init__(self, chromium: FakeChromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def install_fake_playwright(monkeypatch, chromium: FakeChromium, error_cls=Exception):
    """Inject a fake `playwright.sync_api` module so `fetch_rendered_html`'s
    `from playwright.sync_api import ...` succeeds and drives our fake objects
    instead of a real browser."""
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: FakePlaywrightContext(chromium)
    fake_module.Error = error_cls

    fake_playwright_pkg = types.ModuleType("playwright")
    fake_playwright_pkg.sync_api = fake_module

    monkeypatch.setitem(sys.modules, "playwright", fake_playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)


class FakePlaywrightError(Exception):
    """Stands in for playwright.sync_api.Error in these tests."""


class TestFetchRenderedHtml:
    def test_a_successful_fetch_returns_the_rendered_content(self, monkeypatch):
        page = FakePage(html="<html><body>rendered content</body></html>")
        install_fake_playwright(
            monkeypatch, FakeChromium(FakeBrowser(page)), FakePlaywrightError
        )

        html = fetch_rendered_html(
            "https://example.test/laptops",
            user_agent="robbin/0.1",
            wait_for_selector="li.product",
        )

        assert html == "<html><body>rendered content</body></html>"
        assert any("goto(https://example.test/laptops" in c for c in page.calls)
        assert any("wait_for_selector(li.product, state=visible)" in c
                  for c in page.calls)

    def test_without_a_selector_it_waits_for_network_idle_instead(self, monkeypatch):
        page = FakePage(html="<html></html>")
        install_fake_playwright(
            monkeypatch, FakeChromium(FakeBrowser(page)), FakePlaywrightError
        )

        fetch_rendered_html("https://example.test", user_agent="robbin/0.1")

        assert any("wait_for_load_state(networkidle)" in c for c in page.calls)
        assert not any("wait_for_selector" in c for c in page.calls)

    def test_the_browser_is_always_closed(self, monkeypatch):
        page = FakePage(html="<html></html>")
        browser = FakeBrowser(page)
        install_fake_playwright(monkeypatch, FakeChromium(browser), FakePlaywrightError)

        fetch_rendered_html("https://example.test", user_agent="robbin/0.1")
        assert browser.closed is True

    def test_the_browser_is_closed_even_when_the_page_fails(self, monkeypatch):
        page = FakePage(html="", goto_error=FakePlaywrightError("timed out"))
        browser = FakeBrowser(page)
        install_fake_playwright(monkeypatch, FakeChromium(browser), FakePlaywrightError)

        with pytest.raises(HeadlessFetchFailed):
            fetch_rendered_html("https://example.test", user_agent="robbin/0.1")
        assert browser.closed is True

    def test_a_page_that_never_finishes_loading_raises_fetch_failed(self, monkeypatch):
        page = FakePage(html="", goto_error=FakePlaywrightError("Timeout 30000ms exceeded"))
        install_fake_playwright(
            monkeypatch, FakeChromium(FakeBrowser(page)), FakePlaywrightError
        )

        with pytest.raises(HeadlessFetchFailed, match="did not finish rendering"):
            fetch_rendered_html(
                "https://example.test", user_agent="robbin/0.1", timeout_seconds=30
            )

    def test_a_selector_that_never_appears_raises_fetch_failed(self, monkeypatch):
        page = FakePage(html="<html></html>",
                        selector_error=FakePlaywrightError("selector not found"))
        install_fake_playwright(
            monkeypatch, FakeChromium(FakeBrowser(page)), FakePlaywrightError
        )

        with pytest.raises(HeadlessFetchFailed):
            fetch_rendered_html(
                "https://example.test", user_agent="robbin/0.1",
                wait_for_selector=".never-appears",
            )

    def test_a_missing_chromium_binary_is_reported_as_unavailable_not_failed(
        self, monkeypatch
    ):
        """Distinct from a page failing to load: the browser could not even
        start, which is a setup problem — 'run playwright install' — not a
        site-specific failure."""
        chromium = FakeChromium(launch_error=FakePlaywrightError(
            "Executable doesn't exist at .../chromium-1234/chrome"
        ))
        install_fake_playwright(monkeypatch, chromium, FakePlaywrightError)

        with pytest.raises(HeadlessBrowserUnavailable, match="playwright install"):
            fetch_rendered_html("https://example.test", user_agent="robbin/0.1")

    def test_extra_wait_is_applied_after_the_selector_appears(self, monkeypatch):
        page = FakePage(html="<html></html>")
        install_fake_playwright(
            monkeypatch, FakeChromium(FakeBrowser(page)), FakePlaywrightError
        )

        fetch_rendered_html(
            "https://example.test", user_agent="robbin/0.1",
            wait_for_selector="li.product", extra_wait_seconds=2.0,
        )

        assert any("wait_for_timeout(2000" in c for c in page.calls)
        # Order matters: the extra wait is insurance ON TOP of the selector
        # being visible, not a replacement for waiting on it.
        selector_idx = next(i for i, c in enumerate(page.calls) if "wait_for_selector" in c)
        timeout_idx = next(i for i, c in enumerate(page.calls) if "wait_for_timeout" in c)
        assert selector_idx < timeout_idx
