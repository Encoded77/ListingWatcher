"""SQLite persistence: listings (key watch + source + listing_id), price history, source state.

Several watches share the same database, column `watch`. A database created before watches existed is
migrated at startup: its rows are attached to the `hdd` watch. `WatchStore` is the view bound to one
watch that the pipeline, the filter and the digest use without caring about the column."""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import LEGACY_WATCH
from .models import Decision, Listing, ModelInfo, worst_status

_LISTING_COLS = """
    watch TEXT NOT NULL DEFAULT 'hdd',
    source TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    url TEXT,
    title TEXT,
    model TEXT,
    family TEXT,
    verdict TEXT,
    reasons TEXT,
    flags TEXT,
    price REAL,
    shipping REAL,
    fees REAL,
    delivered REAL,
    unit_price REAL,
    quantity INTEGER,
    tier TEXT,
    keep INTEGER,
    suspicious TEXT,
    condition TEXT,
    seller TEXT,
    seller_rating REAL,
    seller_reviews INTEGER,
    location TEXT,
    delivery INTEGER,
    smart_hours INTEGER,
    status TEXT,
    first_seen TEXT,
    last_seen TEXT,
    missed_scans INTEGER DEFAULT 0,
    notified_price REAL,
    notified_at TEXT,
    notify_count INTEGER DEFAULT 0,
    raw TEXT,
    review TEXT DEFAULT '',
    note TEXT DEFAULT '',
    posted_at TEXT,
    attrs TEXT,
    PRIMARY KEY (watch, source, listing_id)
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS listings ({_LISTING_COLS});
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch TEXT NOT NULL DEFAULT 'hdd',
    source TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    price REAL,
    delivered REAL,
    unit_price REAL,
    model TEXT,
    family TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    blocked_until TEXT,
    failures INTEGER DEFAULT 0,
    last_ok TEXT,
    last_error TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str = ":memory:", history_days: int = 30):
        self.path = path
        self.history_days = history_days
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self._migrate()

    def _cols(self, table: str) -> list[str]:
        return [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]

    def _migrate(self) -> None:
        cols = self._cols("listings")
        for name, ddl in (("review", "TEXT DEFAULT ''"), ("note", "TEXT DEFAULT ''"), ("posted_at", "TEXT"), ("attrs", "TEXT")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE listings ADD COLUMN {name} {ddl}")
        if "watch" not in cols:
            # the primary key changes: SQLite requires rebuilding the table. Existing rows
            # belong to the legacy watch.
            common = [c for c in self._cols("listings") if c != "watch"]
            self.db.executescript(f"CREATE TABLE listings_new ({_LISTING_COLS});")
            self.db.execute(
                f"INSERT INTO listings_new (watch, {', '.join(common)}) SELECT ?, {', '.join(common)} FROM listings",
                (LEGACY_WATCH,),
            )
            self.db.executescript("DROP TABLE listings; ALTER TABLE listings_new RENAME TO listings;")
        if "watch" not in self._cols("price_history"):
            self.db.execute(f"ALTER TABLE price_history ADD COLUMN watch TEXT NOT NULL DEFAULT '{LEGACY_WATCH}'")
            self.db.executescript("DROP INDEX IF EXISTS ix_price_history_model; DROP INDEX IF EXISTS ix_price_history_family;")
        # indexes created after the migration: they reference the watch column
        self.db.executescript(
            "CREATE INDEX IF NOT EXISTS ix_price_history_model ON price_history (watch, model, seen_at);"
            "CREATE INDEX IF NOT EXISTS ix_price_history_family ON price_history (watch, family, seen_at);"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def for_watch(self, watch: str) -> "WatchStore":
        return WatchStore(self, watch)

    # ------------------------------------------------------------------ listings
    def get(self, watch: str, source: str, listing_id: str) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT * FROM listings WHERE watch=? AND source=? AND listing_id=?", (watch, source, listing_id)
        ).fetchone()
        return dict(row) if row else None

    def upsert(self, watch: str, listing: Listing, info: ModelInfo, decision: Decision,
               now: datetime | None = None) -> dict[str, Any]:
        """Records the observation and returns an event:
        {"event": new|price_drop|price_up|unchanged|status_change, "prev": previous row or None}."""
        now = now or utcnow()
        ts = iso(now)
        prev = self.get(watch, listing.source, listing.listing_id)
        status = listing.status
        if prev:
            # worst status wins: a cached page can show "active" after a "Vendu"
            status = worst_status(prev["status"] or "active", status)
            if prev["status"] == "gone" and listing.status == "active":
                status = "active"      # a listing that reappears is no longer "gone"
        record = dict(
            watch=watch, source=listing.source, listing_id=listing.listing_id, url=listing.url, title=listing.title,
            model=info.model, family=info.family, verdict=info.verdict,
            reasons=json.dumps(info.reasons), flags=json.dumps(info.flags),
            price=listing.price, shipping=listing.shipping, fees=listing.fees,
            delivered=listing.delivered_price, unit_price=decision.unit_price, quantity=info.quantity,
            tier=decision.tier, keep=int(decision.keep), suspicious=json.dumps(decision.suspicious),
            condition=listing.condition, seller=listing.seller_name, seller_rating=listing.seller_rating,
            seller_reviews=listing.seller_reviews, location=listing.location,
            delivery=None if listing.delivery is None else int(listing.delivery),
            attrs=json.dumps(info.attrs, ensure_ascii=False, default=str), status=status, last_seen=ts, missed_scans=0,
            posted_at=listing.posted_at or None,
            raw=json.dumps(listing.raw, ensure_ascii=False, default=str)[:20000],
        )
        if prev is None:
            record.update(first_seen=ts, notified_price=None, notified_at=None, notify_count=0)
            cols = ", ".join(record)
            self.db.execute(f"INSERT INTO listings ({cols}) VALUES ({', '.join('?' * len(record))})", tuple(record.values()))
            self._add_price(watch, listing, info, decision, ts)
            self.db.commit()
            return {"event": "new", "prev": None}

        sets = ", ".join(f"{k}=?" for k in record)
        self.db.execute(
            f"UPDATE listings SET {sets} WHERE watch=? AND source=? AND listing_id=?",
            tuple(record.values()) + (watch, listing.source, listing.listing_id),
        )
        event = "unchanged"
        if prev["unit_price"] is not None and abs(prev["unit_price"] - decision.unit_price) > 0.005:
            self._add_price(watch, listing, info, decision, ts)
            event = "price_drop" if decision.unit_price < prev["unit_price"] else "price_up"
        elif status != prev["status"]:
            event = "status_change"
        self.db.commit()
        return {"event": event, "prev": prev}

    def _add_price(self, watch: str, listing: Listing, info: ModelInfo, decision: Decision, ts: str) -> None:
        self.db.execute(
            "INSERT INTO price_history (watch, source, listing_id, seen_at, price, delivered, unit_price, model, family)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (watch, listing.source, listing.listing_id, ts, listing.price, listing.delivered_price,
             decision.unit_price, info.model, info.family),
        )

    def mark_notified(self, watch: str, source: str, listing_id: str, unit_price: float,
                      now: datetime | None = None) -> None:
        self.db.execute(
            "UPDATE listings SET notified_price=?, notified_at=?, notify_count=notify_count+1"
            " WHERE watch=? AND source=? AND listing_id=?",
            (unit_price, iso(now or utcnow()), watch, source, listing_id),
        )
        self.db.commit()

    def mark_missing(self, watch: str, source: str, seen_ids: set[str], gone_after: int = 2) -> int:
        """Listings of this watch and source missing from this scan: bump the counter, then status 'gone'."""
        rows = self.db.execute(
            "SELECT listing_id, missed_scans FROM listings WHERE watch=? AND source=? AND status IN ('active','pending')",
            (watch, source),
        ).fetchall()
        gone = 0
        for r in rows:
            if r["listing_id"] in seen_ids:
                continue
            missed = (r["missed_scans"] or 0) + 1
            if missed >= gone_after:
                self.db.execute("UPDATE listings SET status='gone', missed_scans=? WHERE watch=? AND source=? AND listing_id=?",
                                (missed, watch, source, r["listing_id"]))
                gone += 1
            else:
                self.db.execute("UPDATE listings SET missed_scans=? WHERE watch=? AND source=? AND listing_id=?",
                                (missed, watch, source, r["listing_id"]))
        self.db.commit()
        return gone

    def digest_rows(self, watch: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM listings WHERE watch=? AND keep=1 AND status='active' AND suspicious='[]'"
            " AND COALESCE(review,'')<>'ignored' ORDER BY unit_price ASC", (watch,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ review (web interface)
    def list_listings(self, watch: str = "", status: str = "active", keep: str = "1", source: str = "", review: str = "",
                      search: str = "", limit: int = 1000, include_suspicious: bool = True) -> list[dict[str, Any]]:
        where, args = self._listing_filters(watch, status, keep, source, review, search, include_suspicious)
        sql = "SELECT * FROM listings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY unit_price ASC, first_seen DESC LIMIT ?"
        args.append(int(limit))
        return [{k: v for k, v in dict(r).items() if k != "raw"} for r in self.db.execute(sql, args)]

    def count_listings(self, watch: str = "", status: str = "active", keep: str = "1", source: str = "", review: str = "",
                       search: str = "", include_suspicious: bool = True) -> int:
        where, args = self._listing_filters(watch, status, keep, source, review, search, include_suspicious)
        sql = "SELECT COUNT(*) FROM listings" + (" WHERE " + " AND ".join(where) if where else "")
        return int(self.db.execute(sql, args).fetchone()[0])

    @staticmethod
    def _listing_filters(watch, status, keep, source, review, search, include_suspicious):
        where, args = [], []
        if watch:
            where.append("watch=?"); args.append(watch)
        if status:
            where.append("status=?"); args.append(status)
        if keep in ("0", "1"):
            where.append("keep=?"); args.append(int(keep))
        if source:
            where.append("source=?"); args.append(source)
        if review == "none":
            where.append("COALESCE(review,'')=''")
        elif review:
            where.append("review=?"); args.append(review)
        if not include_suspicious:
            where.append("suspicious='[]'")
        if search:
            like = f"%{search}%"
            where.append("(title LIKE ? OR model LIKE ? OR family LIKE ? OR seller LIKE ? OR location LIKE ? OR note LIKE ?)")
            args += [like] * 6
        return where, args

    def set_review(self, watch: str, source: str, listing_id: str, review: str, note: Optional[str] = None) -> None:
        if note is None:
            self.db.execute("UPDATE listings SET review=? WHERE watch=? AND source=? AND listing_id=?",
                            (review, watch, source, listing_id))
        else:
            self.db.execute("UPDATE listings SET review=?, note=? WHERE watch=? AND source=? AND listing_id=?",
                            (review, note[:1000], watch, source, listing_id))
        self.db.commit()

    def price_history(self, watch: str, source: str, listing_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT seen_at, price, delivered, unit_price FROM price_history"
            " WHERE watch=? AND source=? AND listing_id=? ORDER BY id",
            (watch, source, listing_id)).fetchall()
        return [dict(r) for r in rows]

    def count_review(self, watch: str, review: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM listings WHERE watch=? AND review=? AND status='active'",
                               (watch, review)).fetchone()[0]

    def count_new_today(self, watch: str) -> int:
        since = iso(utcnow() - timedelta(hours=24))
        return self.db.execute("SELECT COUNT(*) FROM listings WHERE watch=? AND keep=1 AND first_seen>=?",
                               (watch, since)).fetchone()[0]

    def source_states(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM source_state")]

    def watches(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT watch FROM listings ORDER BY watch")]

    def stats(self, watch: str = "") -> dict[str, Any]:
        q = self.db.execute
        w, a = ("WHERE watch=?", (watch,)) if watch else ("", ())
        return {
            "listings": q(f"SELECT COUNT(*) FROM listings {w}", a).fetchone()[0],
            "kept_active": q(f"SELECT COUNT(*) FROM listings {w}{' AND' if w else ' WHERE'} keep=1 AND status='active'", a).fetchone()[0],
            "by_status": {r[0]: r[1] for r in q(f"SELECT status, COUNT(*) FROM listings {w} GROUP BY status", a)},
            "by_verdict": {r[0]: r[1] for r in q(f"SELECT verdict, COUNT(*) FROM listings {w} GROUP BY verdict", a)},
            "notified": q(f"SELECT COUNT(*) FROM listings {w}{' AND' if w else ' WHERE'} notify_count>0", a).fetchone()[0],
            "price_points": q(f"SELECT COUNT(*) FROM price_history {w}", a).fetchone()[0],
        }

    # ------------------------------------------------------------------ market
    def median_unit_price(self, watch: str, model: Optional[str], family: Optional[str]) -> tuple[Optional[float], int]:
        since = iso(utcnow() - timedelta(days=self.history_days))
        for col, val in (("model", model), ("family", family)):
            if not val:
                continue
            # one point per listing (the latest), so listings that move are not over-weighted;
            # only single-unit, kept, non-suspicious listings, otherwise badly divided lots
            # and scams drag the median down and whitewash each other
            rows = self.db.execute(
                f"SELECT p.unit_price FROM price_history p JOIN listings l"
                f" ON l.watch=p.watch AND l.source=p.source AND l.listing_id=p.listing_id"
                f" WHERE p.watch=? AND p.{col}=? AND p.seen_at>=? AND l.keep=1 AND l.quantity=1 AND l.suspicious='[]'"
                f" AND p.id = (SELECT MAX(id) FROM price_history WHERE watch=p.watch AND source=p.source AND listing_id=p.listing_id)",
                (watch, val, since),
            ).fetchall()
            vals = [r[0] for r in rows if r[0]]
            if len(vals) >= 1:
                return statistics.median(vals), len(vals)
        return None, 0

    # ------------------------------------------------------------------ meta / sources
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))
        self.db.commit()

    def source_blocked_until(self, source: str) -> Optional[datetime]:
        row = self.db.execute("SELECT blocked_until FROM source_state WHERE source=?", (source,)).fetchone()
        if not row or not row[0]:
            return None
        until = datetime.fromisoformat(row[0])
        return until if until > utcnow() else None

    def block_source(self, source: str, hours: float, error: str) -> None:
        until = iso(utcnow() + timedelta(hours=hours))
        self.db.execute(
            "INSERT INTO source_state (source, blocked_until, failures, last_error) VALUES (?,?,1,?)"
            " ON CONFLICT(source) DO UPDATE SET blocked_until=excluded.blocked_until,"
            " failures=failures+1, last_error=excluded.last_error",
            (source, until, error[:500]),
        )
        self.db.commit()

    def source_ok(self, source: str) -> None:
        self.db.execute(
            "INSERT INTO source_state (source, blocked_until, failures, last_ok) VALUES (?,NULL,0,?)"
            " ON CONFLICT(source) DO UPDATE SET blocked_until=NULL, failures=0, last_ok=excluded.last_ok",
            (source, iso(utcnow())),
        )
        self.db.commit()


class WatchStore:
    """View of a Store bound to one watch: same methods, without repeating the `watch` column.
    Global methods (meta, source state, close) are delegated as they are."""

    def __init__(self, store: Store, watch: str):
        self.store = store
        self.watch = watch

    @property
    def path(self) -> str:
        return self.store.path

    @property
    def history_days(self) -> int:
        return self.store.history_days

    def get(self, source, listing_id):
        return self.store.get(self.watch, source, listing_id)

    def upsert(self, listing, info, decision, now=None):
        return self.store.upsert(self.watch, listing, info, decision, now)

    def mark_notified(self, source, listing_id, unit_price, now=None):
        return self.store.mark_notified(self.watch, source, listing_id, unit_price, now)

    def mark_missing(self, source, seen_ids, gone_after=2):
        return self.store.mark_missing(self.watch, source, seen_ids, gone_after)

    def digest_rows(self):
        return self.store.digest_rows(self.watch)

    def set_review(self, source, listing_id, review, note=None):
        return self.store.set_review(self.watch, source, listing_id, review, note)

    def price_history(self, source, listing_id):
        return self.store.price_history(self.watch, source, listing_id)

    def median_unit_price(self, model, family):
        return self.store.median_unit_price(self.watch, model, family)

    def stats(self):
        return self.store.stats(self.watch)

    def __getattr__(self, name):  # meta, source_state, close, list_listings…
        return getattr(self.store, name)
