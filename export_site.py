"""
Render the dashboard to static HTML for GitHub Pages.

    python export_site.py --db data/deals.db --out site

Gives you a URL you can open on your phone, from anywhere, without your laptop
being on and without a server to pay for. The GitHub Actions workflow runs this
after every sweep, so the published page is never more than 8 hours old.

How it works: it drives the real Flask app through its test client and writes
each response to a file. That means the export can never drift from the live
dashboard — there is exactly one set of templates and one set of queries.

Two things a static site cannot do, and how they're handled:

* **Filter forms don't submit anywhere.** So the variants you'd actually reach
  by filtering (cheapest first, newest, including stale) are pre-rendered as
  separate pages, and the form is replaced by links to them.
* **Relative times freeze at build time.** "2h ago" baked into a page you open
  the next morning is a lie. Timestamps are emitted as `<time datetime=…>` and
  a few lines of JavaScript recompute them on load, so they stay true however
  long after the build you read the page.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import dashboard
from dealhunter import load_config
from dealhunter.store import Store
from dealhunter.views import live_board

# The pages to render: (output filename, dashboard URL).
PAGES: list[tuple[str, str]] = [
    ("index.html",        "/?sort=score"),
    ("cheapest.html",     "/?sort=price"),
    ("newest.html",       "/?sort=newest"),
    ("moved.html",        "/?sort=moved"),
    ("all.html",          "/?sort=score&stale=1"),
    ("changes.html",      "/changes?hours=24"),
    ("changes-week.html", "/changes?hours=168"),
    ("models.html",       "/models"),
    ("health.html",       "/health"),
]

# Live-app URL -> static filename. Applied to every href in the output.
LINK_MAP = {
    "/changes?hours=168": "changes-week.html",
    "/changes": "changes.html",
    "/models": "models.html",
    "/health": "health.html",
    "/static/style.css": "style.css",
}


def deal_filename(fingerprint: str) -> str:
    """A filesystem-safe page name for one listing.

    Fingerprints contain ':' and '|', which are illegal in Windows filenames
    and awkward in URLs, so they're hashed. Flat filenames keep every link at
    the same directory level, which avoids a whole class of '../' bugs.
    """
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    return f"deal-{digest}.html"


def rewrite_links(html: str) -> str:
    """Turn live-app URLs into static filenames."""
    # Deal pages first — they're the most specific pattern.
    html = re.sub(
        r'href="/deal/([^"]+)"',
        lambda m: f'href="{deal_filename(_unquote(m.group(1)))}"',
        html,
    )

    for live, static in LINK_MAP.items():
        html = html.replace(f'href="{live}"', f'href="{static}"')

    # The board's own link, and any sort variants pointing back at "/".
    html = re.sub(r'href="/\?sort=score"', 'href="index.html"', html)
    html = re.sub(r'href="/"', 'href="index.html"', html)

    return html


def _unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


def inject_static_bits(html: str, generated_at: datetime) -> str:
    """Add the noindex hint, the build stamp, and the live-time script."""
    banner = (
        f'<div class="buildstamp">Snapshot built '
        f'<time datetime="{generated_at.isoformat()}">'
        f'{generated_at:%Y-%m-%d %H:%M} UTC</time>'
        f' · refreshed after every 8-hourly sweep</div>'
    )

    html = html.replace("<main>", f"<main>{banner}", 1)

    # Keep the snapshot out of search results. It is a page about what you are
    # personally shopping for; there is no reason for it to be indexed.
    html = html.replace(
        "<head>",
        '<head>\n  <meta name="robots" content="noindex, nofollow">',
        1,
    )

    # Recompute relative times in the browser so they stay honest.
    html = html.replace("</body>", _LIVE_TIME_SCRIPT + "\n</body>", 1)
    return html


_LIVE_TIME_SCRIPT = """
<script>
// Relative times are rendered at build time, so they would otherwise say
// "2h ago" on a page you open the next morning. Recompute from the machine
// -readable datetime attribute instead.
(function () {
  function relative(iso) {
    var mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 1)    return 'just now';
    if (mins < 60)   return mins + 'm ago';
    if (mins < 2880) return Math.floor(mins / 60) + 'h ago';
    return Math.floor(mins / 1440) + 'd ago';
  }
  document.querySelectorAll('time[datetime]').forEach(function (el) {
    if (el.dataset.absolute === '1') return;
    el.textContent = relative(el.getAttribute('datetime'));
  });
})();
</script>"""


def export(db_path: str, out_dir: str, config_path: str | None = None) -> int:
    """Render every page. Returns the number of files written."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    dashboard.app.config["DEALS_DB"] = db_path
    dashboard.app.config["DEALS_CONFIG"] = load_config(config_path)
    dashboard.app.config["STATIC_EXPORT"] = True

    generated_at = datetime.now(timezone.utc)
    written = 0

    with dashboard.app.test_client() as client:
        for filename, url in PAGES:
            response = client.get(url)
            if response.status_code != 200:
                print(f"  !! {url} returned {response.status_code}; skipping")
                continue

            html = inject_static_bits(
                rewrite_links(response.get_data(as_text=True)), generated_at
            )
            (out / filename).write_text(html, encoding="utf-8")
            written += 1
            print(f"  {filename:<20} <- {url}")

        # A page per listing, so the "why this score" links resolve.
        config = dashboard.app.config["DEALS_CONFIG"]
        with Store(db_path) as store:
            deals = live_board(store, config, include_stale=True, limit=5000)

        for deal in deals:
            response = client.get(f"/deal/{deal.fingerprint}")
            if response.status_code != 200:
                continue
            html = inject_static_bits(
                rewrite_links(response.get_data(as_text=True)), generated_at
            )
            (out / deal_filename(deal.fingerprint)).write_text(html, encoding="utf-8")
            written += 1

        print(f"  {len(deals)} listing detail pages")

    # Stylesheet, flattened next to the pages so one relative href works.
    source_css = Path(__file__).parent / "static" / "style.css"
    if source_css.exists():
        extra = "\n\n" + _BUILDSTAMP_CSS
        (out / "style.css").write_text(
            source_css.read_text(encoding="utf-8") + extra, encoding="utf-8"
        )
        written += 1

    # Tells GitHub Pages not to run the output through Jekyll, which would
    # otherwise ignore any file or directory beginning with an underscore.
    (out / ".nojekyll").write_text("", encoding="utf-8")
    written += 1

    return written


_BUILDSTAMP_CSS = """
/* Added by export_site.py — only present in the static snapshot. */
.buildstamp {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 9px 14px;
  margin-bottom: 18px;
  font-size: 12px;
  color: var(--dim);
}
.staticnav { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
.staticnav a {
  padding: 7px 13px; border-radius: var(--radius); font-size: 13px;
  background: var(--panel); border: 1px solid var(--line); color: var(--muted);
}
.staticnav a:hover { color: var(--text); text-decoration: none; }
.staticnav a.on { background: var(--panel-2); color: var(--text);
                  border-color: var(--accent); }
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the dashboard as a static site")
    parser.add_argument("--db", default="data/deals.db")
    parser.add_argument("--out", default="site")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"No database at {args.db}. Run a sweep first, or use --db data/demo.db.")
        raise SystemExit(1)

    print(f"Exporting {args.db} -> {args.out}/")
    count = export(args.db, args.out, args.config)
    print(f"\nWrote {count} files to {args.out}/")
    print(f"Preview locally:  python -m http.server -d {args.out} 8080")


if __name__ == "__main__":
    main()
