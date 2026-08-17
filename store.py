"""store — SQLite persistence for listings, price history and crawl runs.

WAL mode with a busy timeout: the crawler thread, the Telegram bot thread and
the dashboard's request threads all touch this file concurrently, and WAL lets
the readers keep working while a crawl writes.
"""
import hashlib
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    address       TEXT,
    city          TEXT,
    state         TEXT,
    zip           TEXT,
    lat           REAL,
    lon           REAL,
    price         INTEGER,
    beds          REAL,
    baths         REAL,
    sqft          INTEGER,
    prop_type     TEXT,
    property_name TEXT,
    is_complex    INTEGER DEFAULT 0,
    unit          TEXT,
    available     TEXT,
    photo         TEXT,
    description   TEXT,
    score         REAL,
    distances     TEXT,          -- JSON {anchor: miles}
    is_baseline   INTEGER DEFAULT 0,
    first_seen    TEXT,
    last_seen     TEXT,
    active        INTEGER DEFAULT 1,
    alerted       INTEGER DEFAULT 0,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(active, score DESC);
CREATE INDEX IF NOT EXISTS idx_listings_seen   ON listings(first_seen DESC);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id TEXT NOT NULL,
    price      INTEGER NOT NULL,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (listing_id, price, seen_at)
);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   TEXT,
    finished  TEXT,
    found     INTEGER DEFAULT 0,
    new       INTEGER DEFAULT 0,
    drops     INTEGER DEFAULT 0,
    ok        INTEGER DEFAULT 1,
    detail    TEXT           -- JSON {source: {"n": int, "error": str|null}}
);

CREATE TABLE IF NOT EXISTS pins (
    listing_id TEXT PRIMARY KEY,
    note       TEXT,
    pinned_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS http_cache (
    url      TEXT PRIMARY KEY,
    body     TEXT,
    fetched  REAL
);
"""


def listing_id(source: str, source_id: str) -> str:
    return hashlib.sha1(f"{source}:{source_id}".encode()).hexdigest()[:16]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    # ── writes ──────────────────────────────────────────────────────────────

    def upsert(self, rec: dict) -> str:
        """Insert or refresh one listing. Returns 'new' | 'drop' | 'seen'.

        A price change is recorded in price_history and reported as 'drop'
        only when the new price is lower — a rent increase is noise, a rent
        cut is the thing worth a push notification.
        """
        lid = listing_id(rec["source"], rec["source_id"])
        ts = now()
        with self._lock, self._conn() as c:
            prev = c.execute("SELECT price, active FROM listings WHERE id=?",
                             (lid,)).fetchone()
            cols = dict(
                id=lid, source=rec["source"], source_id=str(rec["source_id"]),
                url=rec.get("url"), title=rec.get("title"),
                address=rec.get("address"), city=rec.get("city"),
                state=rec.get("state"), zip=rec.get("zip"),
                lat=rec.get("lat"), lon=rec.get("lon"),
                price=rec.get("price"), beds=rec.get("beds"),
                baths=rec.get("baths"), sqft=rec.get("sqft"),
                prop_type=rec.get("prop_type"),
                property_name=rec.get("property_name"),
                is_complex=int(bool(rec.get("is_complex"))),
                unit=rec.get("unit"), available=rec.get("available"),
                photo=rec.get("photo"), description=rec.get("description"),
                score=rec.get("score"),
                distances=json.dumps(rec.get("distances") or {}),
                is_baseline=int(bool(rec.get("is_baseline"))),
                last_seen=ts, active=1,
                raw=json.dumps(rec.get("raw") or {})[:20000],
            )
            if prev is None:
                cols["first_seen"] = ts
                cols["alerted"] = 0
                keys = ",".join(cols)
                c.execute(f"INSERT INTO listings ({keys}) VALUES "
                          f"({','.join('?' * len(cols))})", tuple(cols.values()))
                if cols["price"]:
                    c.execute("INSERT OR IGNORE INTO price_history VALUES (?,?,?)",
                              (lid, cols["price"], ts))
                return "new"

            sets = ",".join(f"{k}=?" for k in cols)
            c.execute(f"UPDATE listings SET {sets} WHERE id=?",
                      (*cols.values(), lid))
            outcome = "seen"
            if cols["price"] and prev["price"] and cols["price"] != prev["price"]:
                c.execute("INSERT OR IGNORE INTO price_history VALUES (?,?,?)",
                          (lid, cols["price"], ts))
                if cols["price"] < prev["price"]:
                    outcome = "drop"
            return outcome

    def deactivate_missing(self, seen_ids: set, stale_days: int) -> int:
        """Mark listings we did not see this run, and that have aged out, gone.

        Not immediate: a source erroring out for one run would otherwise wipe
        its whole inventory. Only rows untouched for `stale_days` retire.
        """
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - stale_days * 86400))
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE listings SET active=0 WHERE active=1 AND last_seen < ?",
                (cutoff,))
            return cur.rowcount

    def mark_alerted(self, ids) -> None:
        if not ids:
            return
        with self._lock, self._conn() as c:
            c.executemany("UPDATE listings SET alerted=1 WHERE id=?",
                          [(i,) for i in ids])

    def start_run(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("INSERT INTO runs (started) VALUES (?)", (now(),))
            return cur.lastrowid

    def finish_run(self, run_id, found, new, drops, ok, detail) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE runs SET finished=?, found=?, new=?, drops=?, "
                      "ok=?, detail=? WHERE id=?",
                      (now(), found, new, drops, int(ok),
                       json.dumps(detail), run_id))

    def pin(self, lid: str, note: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO pins VALUES (?,?,?)",
                      (lid, note, now()))

    def unpin(self, lid: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM pins WHERE listing_id=?", (lid,))

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    # ── http cache (floorplan lookups are the expensive part of a crawl) ────

    def cache_get(self, url: str, ttl_s: float):
        with self._conn() as c:
            row = c.execute("SELECT body, fetched FROM http_cache WHERE url=?",
                            (url,)).fetchone()
        if row and time.time() - row["fetched"] < ttl_s:
            return row["body"]
        return None

    def cache_put(self, url: str, body: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?)",
                      (url, body, time.time()))

    def cache_prune(self, ttl_s: float) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM http_cache WHERE fetched < ?",
                      (time.time() - ttl_s,))

    # ── reads ───────────────────────────────────────────────────────────────

    def get_meta(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def top(self, limit=10, prop_types=None, active_only=True, source=None):
        q = "SELECT * FROM listings WHERE 1=1"
        args = []
        if active_only:
            q += " AND active=1"
        if prop_types:
            q += f" AND prop_type IN ({','.join('?' * len(prop_types))})"
            args += list(prop_types)
        if source:
            q += " AND source=?"
            args.append(source)
        q += " ORDER BY score DESC NULLS LAST LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def newest(self, limit=10, since=None):
        q = "SELECT * FROM listings WHERE active=1"
        args = []
        if since:
            q += " AND first_seen > ?"
            args.append(since)
        q += " ORDER BY first_seen DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def unalerted(self, min_score: float, limit: int):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM listings WHERE active=1 AND alerted=0 "
                "AND score >= ? ORDER BY score DESC LIMIT ?",
                (min_score, limit))]

    def price_drops(self, limit=10):
        """Listings whose latest recorded price is below their previous one."""
        with self._conn() as c:
            rows = c.execute("""
                SELECT l.*, h.price AS old_price, h.seen_at AS changed_at
                FROM listings l
                JOIN price_history h ON h.listing_id = l.id
                WHERE l.active = 1 AND h.price > l.price
                GROUP BY l.id
                HAVING h.seen_at = MAX(h.seen_at)
                ORDER BY (h.price - l.price) DESC LIMIT ?""", (limit,))
            return [dict(r) for r in rows]

    def history(self, lid: str):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT price, seen_at FROM price_history WHERE listing_id=? "
                "ORDER BY seen_at", (lid,))]

    def pinned(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT l.*, p.note, p.pinned_at FROM pins p "
                "JOIN listings l ON l.id = p.listing_id "
                "ORDER BY p.pinned_at DESC")]

    def baselines(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM listings WHERE is_baseline=1 AND active=1 "
                "ORDER BY price")]

    def find(self, prefix: str):
        """Resolve a short id prefix (what the bot and dashboard show)."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM listings WHERE id LIKE ?",
                            (prefix + "%",)).fetchone()
        return dict(row) if row else None

    def last_runs(self, limit=5):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def stats(self) -> dict:
        with self._conn() as c:
            row = c.execute("""
                SELECT COUNT(*) AS n,
                       SUM(active) AS active,
                       SUM(CASE WHEN active=1 AND prop_type IN
                           ('house','townhouse','duplex','condo') THEN 1 ELSE 0 END) AS houses,
                       SUM(CASE WHEN active=1 AND prop_type='apartment' THEN 1 ELSE 0 END) AS apts,
                       AVG(CASE WHEN active=1 THEN price END) AS avg_price,
                       MIN(CASE WHEN active=1 THEN price END) AS min_price
                FROM listings""").fetchone()
            return dict(row)
