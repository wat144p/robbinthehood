"""
Local dashboard.

    python -m pip install flask
    python dashboard.py                       # http://127.0.0.1:5000
    python dashboard.py --db data/demo.db     # against the demo data

Read-only by design: it reads the same SQLite file the agent writes, and never
triggers a run or edits config. That means you can leave it open while the
8-hourly schedule works, and nothing you click can corrupt a sweep in progress.

The board is organised around **what you can buy right now**, not what was ever
cheapest. Freshness outranks score in the ordering, because a live listing you
can act on beats a higher-scoring one that probably sold three days ago.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from flask import Flask, redirect, render_template, request, url_for

from dealhunter import load_config
from dealhunter.store import Store
from dealhunter.views import health, live_board, model_watch, recent_changes

app = Flask(__name__)

# Set by main(); the request handlers read them.
app.config["DEALS_DB"] = "data/deals.db"
app.config["DEALS_CONFIG"] = None


def _open():
    """A fresh connection per request.

    SQLite connections are not safe to share across threads, and Flask's dev
    server is threaded. Opening per request is cheap and avoids a whole class
    of intermittent 'objects created in a thread can only be used in that
    thread' failures.
    """
    return Store(app.config["DEALS_DB"])


def _static_export() -> bool:
    """True when rendering for the static snapshot.

    Passed explicitly to templates rather than read from Flask's `config`,
    because several views pass the dealhunter Config object under that same
    name — and only one of the two has a .get() method.
    """
    return bool(app.config.get("STATIC_EXPORT"))


def _config():
    if app.config["DEALS_CONFIG"] is None:
        app.config["DEALS_CONFIG"] = load_config()
    return app.config["DEALS_CONFIG"]


@app.route("/")
def board():
    """The live board — what is buyable, best first."""
    config = _config()
    with _open() as store:
        deals = live_board(
            store, config,
            region=request.args.get("region") or None,
            min_score=float(request.args.get("min_score") or 0),
            max_landed=(
                float(request.args["max_landed"])
                if request.args.get("max_landed") else None
            ),
            include_stale=request.args.get("stale") == "1",
            sort=request.args.get("sort", "score"),
        )
        latest_run = store.latest_run_at()
        counts = {
            "live": sum(1 for d in deals if d.freshness.value == "live"),
            "total": len(deals),
        }

    return render_template(
        "board.html", deals=deals, config=config, latest_run=latest_run,
        counts=counts, now=datetime.now(timezone.utc),
        selected_region=request.args.get("region", ""),
        sort=request.args.get("sort", "score"),
        include_stale=request.args.get("stale") == "1",
        min_score=request.args.get("min_score", ""),
        active="board",
        static_export=_static_export(),
    )


@app.route("/changes")
def changes():
    """What moved since the last sweeps — the whole point of running 3x a day."""
    hours = int(request.args.get("hours", 24))
    config = _config()
    with _open() as store:
        data = recent_changes(store, config, hours=hours)
        latest_run = store.latest_run_at()

    return render_template(
        "changes.html", changes=data, hours=hours, latest_run=latest_run,
        now=datetime.now(timezone.utc), active="changes",
        static_export=_static_export(),
    )


@app.route("/models")
def models():
    """Target models: what's live now, floor shown only as a reference."""
    config = _config()
    with _open() as store:
        watches = model_watch(store, config)
        latest_run = store.latest_run_at()

    return render_template(
        "models.html", watches=watches, config=config, latest_run=latest_run,
        now=datetime.now(timezone.utc), active="models",
        static_export=_static_export(),
    )


@app.route("/health")
def health_page():
    with _open() as store:
        data = health(store)
    return render_template(
        "health.html", health=data, now=datetime.now(timezone.utc), active="health",
        static_export=_static_export(),
    )


@app.route("/deal/<path:fingerprint>")
def deal(fingerprint: str):
    """One listing, with its full reasoning and price history."""
    config = _config()
    with _open() as store:
        deals = live_board(store, config, include_stale=True, limit=5000)
        match = next((d for d in deals if d.fingerprint == fingerprint), None)
        history = [
            dict(r) for r in store.connection.execute(
                """SELECT seen_at, landed_usd, sticker_local, fx_rate
                   FROM price_history WHERE fingerprint = ? ORDER BY seen_at""",
                (fingerprint,),
            ).fetchall()
        ]

    if match is None:
        return redirect(url_for("board"))

    return render_template(
        "deal.html", deal=match, history=history,
        now=datetime.now(timezone.utc), active="board",
        static_export=_static_export(),
    )


@app.template_filter("money")
def money(value) -> str:
    return "—" if value is None else f"${value:,.0f}"


@app.template_filter("signed")
def signed(value) -> str:
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '−'}${abs(value):,.0f}"


@app.template_filter("ago")
def ago(moment) -> str:
    if moment is None:
        return "never"
    delta = datetime.now(timezone.utc) - moment
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 2880:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def main() -> None:
    parser = argparse.ArgumentParser(description="robbin-the-hood dashboard")
    parser.add_argument("--db", default="data/deals.db", help="SQLite database")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Loopback by default — do not expose this to a network.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    app.config["DEALS_DB"] = args.db
    print(f"Dashboard: http://{args.host}:{args.port}   (database: {args.db})")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
