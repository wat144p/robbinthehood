"""
Data shaping for the dashboard.

Deliberately separate from the Flask app so the queries are testable without a
web server, and so swapping the UI later doesn't mean rewriting the logic.

**The organising principle here is "what can I buy right now".** Not "what was
cheapest ever" — a record low from six weeks ago on a unit that sold is not
purchasable, and ranking by it is misleading. The all-time floor appears as a
*reference column* so you can tell a genuine bargain from a merely-available
one, but it never drives the ordering.

That makes freshness a first-class concept. With sweeps every 8 hours, a
listing that missed the last sweep is already questionable, and one last
confirmed three days ago is probably sold. `Freshness` grades that, and the
board leads with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from .config import Config
from .store import Store


class Freshness(str, Enum):
    """How recently we actually confirmed a listing exists.

    LIVE is the only tier you should trust without re-checking the link.
    """

    LIVE = "live"        # seen in the most recent completed sweep
    RECENT = "recent"    # within 24h, but missed the last sweep
    AGING = "aging"      # 24-72h
    STALE = "stale"      # older than 72h and still not marked gone
    GONE = "gone"        # not seen in 7 days

    @property
    def label(self) -> str:
        return {
            Freshness.LIVE: "live",
            Freshness.RECENT: "missed last sweep",
            Freshness.AGING: "aging",
            Freshness.STALE: "probably sold",
            Freshness.GONE: "gone",
        }[self]


@dataclass
class Deal:
    """One currently-listed machine, as the board shows it."""

    fingerprint: str
    title: str
    url: str
    region: str
    region_flag: str
    region_name: str
    currency: str
    sticker_local: float
    landed_usd: float
    score: float
    condition: str
    seller_name: str
    source: str
    flags: list[str]
    spec_line: str
    score_breakdown: str
    landed_explain: str
    model_key: str | None
    model_display: str | None
    first_seen: datetime
    last_seen: datetime
    freshness: Freshness

    # Reference points. Neither drives the ordering.
    floor_usd: float | None = None          # all-time low for this model
    best_available_usd: float | None = None  # cheapest live listing of this model

    # Movement since we started tracking this specific listing.
    previous_usd: float | None = None
    notified_usd: float | None = None

    @property
    def age(self) -> timedelta:
        return datetime.now(timezone.utc) - self.last_seen

    @property
    def age_display(self) -> str:
        minutes = int(self.age.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 48:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"

    @property
    def sticker_display(self) -> str:
        symbols = {"USD": "$", "CAD": "C$", "GBP": "£",
                   "EUR": "€", "SEK": "kr", "AUD": "A$"}
        return f"{symbols.get(self.currency, self.currency + ' ')}{self.sticker_local:,.2f}"

    @property
    def landed_display(self) -> str:
        return f"${self.landed_usd:,.0f}"

    @property
    def vs_floor(self) -> float | None:
        """Reference only: how far above the all-time low this sits."""
        return None if self.floor_usd is None else self.landed_usd - self.floor_usd

    @property
    def is_best_available(self) -> bool:
        """True when nothing cheaper of this model is currently listed.

        This is the one that should influence a buying decision — unlike the
        all-time floor, it describes something you can actually act on.
        """
        if self.best_available_usd is None:
            return False
        return self.landed_usd <= self.best_available_usd + 0.01

    @property
    def own_change_usd(self) -> float | None:
        """How this listing's own price has moved since we first saw it."""
        return None if self.previous_usd is None else self.landed_usd - self.previous_usd

    @property
    def is_high_risk(self) -> bool:
        return "HIGH_RISK" in self.flags

    @property
    def is_priority(self) -> bool:
        return "PRIORITY_TARGET" in self.flags or "RTX_5070_12GB" in self.flags

    @property
    def unverified_flags(self) -> list[str]:
        return [f for f in self.flags if f.startswith("UNVERIFIED")]


@dataclass
class Change:
    """Something that moved between sweeps."""

    kind: str            # new | drop | rise | gone
    title: str
    url: str
    region_flag: str
    landed_usd: float
    previous_usd: float | None = None
    when: datetime | None = None

    @property
    def delta(self) -> float | None:
        return None if self.previous_usd is None else self.landed_usd - self.previous_usd

    @property
    def percent(self) -> float | None:
        if not self.previous_usd:
            return None
        return (self.landed_usd - self.previous_usd) / self.previous_usd * 100


@dataclass
class ModelWatch:
    """One target model: what's available now, against the floor as reference."""

    model_key: str
    display: str
    floor_usd: float
    best_available_usd: float | None
    best_url: str | None
    best_region: str | None
    live_count: int
    priority_trigger_usd: float | None = None

    @property
    def above_floor(self) -> float | None:
        if self.best_available_usd is None:
            return None
        return self.best_available_usd - self.floor_usd

    @property
    def triggers_alert(self) -> bool:
        if self.best_available_usd is None or self.priority_trigger_usd is None:
            return False
        return self.best_available_usd <= self.priority_trigger_usd


@dataclass
class Health:
    sources: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)
    latest_run_at: datetime | None = None
    totals: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def classify_freshness(
    last_seen: datetime, status: str, latest_run_at: datetime | None
) -> Freshness:
    """Grade how much to trust that a listing still exists."""
    if status == "gone":
        return Freshness.GONE

    # Confirmed in the most recent completed sweep. The only tier worth acting
    # on without re-checking the link yourself.
    if latest_run_at is not None and last_seen >= latest_run_at:
        return Freshness.LIVE

    age = datetime.now(timezone.utc) - last_seen
    if age < timedelta(hours=24):
        return Freshness.RECENT
    if age < timedelta(hours=72):
        return Freshness.AGING
    return Freshness.STALE


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


def live_board(
    store: Store,
    config: Config,
    *,
    region: str | None = None,
    min_score: float = 0.0,
    max_landed: float | None = None,
    include_stale: bool = False,
    sort: str = "score",
    limit: int = 200,
) -> list[Deal]:
    """Everything currently listed, ranked by how good a buy it is *now*.

    `include_stale=False` hides anything we haven't confirmed in 72 hours,
    because a listing that old has usually sold and clicking through to a dead
    page is the fastest way to stop trusting a dashboard.
    """
    latest_run = store.latest_run_at()
    floors = store.floors()
    notified = store.already_notified()

    sql = """
        SELECT l.*,
               (SELECT ph.landed_usd FROM price_history ph
                 WHERE ph.fingerprint = l.fingerprint
                 ORDER BY ph.seen_at ASC LIMIT 1) AS first_price
        FROM listings l
        WHERE l.status = 'active'
          AND l.score IS NOT NULL
          AND l.landed_usd IS NOT NULL
          AND l.score >= ?
    """
    params: list = [min_score]

    if region:
        sql += " AND l.region = ?"
        params.append(region)
    if max_landed is not None:
        sql += " AND l.landed_usd <= ?"
        params.append(max_landed)

    rows = store.connection.execute(sql, params).fetchall()

    # Cheapest currently-listed price per model — the reference that actually
    # describes something buyable.
    best_by_model: dict[str, float] = {}
    for row in rows:
        key = row["model_key"]
        if key and row["landed_usd"] is not None:
            best = best_by_model.get(key)
            if best is None or row["landed_usd"] < best:
                best_by_model[key] = row["landed_usd"]

    deals: list[Deal] = []
    for row in rows:
        last_seen = datetime.fromisoformat(row["last_seen"])
        freshness = classify_freshness(last_seen, row["status"], latest_run)

        if not include_stale and freshness is Freshness.STALE:
            continue

        model = config.model_by_key(row["model_key"]) if row["model_key"] else None
        region_cfg = _region_cfg(config, row["region"])

        deals.append(Deal(
            fingerprint=row["fingerprint"],
            title=row["title"],
            url=row["url"] or "",
            region=row["region"],
            region_flag=region_cfg.flag if region_cfg else "",
            region_name=region_cfg.display if region_cfg else row["region"],
            currency=row["currency"],
            sticker_local=row["sticker_local"] or 0.0,
            landed_usd=row["landed_usd"],
            score=row["score"],
            condition=row["condition"] or "",
            seller_name=row["seller_name"] or "",
            source=row["source"],
            flags=json.loads(row["flags"] or "[]"),
            spec_line=row["spec_line"] or "",
            score_breakdown=row["score_breakdown"] or "",
            landed_explain=row["landed_explain"] or "",
            model_key=row["model_key"],
            model_display=model.display if model else None,
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=last_seen,
            freshness=freshness,
            floor_usd=floors.get(row["model_key"]) if row["model_key"] else None,
            best_available_usd=best_by_model.get(row["model_key"]),
            previous_usd=row["first_price"],
            notified_usd=notified.get(row["fingerprint"]),
        ))

    # Freshness always outranks the sort key: a live listing beats a stale one
    # even if the stale one scores higher, because you can only buy the live one.
    freshness_rank = {
        Freshness.LIVE: 0, Freshness.RECENT: 1,
        Freshness.AGING: 2, Freshness.STALE: 3, Freshness.GONE: 4,
    }
    sorters = {
        "score": lambda d: (freshness_rank[d.freshness], -d.score),
        "price": lambda d: (freshness_rank[d.freshness], d.landed_usd),
        "newest": lambda d: (freshness_rank[d.freshness], -d.first_seen.timestamp()),
        "moved": lambda d: (freshness_rank[d.freshness], d.own_change_usd or 0),
    }
    deals.sort(key=sorters.get(sort, sorters["score"]))
    return deals[:limit]


# ---------------------------------------------------------------------------
# What changed
# ---------------------------------------------------------------------------


def recent_changes(
    store: Store, config: Config, *, hours: int = 24, limit: int = 60
) -> dict[str, list[Change]]:
    """New arrivals, price moves and disappearances inside a time window.

    This is the view the every-8-hours schedule exists to produce. Default 24h
    covers the last three sweeps.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    new_rows = store.connection.execute(
        """SELECT * FROM listings
           WHERE first_seen >= ? AND status = 'active' AND landed_usd IS NOT NULL
           ORDER BY score DESC NULLS LAST LIMIT ?""",
        (since, limit),
    ).fetchall()

    # Price moves: compare the two most recent history points inside the window.
    move_rows = store.connection.execute(
        """SELECT l.title, l.url, l.region, l.landed_usd AS current_usd,
                  (SELECT ph.landed_usd FROM price_history ph
                    WHERE ph.fingerprint = l.fingerprint AND ph.seen_at < ?
                    ORDER BY ph.seen_at DESC LIMIT 1) AS before_usd,
                  l.last_seen
           FROM listings l
           WHERE l.status = 'active' AND l.landed_usd IS NOT NULL
             AND EXISTS (SELECT 1 FROM price_history ph
                          WHERE ph.fingerprint = l.fingerprint AND ph.seen_at >= ?)
           LIMIT ?""",
        (since, since, limit * 3),
    ).fetchall()

    gone_rows = store.connection.execute(
        """SELECT * FROM listings
           WHERE status = 'gone' AND last_seen >= ?
           ORDER BY landed_usd ASC LIMIT ?""",
        ((datetime.now(timezone.utc) - timedelta(days=14)).isoformat(), limit),
    ).fetchall()

    def flag_for(region: str) -> str:
        cfg = _region_cfg(config, region)
        return cfg.flag if cfg else ""

    drops, rises = [], []
    for row in move_rows:
        before = row["before_usd"]
        if before is None or abs(before - row["current_usd"]) < 0.01:
            continue
        change = Change(
            kind="drop" if row["current_usd"] < before else "rise",
            title=row["title"], url=row["url"] or "",
            region_flag=flag_for(row["region"]),
            landed_usd=row["current_usd"], previous_usd=before,
            when=datetime.fromisoformat(row["last_seen"]),
        )
        (drops if change.kind == "drop" else rises).append(change)

    drops.sort(key=lambda c: c.percent or 0)
    rises.sort(key=lambda c: -(c.percent or 0))

    return {
        "new": [
            Change(kind="new", title=r["title"], url=r["url"] or "",
                   region_flag=flag_for(r["region"]), landed_usd=r["landed_usd"],
                   when=datetime.fromisoformat(r["first_seen"]))
            for r in new_rows
        ],
        "drops": drops[:limit],
        "rises": rises[:limit],
        "gone": [
            Change(kind="gone", title=r["title"], url=r["url"] or "",
                   region_flag=flag_for(r["region"]),
                   landed_usd=r["landed_usd"] or 0.0,
                   when=datetime.fromisoformat(r["last_seen"]))
            for r in gone_rows
        ],
    }


# ---------------------------------------------------------------------------
# Target models
# ---------------------------------------------------------------------------


def model_watch(store: Store, config: Config) -> list[ModelWatch]:
    """Each tracked model: what's live now, with the floor as a reference.

    Sorted so anything currently triggering an alert floats to the top, then
    by how close current stock sits to the record low.
    """
    floors = store.floors()
    watches: list[ModelWatch] = []

    for model in config.known_models:
        row = store.connection.execute(
            """SELECT landed_usd, url, region, COUNT(*) OVER () AS live_count
               FROM listings
               WHERE model_key = ? AND status = 'active' AND landed_usd IS NOT NULL
               ORDER BY landed_usd ASC LIMIT 1""",
            (model.key,),
        ).fetchone()

        watches.append(ModelWatch(
            model_key=model.key,
            display=model.display,
            floor_usd=floors.get(model.key, model.floor_usd),
            best_available_usd=row["landed_usd"] if row else None,
            best_url=row["url"] if row else None,
            best_region=row["region"] if row else None,
            live_count=row["live_count"] if row else 0,
            priority_trigger_usd=model.priority_alert_at_or_below_usd,
        ))

    watches.sort(key=lambda w: (
        not w.triggers_alert,
        w.above_floor if w.above_floor is not None else 1e9,
    ))
    return watches


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def health(store: Store) -> Health:
    sources = [dict(r) for r in store.connection.execute(
        """SELECT source,
                  COUNT(*) AS total,
                  SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                  SUM(CASE WHEN score IS NOT NULL THEN 1 ELSE 0 END) AS passed,
                  ROUND(AVG(score), 1) AS avg_score,
                  MAX(last_seen) AS last_produced
           FROM listings GROUP BY source ORDER BY passed DESC"""
    ).fetchall()]

    runs = [dict(r) for r in store.connection.execute(
        """SELECT started_at, finished_at, listings_seen, alerts_sent,
                  digest_sent, discovery_run, notes
           FROM runs ORDER BY id DESC LIMIT 20"""
    ).fetchall()]

    rejections = [dict(r) for r in store.connection.execute(
        """SELECT value AS reason, COUNT(*) AS n
           FROM listings, json_each(listings.reject_reasons)
           WHERE listings.reject_reasons != '[]'
           GROUP BY value ORDER BY n DESC"""
    ).fetchall()]

    return Health(
        sources=sources, runs=runs, rejections=rejections,
        latest_run_at=store.latest_run_at(), totals=store.stats(),
    )


def _region_cfg(config: Config, region_value: str):
    from .models import Region

    try:
        return config.region(Region(region_value))
    except (ValueError, KeyError):
        return None
