"""
Headless-browser fetching, for sites a plain HTTP client cannot see.

A `requests.get()` call gets back exactly what the server sent, before any
JavaScript runs. Most modern retail category pages (Newegg, gaminglaptop.deals,
bestlaptop.deals confirmed live) build their listings client-side, in the
browser, after the page loads — so a plain GET returns an empty shell: the page
skeleton, with nothing where the products should be.

A headless browser is a real browser engine (Chromium, the same one behind
Chrome and Edge) with no visible window, driven by code. It loads the page,
runs the JavaScript exactly like a person's browser would, waits for the
content to actually appear, then hands back the fully-rendered HTML.

This is deliberately the LAST resort, used only when a site's `render: js` is
set in config.yaml. It is slower (seconds per page, not milliseconds) and
needs a real browser binary installed, so `sources/html.py` tries the cheap
`requests` path for every site and only reaches for this module when a site is
explicitly marked as needing it.

Setup, one-time:

    pip install -r requirements-headless.txt
    playwright install chromium

That second command downloads the actual browser (~150-300 MB) into a local
cache — nothing project-specific, it is shared by every tool that uses
Playwright on this machine.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class HeadlessBrowserUnavailable(Exception):
    """Playwright isn't installed, or its browser binary isn't downloaded.

    Raised instead of letting an ImportError propagate, so a source using
    `render: js` fails with an actionable message rather than a stack trace
    pointing at a third-party package the reader has never heard of.
    """


class HeadlessFetchFailed(Exception):
    """The browser launched but the page did not load in time, or errored."""


def fetch_rendered_html(
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float = 30.0,
    wait_for_selector: str | None = None,
    extra_wait_seconds: float = 1.5,
) -> str:
    """Load `url` in a real (headless) browser and return the rendered HTML.

    `wait_for_selector` is the CSS selector for whatever element proves the
    real content has arrived — normally the same value as the site's `item`
    selector in config.yaml. Without it, this only waits for the page's
    initial network activity to settle, which on a slow-loading product grid
    can still be too early. Pass it whenever you know the selector; that is
    almost always safer than the fixed extra wait alone.

    Raises `HeadlessBrowserUnavailable` if Playwright or its browser binary
    is missing, and `HeadlessFetchFailed` for a page that would not load —
    both are caught by the caller (`sources/html.py`) and treated exactly
    like any other source failure: logged, reported, never fatal to the run.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise HeadlessBrowserUnavailable(
            "Playwright is not installed. Run:\n"
            "    pip install -r requirements-headless.txt\n"
            "    playwright install chromium"
        ) from exc

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                raise HeadlessBrowserUnavailable(
                    "Playwright is installed but its Chromium binary is not. "
                    "Run:\n    playwright install chromium"
                ) from exc

            try:
                page = browser.new_page(user_agent=user_agent)
                page.set_default_timeout(timeout_seconds * 1000)

                try:
                    page.goto(url, wait_until="domcontentloaded")

                    if wait_for_selector:
                        # Waits for the element to exist AND be visible — a
                        # placeholder that's in the DOM but hidden behind a
                        # loading spinner does not count as "arrived".
                        page.wait_for_selector(wait_for_selector, state="visible")
                    else:
                        # No selector to key off; give client-side rendering
                        # a fixed grace period after the network goes quiet.
                        page.wait_for_load_state("networkidle")

                    if extra_wait_seconds:
                        page.wait_for_timeout(extra_wait_seconds * 1000)

                    return page.content()

                except PlaywrightError as exc:
                    raise HeadlessFetchFailed(
                        f"{url} did not finish rendering within "
                        f"{timeout_seconds:.0f}s: {exc}"
                    ) from exc
            finally:
                browser.close()

    except HeadlessBrowserUnavailable:
        raise
    except HeadlessFetchFailed:
        raise
    except Exception as exc:  # noqa: BLE001 — any other Playwright/OS failure
        raise HeadlessFetchFailed(f"headless fetch of {url} failed: {exc}") from exc
