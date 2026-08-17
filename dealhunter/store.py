"""
SQLite state: deduplication, price history, floors, and run bookkeeping.

This is what turns a stateless scraper into an agent that knows what it has
already told you. Five things live here:

    listings        every listing ever seen, with first/last seen and status
    price_history   a row per *price change*, so trends are real
    notifications   what we've already told you about, and at what price
    floors          per-model record lows, auto-updated from verified sightings
    runs            bookkeeping: last digest, last discovery pass

Two properties matter more than anything else in this file:

**Idempotency.** Every run must be safe to repeat. All writes are upserts keyed
on the listing fingerprint, and price history only gains a row when the price
actually moves. Running the same fetch twice changes nothing the second time.

**Conservative floor updates.** The floor feeds the ±10 price-vs-floor scoring
component, so a bad floor silently poisons every future score for that model.
Only structured, filter-passing, non-community listings can lower a floor —
a Reddit post claiming $900 must never become the baseline everything else is
judged against.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .models import EvaluatedListing, Flag

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- One row per listing ever seen. `status` goes 'active' -> 'gone' when a
-- listing stops appearing; we never delete, so history stays honest.
CREATE TABLE IF NOT EXISTS listings (
    fingerprint     TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    listing_id      TEXT,
    title           TEXT NOT NULL,
    url             TEXT,
    region          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    sticker_local   REAL,
    landed_usd      REAL,
    -- The FX rate is stored per listing so a comparison made weeks from now
    -- against today's record is still honest.
    fx_rate         REAL,
    fx_source       TEXT,
    fx_fetched_at   TEXT,
    score           REAL,
    model_key       TEXT,
    condition       TEXT,
    seller_name     TEXT,
    flags           TEXT,
    reject_reasons  TEXT,
    -- Reasoning, captured at scoring time. The parsed specs and score
    -- components are not otherwise reconstructable from a stored row, and a
    -- dashboard that shows a number without showing why is not much use.
    spec_line       TEXT,
    score_breakdown TEXT,
    landed_explain  TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_listings_model  ON listings(model_key);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status, last_seen);

-- A row only when the price CHANGES, so the table stays small and every row
-- is meaningful.
CREATE TABLE IF NOT EXISTS price_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL,
    seen_at       TEXT NOT NULL,
    sticker_local REAL,
    landed_usd    REAL,
    fx_rate       REAL,
    FOREIGN KEY (fingerprint) REFERENCES listings(fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_price_fingerprint ON price_history(fingerprint, seen_at);

-- What we've already told you about. `landed_usd` is the price we quoted,
-- which is what a later price drop gets measured against.
CREATE TABLE IF NOT EXISTS notifications (
    fingerprint TEXT PRIMARY KEY,
    notified_at TEXT NOT NULL,
    landed_usd  REAL NOT NULL,
    score       REAL,
    kind        TEXT
);

-- Per-model record lows. Seeded from config, lowered only by verified sightings.
CREATE TABLE IF NOT EXISTS floors (
    model_key           TEXT PRIMARY KEY,
    floor_usd           REAL NOT NULL,
    source_fingerprint  TEXT,
    set_at              TEXT NOT NULL,
    note                TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    listings_seen  INTEGER DEFAULT 0,
    alerts_sent    INTEGER DEFAULT 0,
    digest_sent    INTEGER DEFAULT 0,
    discovery_run  INTEGER DEFAULT 0,
    notes          TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PriceMovement:
    """A price change worth mentioning in the digest."""

    fingerprint: str
    title: str
    region: str
    previous_usd: float
    current_usd: float
    url: str

    @property
    def delta(self) -> float:
        return self.current_usd - self.previous_usd

    @property
    def percent(self) -> float:
        if self.previous_usd <= 0:
            return 0.0
        return self.delta / self.previous_usd * 100.0

    def summary(self) -> str:
        direction = "down" if self.delta < 0 else "up"
        return (
            f"{self.title[:60]} [{self.region}] — {direction} "
            f"${abs(self.delta):,.0f} ({abs(self.percent):.1f}%): "
            f"${self.previous_usd:,.0f} → ${self.current_usd:,.0f}"
        )


class Store:
    """The SQLite-backed state. Use as a context manager."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        # WAL keeps reads fast and makes an interrupted run far less likely to
        # leave a corrupt file behind — which matters when CI can be killed.
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @classmethod
    def from_config(cls, config: Config, path: str | Path | None = None) -> "Store":
        from .config import PROJECT_ROOT

        if path is None:
            configured = (config.raw_sources or {}).get("database_path", "data/deals.db")
            path = Path(configured)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
        return cls(path)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Commit, fold the WAL back into the main file, and close.

        The checkpoint matters for deployment: WAL mode leaves `-wal` and
        `-shm` sidecar files, and the GitHub Actions workflow commits only
        `deals.db`. Without truncating the WAL, the most recent run's writes
        would live in an uncommitted sidecar and silently vanish.
        """
        self.connection.commit()
        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:  # pragma: no cover - non-fatal
            log.warning("Could not checkpoint the WAL: %s", exc)
        self.connection.close()

    @contextmanager
    def _tx(self):
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _migrate(self) -> None:
        """Create or upgrade the schema.

        Version-stamped so future changes have somewhere obvious to live. All
        DDL is `IF NOT EXISTS`, so running this against an existing database is
        a no-op — part of the idempotency guarantee.
        """
        with self._tx() as connection:
            connection.executescript(SCHEMA)
            row = connection.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row["version"] < SCHEMA_VERSION:
                if row["version"] < 2:
                    # v2 added the reasoning columns. ADD COLUMN is
                    # non-destructive and cheap, so an existing database
                    # upgrades in place without losing a single row.
                    for column in ("spec_line", "score_breakdown", "landed_explain"):
                        try:
                            connection.execute(
                                f"ALTER TABLE listings ADD COLUMN {column} TEXT"
                            )
                        except sqlite3.OperationalError:
                            pass    # already present; nothing to do

                connection.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )

    # -----------------------------------------------------------------------
    # Listings and price history
    # -----------------------------------------------------------------------

    def record_listings(
        self, evaluated: list[EvaluatedListing], config: Config | None = None
    ) -> None:
        """Upsert every listing this run saw, and log any price change.

        Rejected listings are stored too — with their reasons — so you can
        audit what was thrown away without re-running the fetch.

        Pass `config` to also capture the reasoning (spec line, score
        breakdown, landed-cost derivation). Those cannot be reconstructed from
        a stored row later, so if they aren't captured here the dashboard can
        only show numbers without explanations.
        """
        # Imported here rather than at module scope to keep the store usable
        # without pulling in the notification layer.
        from .notify.render import spec_line as build_spec_line
        from .regions import explain_landed_cost

        now = _now()

        with self._tx() as connection:
            for item in evaluated:
                listing = item.listing
                fingerprint = item.fingerprint
                landed = item.landed.landed_usd if item.landed else None

                previous = connection.execute(
                    "SELECT landed_usd FROM listings WHERE fingerprint = ?",
                    (fingerprint,),
                ).fetchone()

                explain = ""
                if item.landed is not None and config is not None:
                    explain = explain_landed_cost(
                        item.landed, config.region(listing.region)
                    )

                connection.execute(
                    """
                    INSERT INTO listings (
                        fingerprint, source, listing_id, title, url, region,
                        currency, sticker_local, landed_usd, fx_rate, fx_source,
                        fx_fetched_at, score, model_key, condition, seller_name,
                        flags, reject_reasons, spec_line, score_breakdown,
                        landed_explain, first_seen, last_seen, status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        title           = excluded.title,
                        url             = excluded.url,
                        sticker_local   = excluded.sticker_local,
                        landed_usd      = excluded.landed_usd,
                        fx_rate         = excluded.fx_rate,
                        fx_source       = excluded.fx_source,
                        fx_fetched_at   = excluded.fx_fetched_at,
                        score           = excluded.score,
                        model_key       = excluded.model_key,
                        condition       = excluded.condition,
                        seller_name     = excluded.seller_name,
                        flags           = excluded.flags,
                        reject_reasons  = excluded.reject_reasons,
                        spec_line       = excluded.spec_line,
                        score_breakdown = excluded.score_breakdown,
                        landed_explain  = excluded.landed_explain,
                        last_seen       = excluded.last_seen,
                        -- Seeing it again resurrects a listing marked gone.
                        status          = 'active'
                    """,
                    (
                        fingerprint, listing.source, listing.listing_id,
                        listing.title, listing.url, listing.region.value,
                        listing.currency, listing.sticker_price_local, landed,
                        item.landed.fx_rate_to_usd if item.landed else None,
                        item.landed.fx_source if item.landed else None,
                        item.landed.fx_fetched_at.isoformat() if item.landed else None,
                        item.score.total if item.score else None,
                        item.specs.model_key, listing.condition.value,
                        listing.seller_name,
                        json.dumps([f.value for f in item.all_flags]),
                        json.dumps([r.value for r in item.reject_reasons]),
                        build_spec_line(item),
                        item.score.breakdown_line(top_n=9) if item.score else None,
                        explain,
                        now, now,
                    ),
                )

                # Only log a price row when the price actually moved. Otherwise
                # eight runs a day would add 24 identical rows per listing.
                if landed is not None:
                    changed = previous is None or previous["landed_usd"] is None or (
                        abs(previous["landed_usd"] - landed) > 0.005
                    )
                    if changed:
                        connection.execute(
                            """INSERT INTO price_history
                               (fingerprint, seen_at, sticker_local, landed_usd, fx_rate)
                               VALUES (?,?,?,?,?)""",
                            (
                                fingerprint, now, listing.sticker_price_local, landed,
                                item.landed.fx_rate_to_usd if item.landed else None,
                            ),
                        )

    def price_movements(
        self, evaluated: list[EvaluatedListing], min_percent: float = 1.0
    ) -> tuple[list[PriceMovement], list[PriceMovement]]:
        """Drops and rises for the digest, comparing against the previous price.

        Call this *before* `record_listings`, while the stored value is still
        the previous one.
        """
        drops: list[PriceMovement] = []
        rises: list[PriceMovement] = []

        for item in evaluated:
            if item.rejected or not item.landed:
                continue

            row = self.connection.execute(
                "SELECT landed_usd, title FROM listings WHERE fingerprint = ?",
                (item.fingerprint,),
            ).fetchone()
            if row is None or row["landed_usd"] is None:
                continue

            movement = PriceMovement(
                fingerprint=item.fingerprint,
                title=item.listing.title,
                region=item.listing.region.value,
                previous_usd=row["landed_usd"],
                current_usd=item.landed.landed_usd,
                url=item.listing.url,
            )
            if abs(movement.percent) < min_percent:
                continue

            (drops if movement.delta < 0 else rises).append(movement)

        drops.sort(key=lambda m: m.percent)
        rises.sort(key=lambda m: -m.percent)
        return drops, rises

    def expire_stale(self, days: int = 7) -> list[str]:
        """Mark listings not seen in `days` as gone, and return their titles.

        Surfaced in the digest so a listing vanishing is visible — that usually
        means it sold, which is itself a price signal.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with self._tx() as connection:
            rows = connection.execute(
                """SELECT fingerprint, title, region, landed_usd FROM listings
                   WHERE status = 'active' AND last_seen < ?""",
                (cutoff,),
            ).fetchall()

            if rows:
                connection.execute(
                    "UPDATE listings SET status = 'gone' WHERE status = 'active' "
                    "AND last_seen < ?",
                    (cutoff,),
                )

        return [
            f"{row['title'][:60]} [{row['region']}] — last seen at "
            f"${row['landed_usd']:,.0f}" if row["landed_usd"] else row["title"][:60]
            for row in rows
        ]

    # -----------------------------------------------------------------------
    # Notifications
    # -----------------------------------------------------------------------

    def already_notified(self) -> dict[str, float]:
        """Fingerprint -> the landed price we last quoted you.

        This is what the router uses to suppress repeats and to measure a
        price drop against.
        """
        rows = self.connection.execute(
            "SELECT fingerprint, landed_usd FROM notifications"
        ).fetchall()
        return {row["fingerprint"]: row["landed_usd"] for row in rows}

    def record_notifications(
        self, fingerprints_with_prices: list[tuple[str, float, float]], kind: str = "alert"
    ) -> None:
        """Remember what we just sent. Upsert, so a repeat run is harmless."""
        now = _now()
        with self._tx() as connection:
            connection.executemany(
                """INSERT INTO notifications (fingerprint, notified_at, landed_usd, score, kind)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       notified_at = excluded.notified_at,
                       landed_usd  = excluded.landed_usd,
                       score       = excluded.score,
                       kind        = excluded.kind""",
                [(fp, now, price, score, kind)
                 for fp, price, score in fingerprints_with_prices],
            )

    # -----------------------------------------------------------------------
    # Price floors
    # -----------------------------------------------------------------------

    def seed_floors(self, config: Config) -> None:
        """Load the researched floors from config, without clobbering better ones.

        Runs every startup. If the database already holds a *lower* verified
        floor than the config seed, the database wins — that is the whole point
        of tracking them.
        """
        with self._tx() as connection:
            for model in config.known_models:
                row = connection.execute(
                    "SELECT floor_usd FROM floors WHERE model_key = ?", (model.key,)
                ).fetchone()

                if row is None:
                    connection.execute(
                        """INSERT INTO floors (model_key, floor_usd, set_at, note)
                           VALUES (?,?,?,?)""",
                        (model.key, model.floor_usd, _now(), "seeded from config.yaml"),
                    )
                elif model.floor_usd < row["floor_usd"]:
                    # You lowered the seed by hand; trust that over our record.
                    connection.execute(
                        """UPDATE floors SET floor_usd = ?, set_at = ?, note = ?
                           WHERE model_key = ?""",
                        (model.floor_usd, _now(), "lowered by config.yaml edit", model.key),
                    )

    def floors(self) -> dict[str, float]:
        """model_key -> current floor, for the scoring engine."""
        rows = self.connection.execute(
            "SELECT model_key, floor_usd FROM floors"
        ).fetchall()
        return {row["model_key"]: row["floor_usd"] for row in rows}

    def update_floors(self, evaluated: list[EvaluatedListing]) -> list[str]:
        """Lower a model's floor when a *verified* cheaper listing appears.

        Deliberately strict about what counts as verified, because the floor
        feeds the ±10 price-vs-floor score and a bad one poisons every future
        comparison for that model. A listing qualifies only if it:

          * passed every hard filter,
          * matched a known model,
          * came from a structured source rather than a community claim
            (no UNVERIFIED_SOURCE — a Reddit post is not a price), and
          * isn't flagged HIGH RISK or as a multi-variation listing, where the
            advertised price may not belong to the config in the title.

        Returns human-readable notes for the digest.
        """
        disqualifying = {
            Flag.UNVERIFIED_SOURCE,
            Flag.HIGH_RISK,
            Flag.MULTI_VARIATION_LISTING,
            Flag.FX_STALE,          # an approximate rate must not set a record
        }
        notes: list[str] = []

        with self._tx() as connection:
            for item in evaluated:
                if item.rejected or not item.landed or not item.specs.model_key:
                    continue
                if disqualifying & set(item.all_flags):
                    continue

                model_key = item.specs.model_key
                landed = item.landed.landed_usd

                row = connection.execute(
                    "SELECT floor_usd FROM floors WHERE model_key = ?", (model_key,)
                ).fetchone()
                if row is not None and landed >= row["floor_usd"]:
                    continue

                previous = row["floor_usd"] if row else None
                connection.execute(
                    """INSERT INTO floors (model_key, floor_usd, source_fingerprint, set_at, note)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(model_key) DO UPDATE SET
                           floor_usd          = excluded.floor_usd,
                           source_fingerprint = excluded.source_fingerprint,
                           set_at             = excluded.set_at,
                           note               = excluded.note""",
                    (model_key, landed, item.fingerprint, _now(),
                     f"verified sighting from {item.listing.source}"),
                )

                if previous is not None:
                    notes.append(
                        f"{item.specs.model_display or model_key}: new record low "
                        f"${landed:,.0f} (was ${previous:,.0f}) via {item.listing.source}"
                    )

        return notes

    # -----------------------------------------------------------------------
    # Run bookkeeping
    # -----------------------------------------------------------------------

    def start_run(self) -> int:
        with self._tx() as connection:
            cursor = connection.execute(
                "INSERT INTO runs (started_at) VALUES (?)", (_now(),)
            )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        listings_seen: int = 0,
        alerts_sent: int = 0,
        digest_sent: bool = False,
        discovery_run: bool = False,
        notes: str = "",
    ) -> None:
        with self._tx() as connection:
            connection.execute(
                """UPDATE runs SET finished_at = ?, listings_seen = ?, alerts_sent = ?,
                                   digest_sent = ?, discovery_run = ?, notes = ?
                   WHERE id = ?""",
                (_now(), listings_seen, alerts_sent, int(digest_sent),
                 int(discovery_run), notes, run_id),
            )

    def latest_run_at(self) -> datetime | None:
        """When the most recent completed sweep started.

        This is the dividing line between "confirmed available right now" and
        "we haven't actually seen this since some earlier sweep". With runs
        every 8 hours, a listing that missed the last one is already suspect —
        far more useful than waiting 7 days to call it gone.
        """
        row = self.connection.execute(
            "SELECT started_at FROM runs WHERE finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["started_at"]) if row else None

    def previous_run_at(self) -> datetime | None:
        """When the sweep before last started — the window for 'what changed'."""
        rows = self.connection.execute(
            "SELECT started_at FROM runs WHERE finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 2"
        ).fetchall()
        return datetime.fromisoformat(rows[1]["started_at"]) if len(rows) > 1 else None

    def last_digest_at(self) -> datetime | None:
        """When we last sent a digest, for the 09:00 PKT scheduling rule."""
        row = self.connection.execute(
            "SELECT finished_at FROM runs WHERE digest_sent = 1 "
            "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["finished_at"]) if row else None

    def last_discovery_at(self) -> datetime | None:
        row = self.connection.execute(
            "SELECT finished_at FROM runs WHERE discovery_run = 1 "
            "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["finished_at"]) if row else None

    # -----------------------------------------------------------------------
    # Maintenance
    # -----------------------------------------------------------------------

    def prune(self, keep_days: int = 180) -> int:
        """Drop old history for listings long gone.

        The database is committed back to the repository between runs, so it
        growing without bound would bloat the repo forever. Active listings and
        floors are never touched.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()

        with self._tx() as connection:
            cursor = connection.execute(
                """DELETE FROM price_history WHERE fingerprint IN (
                       SELECT fingerprint FROM listings
                       WHERE status = 'gone' AND last_seen < ?
                   )""",
                (cutoff,),
            )
            removed = cursor.rowcount
            connection.execute(
                "DELETE FROM listings WHERE status = 'gone' AND last_seen < ?",
                (cutoff,),
            )

        return removed

    def vacuum(self) -> None:
        """Compact the file. Worth doing after a prune, before committing."""
        self.connection.execute("VACUUM")

    def stats(self) -> dict[str, int]:
        def count(sql: str) -> int:
            return int(self.connection.execute(sql).fetchone()[0])

        return {
            "listings": count("SELECT COUNT(*) FROM listings"),
            "active": count("SELECT COUNT(*) FROM listings WHERE status='active'"),
            "gone": count("SELECT COUNT(*) FROM listings WHERE status='gone'"),
            "price_points": count("SELECT COUNT(*) FROM price_history"),
            "notified": count("SELECT COUNT(*) FROM notifications"),
            "runs": count("SELECT COUNT(*) FROM runs"),
        }
