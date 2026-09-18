"""SQLite persistence: samples, outage incidents, aggregates and exports.

Two tables:

``samples``
    one row per probe. A latency round writes one row per target (tagged with
    ``target`` and ``role``), a throughput test writes one row.
``outages``
    one row per incident: when connectivity was lost, how long for, and what
    was still answering. This is the part that answers "prove it happened".

Aggregates are computed in SQL rather than by loading every row into Python,
because a 2 second cadence over a few hours is already tens of thousands of
rows and the dashboard must stay snappy.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

from .config import INCIDENT_KINDS, SAMPLE_FIELDS

# Column name -> SQLite type, used for both creation and lightweight migration.
COLUMN_TYPES: dict[str, str] = {
    "ts": "REAL",
    "kind": "TEXT",
    "target": "TEXT",
    "role": "TEXT",
    "tier": "TEXT",
    "trigger": "TEXT",
    "ok": "INTEGER",
    "error": "TEXT",
    "probe_ms": "REAL",
    "refused": "INTEGER",
    "throttled": "INTEGER",
    "dns_ms": "REAL",
    "tcp_min_ms": "REAL",
    "tcp_avg_ms": "REAL",
    "tcp_max_ms": "REAL",
    "tcp_p95_ms": "REAL",
    "jitter_ms": "REAL",
    "loss_pct": "REAL",
    "sent": "INTEGER",
    "recv": "INTEGER",
    "http_ms": "REAL",
    "download_mbps": "REAL",
    "upload_mbps": "REAL",
    "download_bytes": "INTEGER",
    "upload_bytes": "INTEGER",
    "download_ttfb_ms": "REAL",
    "upload_ttfb_ms": "REAL",
    "download_intervals": "TEXT",
    "upload_intervals": "TEXT",
    "download_decay_pct": "REAL",
    "upload_decay_pct": "REAL",
    "capped": "INTEGER",
    "elapsed_ms": "REAL",
    "streams": "INTEGER",
    "round_id": "INTEGER",
    "source": "TEXT NOT NULL DEFAULT 'local'",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    target TEXT,
    role TEXT,
    tier TEXT,
    trigger TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    probe_ms REAL,
    refused INTEGER,
    dns_ms REAL,
    tcp_min_ms REAL,
    tcp_avg_ms REAL,
    tcp_max_ms REAL,
    tcp_p95_ms REAL,
    jitter_ms REAL,
    loss_pct REAL,
    sent INTEGER,
    recv INTEGER,
    http_ms REAL,
    download_mbps REAL,
    upload_mbps REAL,
    download_bytes INTEGER,
    upload_bytes INTEGER,
    download_ttfb_ms REAL,
    upload_ttfb_ms REAL,
    download_intervals TEXT,
    upload_intervals TEXT,
    download_decay_pct REAL,
    upload_decay_pct REAL,
    capped INTEGER,
    elapsed_ms REAL,
    streams INTEGER,
    round_id INTEGER,
    source TEXT NOT NULL DEFAULT 'local'
);

CREATE TABLE IF NOT EXISTS outages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    scope TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    duration_s REAL,
    rounds INTEGER NOT NULL DEFAULT 1,
    targets_total INTEGER,
    targets_failed INTEGER,
    failed_targets TEXT,
    detail TEXT,
    ongoing INTEGER NOT NULL DEFAULT 1,
    interrupted INTEGER NOT NULL DEFAULT 0,
    start_uncertainty_s REAL,
    end_uncertainty_s REAL
);

-- Hourly statistics for probes older than the raw window. One row per target per
-- hour: the sums are kept so that a range spanning both tiers can still be
-- averaged correctly (a plain average of averages would weight an hour with two
-- probes the same as an hour with 720).
CREATE TABLE IF NOT EXISTS rollups (
    hour INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    role TEXT NOT NULL,
    probes INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    fail INTEGER NOT NULL,
    sum_ms REAL,
    min_ms REAL,
    max_ms REAL,
    sum_jitter REAL,
    min_jitter REAL,
    max_jitter REAL,
    sum_loss REAL,
    max_loss REAL,
    PRIMARY KEY (hour, kind, target)
);

-- Hourly round counts, so uptime and the attribution breakdown survive the raw
-- window without re-deriving verdicts from probes that no longer exist.
CREATE TABLE IF NOT EXISTS round_rollups (
    hour INTEGER PRIMARY KEY,
    rounds INTEGER NOT NULL,
    down_rounds INTEGER NOT NULL,
    dns_rounds INTEGER NOT NULL DEFAULT 0,
    scope_isp INTEGER NOT NULL DEFAULT 0,
    scope_local INTEGER NOT NULL DEFAULT 0,
    scope_internet INTEGER NOT NULL DEFAULT 0
);

-- How far the raw tier has been folded into the tables above. Reads use it to
-- decide whether a range is served from probes or from hourly statistics.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Which device reports under which label. The label is a claim the agent makes;
-- the platform is what it actually was, so a link opened on the wrong device
-- shows up in the record instead of quietly filing the probes under a name that
-- describes something else.
CREATE TABLE IF NOT EXISTS agents (
    source TEXT PRIMARY KEY,
    token TEXT,
    platform TEXT,
    agent TEXT,
    first_seen REAL,
    last_seen REAL,
    probes INTEGER NOT NULL DEFAULT 0
);

-- Every probe that did not answer, kept verbatim however old it is: the whole
-- point of the tool is being able to show a drop, so failures are never
-- summarised away.
CREATE TABLE IF NOT EXISTS failures (
    ts REAL NOT NULL,
    target TEXT NOT NULL,
    role TEXT NOT NULL,
    error TEXT,
    round_id INTEGER,
    source TEXT NOT NULL DEFAULT 'local'
);

-- Where the traffic went, at the moment it stopped going anywhere. Also kept
-- forever, and for the same reason: this is the row that names equipment
-- instead of describing symptoms, and a hop list summarised away is worthless.
-- `trigger` says why it was taken (drop | baseline | manual), so a broken path
-- can be compared with the same path when it was healthy.
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    trigger TEXT NOT NULL,
    host TEXT NOT NULL,
    tracer TEXT NOT NULL,
    reached INTEGER NOT NULL DEFAULT 0,
    hops INTEGER NOT NULL DEFAULT 0,
    answered INTEGER NOT NULL DEFAULT 0,
    max_hops INTEGER NOT NULL DEFAULT 0,
    last_hop TEXT,
    duration_ms REAL,
    error TEXT,
    incident_id INTEGER,
    hop_list TEXT NOT NULL DEFAULT '[]'
);
"""

# Created *after* the column migration, so that upgrading a database written by
# an older version does not fail on an index over a column that does not exist
# yet.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_samples_kind_ts ON samples (kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_samples_target_ts ON samples (target, ts DESC);
CREATE INDEX IF NOT EXISTS idx_samples_round ON samples (round_id);
-- Round counting walks every latency probe and reads only (round, role, ok,
-- ts): with these in the index it never touches the table itself, which on a
-- seven-day window is the difference between 0.6s and 0.2s.
CREATE INDEX IF NOT EXISTS idx_samples_kind_round ON samples (kind, round_id, role, ok, ts);
CREATE INDEX IF NOT EXISTS idx_samples_source_ts ON samples (source, ts DESC);
CREATE INDEX IF NOT EXISTS idx_outages_started ON outages (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_outages_open ON outages (kind, ongoing);
CREATE INDEX IF NOT EXISTS idx_rollups_kind_hour ON rollups (kind, hour);
CREATE INDEX IF NOT EXISTS idx_rollups_target_hour ON rollups (target, hour);
CREATE INDEX IF NOT EXISTS idx_failures_ts ON failures (ts DESC);
CREATE INDEX IF NOT EXISTS idx_failures_target_ts ON failures (target, ts DESC);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces (ts DESC);
CREATE INDEX IF NOT EXISTS idx_traces_trigger_ts ON traces (trigger, ts DESC);
"""

# Round classification in SQL, shared by the hourly fold and the statistics
# reader so both judge a round by exactly the same rules as the live sampler: a
# round is down when no internet target answered, and the scope comes from
# whether the router answered.
_ROUNDS_PER_ROUND = """
    SELECT
        CAST(ts / 3600 AS INTEGER) * 3600 AS hour,
        round_id,
        SUM(CASE WHEN role = 'internet' AND ok = 1 THEN 1 ELSE 0 END) AS internet_ok,
        SUM(CASE WHEN role = 'lan' THEN 1 ELSE 0 END) AS lan_total,
        SUM(CASE WHEN role = 'lan' AND ok = 1 THEN 1 ELSE 0 END) AS lan_ok,
        SUM(CASE WHEN role = 'dns' THEN 1 ELSE 0 END) AS dns_total,
        SUM(CASE WHEN role = 'dns' AND ok = 1 THEN 1 ELSE 0 END) AS dns_ok
    FROM samples
    WHERE kind = 'latency' AND round_id IS NOT NULL {bounds}
    GROUP BY round_id
"""

_ROUNDS_CLASSIFIED = """
    SELECT
        hour, round_id,
        CASE WHEN internet_ok > 0 THEN 1 ELSE 0 END AS internet_up,
        CASE WHEN internet_ok > 0 AND dns_total > 0 AND dns_ok = 0 THEN 1 ELSE 0 END AS dns_down,
        CASE
            WHEN internet_ok > 0 THEN NULL
            WHEN lan_total > 0 AND lan_ok = 0 THEN 'local'
            WHEN lan_ok > 0 THEN 'isp'
            ELSE 'internet'
        END AS scope
    FROM (""" + _ROUNDS_PER_ROUND + """)
"""

_ROUNDS_HOURLY = """
    SELECT
        hour,
        COUNT(*) AS rounds,
        SUM(1 - internet_up) AS down_rounds,
        SUM(dns_down) AS dns_rounds,
        SUM(CASE WHEN scope = 'isp' THEN 1 ELSE 0 END) AS scope_isp,
        SUM(CASE WHEN scope = 'local' THEN 1 ELSE 0 END) AS scope_local,
        SUM(CASE WHEN scope = 'internet' THEN 1 ELSE 0 END) AS scope_internet
    FROM (""" + _ROUNDS_CLASSIFIED + """)
    GROUP BY hour
"""

_HAS_WINDOW_FUNCTIONS = sqlite3.sqlite_version_info >= (3, 25)

# Metrics the dashboard aggregates, and the sample kind they live in.
LATENCY_METRICS = ("probe_ms", "jitter_ms", "loss_pct")
SPEED_METRICS = ("download_mbps", "upload_mbps")


def _adapt(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    return value


def _ts_bounds(since: float | None, until: float | None) -> tuple[str, list[float]]:
    """``(" AND ts >= ? AND ts <= ?", params)``, or ``("", [])`` when unbounded.

    Returns a fragment rather than a clause list so a window with no bounds
    cannot leave a dangling AND in the SQL. Note ``since=0.0`` means "no lower
    bound", which several callers rely on.
    """
    clauses: list[str] = []
    params: list[float] = []
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if until:
        clauses.append("ts <= ?")
        params.append(until)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _hour_bounds(since: float | None, until: float | None) -> tuple[str, list[float]]:
    """Same idea as :func:`_ts_bounds` for the rollup tables, which key on `hour`."""
    clauses: list[str] = []
    params: list[float] = []
    if since:
        # An hour bucket is stamped at its start, so the bucket containing
        # `since` is included rather than cut off.
        clauses.append("hour >= ?")
        params.append(math.floor(since / 3600.0) * 3600.0)
    if until:
        clauses.append("hour <= ?")
        params.append(until)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _percentile(ordered: Sequence[float], pct: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    return float(ordered[low] * (1 - (rank - low)) + ordered[high] * (rank - low))


# Charts only need these columns. Selecting the full row (28 mostly-NULL
# columns) made /api/series ~380 KB per poll and growing, which the open
# dashboard re-fetches every few seconds.
_SERIES_COLUMNS: dict[str, tuple[str, ...]] = {
    "latency": ("ts", "probe_ms", "ok"),
    # `tier` is required here: the chart draws burst and sustained as separate
    # series, and without it every point would be labelled sustained.
    "speed": ("ts", "tier", "trigger", "download_mbps", "upload_mbps", "ok"),
}


class Store:
    """Thin async wrapper around the SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        # Keep the WAL small (~1 MB) rather than letting it reach the default
        # ~4 MB before SQLite bothers to checkpoint.
        await db.execute("PRAGMA wal_autocheckpoint=256")
        # GROUP BY over a few days of probes spills to disk by default, which
        # fails with "unable to open database file" wherever the temp directory
        # is not writable (containers, sandboxes, a read-only /tmp). The
        # intermediate results here are tiny -- the rollups keep raw probes to a
        # week -- so holding them in memory costs nothing and removes the
        # dependency entirely.
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.executescript(SCHEMA)
        await db.commit()
        await self._migrate(db)
        await db.executescript(INDEXES)
        await db.commit()
        self._db = db

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        """Add columns introduced after a database was first created."""
        cursor = await db.execute("PRAGMA table_info(samples)")
        existing = {row[1] for row in await cursor.fetchall()}
        added = False
        for column in SAMPLE_FIELDS:
            if column not in existing:
                await db.execute(f"ALTER TABLE samples ADD COLUMN {column} {COLUMN_TYPES[column]}")
                added = True

        cursor = await db.execute("PRAGMA table_info(agents)")
        existing = {row[1] for row in await cursor.fetchall()}
        if existing and "token" not in existing:
            await db.execute("ALTER TABLE agents ADD COLUMN token TEXT")
            added = True

        cursor = await db.execute("PRAGMA table_info(outages)")
        existing = {row[1] for row in await cursor.fetchall()}
        for column in ("start_uncertainty_s", "end_uncertainty_s"):
            if column not in existing:
                await db.execute(f"ALTER TABLE outages ADD COLUMN {column} REAL")
                added = True
        # Before the two-tier split every speed sample was a duration-based
        # test, so attribute the existing rows to the sustained tier rather than
        # leaving them out of every tier's average. Idempotent: the WHERE clause
        # stops matching once they are labelled.
        await db.execute(
            "UPDATE samples SET tier = 'sustained' WHERE kind = 'speed' AND tier IS NULL"
            " AND download_intervals IS NOT NULL"
        )
        await db.execute(
            "UPDATE samples SET tier = 'quick' WHERE kind = 'speed' AND tier IS NULL"
            " AND download_intervals IS NULL"
        )
        if added:
            await db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.connect() must be awaited first")
        return self._db

    # ---------------------------------------------------------------- writes
    async def add(self, sample: dict) -> dict:
        """Insert one sample; unknown keys are ignored."""
        row = {k: _adapt(sample[k]) for k in SAMPLE_FIELDS if k in sample}
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        async with self._lock:
            cursor = await self.db.execute(
                f"INSERT INTO samples ({columns}) VALUES ({placeholders})",
                list(row.values()),
            )
            await self.db.commit()
            sample_id = cursor.lastrowid
        return {"id": sample_id, **sample}

    async def add_many(self, samples: Sequence[dict]) -> int:
        """Insert a whole round in one transaction."""
        if not samples:
            return 0
        rows = [{k: _adapt(s[k]) for k in SAMPLE_FIELDS if k in s} for s in samples]
        columns = sorted({key for row in rows for key in row})
        statement = (
            f"INSERT INTO samples ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        payload = [[row.get(column) for column in columns] for row in rows]
        async with self._lock:
            await self.db.executemany(statement, payload)
            await self.db.commit()
        return len(rows)

    async def clear(self, kind: str | None = None, incidents: bool = True) -> int:
        async with self._lock:
            if kind:
                cursor = await self.db.execute("DELETE FROM samples WHERE kind = ?", (kind,))
            else:
                cursor = await self.db.execute("DELETE FROM samples")
            if incidents and kind is None:
                await self.db.execute("DELETE FROM outages")
                # A trace without its incident is a hop list nobody can place,
                # so a full reset takes these too -- and only a full reset: they
                # are never pruned by age.
                await self.db.execute("DELETE FROM traces")
            await self.db.commit()
            return cursor.rowcount or 0

    async def prune(self, retention_days: float) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        async with self._lock:
            cursor = await self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            await self.db.execute("DELETE FROM outages WHERE started_at < ?", (cutoff,))
            await self.db.commit()
            return cursor.rowcount or 0

    # ------------------------------------------------------------- rollups
    async def rollout(self, raw_window_hours: float) -> dict:
        """Fold probes older than the raw window into hourly statistics.

        Runs before pruning, so a probe is only ever deleted once its hour has
        been summarised. Only *complete* hours are folded: a partial hour would
        be summarised from a fraction of its probes, and the next pass would
        overwrite it with a different fraction.

        What survives: hourly per-target statistics, hourly round counts (uptime
        and attribution), and every failed probe verbatim. Speed tests are left
        alone -- a couple of hundred rows a day is nothing next to ~86,000 probe
        rows, and their per-second shape is the interesting part.
        """
        empty = {"rolled": 0, "hours": 0, "failures": 0, "trimmed": 0}
        if raw_window_hours <= 0:
            return empty
        hour = 3600.0
        cutoff = math.floor((time.time() - raw_window_hours * hour) / hour) * hour
        if cutoff <= 0:
            return empty

        async with self._lock:
            await self.db.execute(
                """
                INSERT OR REPLACE INTO rollups
                    (hour, kind, target, role, probes, ok, fail, sum_ms, min_ms,
                     max_ms, sum_jitter, min_jitter, max_jitter, sum_loss, max_loss)
                SELECT
                    CAST(ts / 3600 AS INTEGER) * 3600,
                    kind, target, role,
                    COUNT(*),
                    SUM(ok),
                    SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN ok = 1 THEN probe_ms ELSE 0 END),
                    MIN(CASE WHEN ok = 1 THEN probe_ms END),
                    MAX(CASE WHEN ok = 1 THEN probe_ms END),
                    SUM(COALESCE(jitter_ms, 0)),
                    MIN(jitter_ms),
                    MAX(jitter_ms),
                    SUM(COALESCE(loss_pct, 0)),
                    MAX(COALESCE(loss_pct, 0))
                FROM samples
                WHERE kind = 'latency' AND source = 'local' AND ts < ?
                GROUP BY CAST(ts / 3600 AS INTEGER), kind, target, role
                """,
                (cutoff,),
            )
            # Round counts, judged with the same rules the live sampler uses:
            # a round is down when no internet target answered, and the scope
            # comes from whether the router answered.
            await self.db.execute(
                "INSERT OR REPLACE INTO round_rollups"
                " (hour, rounds, down_rounds, dns_rounds, scope_isp, scope_local,"
                " scope_internet) "
                + _ROUNDS_HOURLY.replace("{bounds}", "AND ts < ?"),
                (cutoff,),
            )
            # Failures are never summarised away.
            failures = await self.db.execute(
                """
                INSERT INTO failures (ts, target, role, error, round_id)
                SELECT ts, target, role, error, round_id
                FROM samples
                WHERE kind = 'latency' AND ok = 0 AND ts < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM failures f
                      WHERE f.ts = samples.ts AND f.target = samples.target
                  )
                """,
                (cutoff,),
            )
            # Keep old speed tests' numbers, drop their per-second shape.
            trimmed = await self.db.execute(
                """
                UPDATE samples
                SET download_intervals = NULL, upload_intervals = NULL
                WHERE kind = 'speed' AND ts < ?
                  AND (download_intervals IS NOT NULL OR upload_intervals IS NOT NULL)
                """,
                (cutoff,),
            )
            cursor = await self.db.execute(
                "DELETE FROM samples WHERE kind = 'latency' AND ts < ?", (cutoff,)
            )
            await self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('raw_cutoff', ?)",
                (str(cutoff),),
            )
            await self.db.commit()
            hours = await self.db.execute(
                "SELECT COUNT(*) FROM round_rollups WHERE hour < ?", (cutoff,)
            )
            counted = await hours.fetchone()
            return {
                "rolled": cursor.rowcount or 0,
                "hours": counted[0] if counted else 0,
                "failures": failures.rowcount or 0,
                "trimmed": trimmed.rowcount or 0,
            }

    async def raw_cutoff(self) -> float:
        """Oldest timestamp still held as individual probes (0 = nothing rolled)."""
        cursor = await self.db.execute("SELECT value FROM meta WHERE key = 'raw_cutoff'")
        row = await cursor.fetchone()
        try:
            return float(row[0]) if row else 0.0
        except (TypeError, ValueError):
            return 0.0

    async def meta(self, key: str, value: str | None = None) -> str | None:
        """Read a small setting from the database, optionally writing it first."""
        if value is not None:
            async with self._lock:
                await self.db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
                )
                await self.db.commit()
            return value
        cursor = await self.db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def add_agent(self, source: str, token: str) -> dict:
        """Register a device, or rotate its token if it is already known."""
        now = time.time()
        async with self._lock:
            await self.db.execute(
                "INSERT INTO agents (source, token, first_seen, last_seen, probes)"
                " VALUES (?, ?, ?, ?, 0)"
                " ON CONFLICT(source) DO UPDATE SET token = excluded.token",
                (source, token, now, now),
            )
            await self.db.commit()
        return {"source": source, "token": token, "first_seen": now, "last_seen": now, "probes": 0}

    async def drop_agent(self, source: str) -> bool:
        """Revoke a device: its token stops working immediately."""
        async with self._lock:
            cursor = await self.db.execute("DELETE FROM agents WHERE source = ?", (source,))
            await self.db.commit()
        return bool(cursor.rowcount)

    async def agent_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        cursor = await self.db.execute("SELECT * FROM agents WHERE token = ?", (token,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def record_agent(
        self, source: str, platform: str = "", agent: str = "", probes: int = 0
    ) -> None:
        """Note what is reporting under this label, and when it last did."""
        now = time.time()
        async with self._lock:
            await self.db.execute(
                "INSERT INTO agents (source, platform, agent, first_seen, last_seen, probes)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(source) DO UPDATE SET"
                "   platform = excluded.platform,"
                "   agent = excluded.agent,"
                "   last_seen = excluded.last_seen,"
                "   probes = agents.probes + excluded.probes",
                (source, platform[:40], agent[:16], now, now, probes),
            )
            await self.db.commit()

    async def agents(self) -> dict[str, dict]:
        cursor = await self.db.execute("SELECT * FROM agents")
        return {row["source"]: dict(row) for row in await cursor.fetchall()}

    async def last_seen(self, within: float = 3600.0) -> dict[str, float]:
        """When each vantage point last reported, for the live on/off indicator.

        Bounded to the recent past on purpose: the indicator only asks whether a
        device reported in the last minute, so there is no reason to walk the
        whole table -- and a device quiet for longer than the bound is meant to
        be missing from the answer, which reads as off.
        """
        cutoff = time.time() - max(60.0, float(within))
        cursor = await self.db.execute(
            "SELECT source, MAX(ts) last_ts FROM samples"
            " WHERE kind = 'latency' AND ts >= ? GROUP BY source",
            (cutoff,),
        )
        return {row["source"]: float(row["last_ts"]) for row in await cursor.fetchall()}

    async def source_hours(self, since: float, until: float | None = None) -> list[dict]:
        """Per device, per hour: how much it reported and how much it lost.

        One pass answers both questions the statistics page asks about vantage
        points: the hourly strip (``probes``/``failed``, internet role only, the
        same numbers the round verdicts use) and the per-device totals in the
        table above it (``all_probes``/``all_failed``, every role, plus the first
        and last timestamp). Splitting those into two queries cost a second full
        scan of the table, which is the last thing a page that reads seven days
        of probes can afford.
        """
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            "SELECT source, CAST(ts / 3600 AS INTEGER) * 3600 AS hour,"
            " COUNT(*) all_probes, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) all_failed,"
            " SUM(CASE WHEN role = 'internet' THEN 1 ELSE 0 END) probes,"
            " SUM(CASE WHEN role = 'internet' AND ok = 0 THEN 1 ELSE 0 END) failed,"
            " MIN(ts) first_ts, MAX(ts) last_ts"
            " FROM samples WHERE kind = 'latency'" + bounds +
            " GROUP BY source, hour ORDER BY hour ASC",
            tuple(params),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    @staticmethod
    def fold_sources(hours: Sequence[dict]) -> list[dict]:
        """Per-device totals, folded from the hourly rows above.

        Same numbers the separate query produced, without the second scan.
        """
        folded: dict[str, dict] = {}
        for row in hours:
            source = row.get("source") or ""
            entry = folded.setdefault(source, {
                "source": source, "probes": 0, "failed": 0, "internet_probes": 0,
                "internet_failed": 0, "first_ts": None, "last_ts": None,
            })
            entry["probes"] += int(row.get("all_probes") or 0)
            entry["failed"] += int(row.get("all_failed") or 0)
            entry["internet_probes"] += int(row.get("probes") or 0)
            entry["internet_failed"] += int(row.get("failed") or 0)
            first, last = row.get("first_ts"), row.get("last_ts")
            if first is not None:
                entry["first_ts"] = first if entry["first_ts"] is None else min(entry["first_ts"], first)
            if last is not None:
                entry["last_ts"] = last if entry["last_ts"] is None else max(entry["last_ts"], last)
        return [folded[key] for key in sorted(folded)]

    async def failures(
        self, since: float = 0.0, until: float | None = None, limit: int = 500
    ) -> list[dict]:
        """Every failed probe in the range, newest first.

        Reads both tiers: a failure younger than the raw window is still a row in
        ``samples``, an older one was copied to ``failures`` when its hour was
        folded. Asking only one of them would quietly hide half the drops.
        """
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            "SELECT ts, target, role, error, round_id FROM ("
            "  SELECT ts, target, role, error, round_id FROM failures WHERE 1 = 1" + bounds +
            "  UNION ALL"
            "  SELECT ts, target, role, error, round_id FROM samples"
            "  WHERE kind = 'latency' AND ok = 0" + bounds +
            ") ORDER BY ts DESC LIMIT ?",
            (*params, *params, limit),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    # ------------------------------------------------------------------ loss
    async def bursts(
        self, since: float = 0.0, until: float | None = None, limit: int = 500
    ) -> list[dict]:
        """Every counted handshake burst in the range, newest first.

        Stored as its own sample kind, so it never mixes into the round probes'
        latency, jitter or failure counts -- a burst is a measurement *of* a
        drop, not another probe that happened to fail.
        """
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            "SELECT ts, target, role, sent, recv, loss_pct, probe_ms, tcp_min_ms,"
            " tcp_avg_ms, tcp_max_ms, refused, error FROM samples"
            " WHERE kind = 'burst'" + bounds + " ORDER BY ts DESC LIMIT ?",
            (*params, limit),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    # --------------------------------------------------------------- tracing
    async def add_trace(self, trace: dict, incident_id: int | None = None) -> int:
        """Store one hop list. Never folded, never pruned on age."""
        cursor = await self.db.execute(
            "INSERT INTO traces (ts, trigger, host, tracer, reached, hops, answered,"
            " max_hops, last_hop, duration_ms, error, incident_id, hop_list)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                float(trace.get("ts") or time.time()),
                str(trace.get("trigger") or "manual")[:16],
                str(trace.get("host") or "")[:64],
                str(trace.get("tracer") or "none")[:16],
                1 if trace.get("reached") else 0,
                int(trace.get("hops") or 0),
                int(trace.get("answered") or 0),
                int(trace.get("max_hops") or 0),
                (str(trace["last_hop"])[:64] if trace.get("last_hop") else None),
                float(trace["duration_ms"]) if trace.get("duration_ms") is not None else None,
                trace.get("error"),
                incident_id,
                json.dumps(trace.get("hop_list") or []),
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid or 0)

    async def traces(
        self,
        since: float = 0.0,
        until: float | None = None,
        trigger: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Traces in the range, newest first, hop lists included."""
        bounds, params = _ts_bounds(since, until)
        sql = "SELECT * FROM traces WHERE 1 = 1" + bounds
        if trigger:
            sql += " AND trigger = ?"
            params.append(trigger)
        sql += " ORDER BY ts DESC LIMIT ?"
        cursor = await self.db.execute(sql, (*params, limit))
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def latest_trace(self, trigger: str | None = None) -> dict | None:
        rows = await self.traces(trigger=trigger, limit=1)
        return rows[0] if rows else None

    async def count_traces(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) FROM traces")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------ statistics
    async def hourly_latency(self, since: float, until: float | None = None) -> list[dict]:
        """Per-target, per-hour probe statistics across both storage tiers.

        Reads raw probes for the part of the range still on disk and the rollups
        for the older part, then re-sums by hour, so a range spanning the raw
        window is neither double counted nor blind to half of itself.
        """
        raw_bounds, raw_params = _ts_bounds(since, until)
        roll_bounds, roll_params = _hour_bounds(since, until)
        cursor = await self.db.execute(
            # `ok` is aliased: _row_to_dict coerces a column of that name to a
            # bool, and these are counts.
            "SELECT hour, target, role, SUM(probes) probes, SUM(ok_probes) ok_probes,"
            " SUM(fail) fail,"
            " SUM(sum_ms) sum_ms, MIN(min_ms) min_ms, MAX(max_ms) max_ms,"
            " SUM(sum_jitter) sum_jitter, SUM(sum_loss) sum_loss"
            " FROM ("
            "   SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS hour, target, role,"
            "          COUNT(*) probes, SUM(ok) ok_probes,"
            "          SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) fail,"
            "          SUM(CASE WHEN ok = 1 THEN probe_ms ELSE 0 END) sum_ms,"
            "          MIN(CASE WHEN ok = 1 THEN probe_ms END) min_ms,"
            "          MAX(CASE WHEN ok = 1 THEN probe_ms END) max_ms,"
            "          SUM(COALESCE(jitter_ms, 0)) sum_jitter,"
            "          SUM(COALESCE(loss_pct, 0)) sum_loss"
            "   FROM samples WHERE kind = 'latency' AND source = 'local'" + raw_bounds +
            "   GROUP BY hour, target, role"
            "   UNION ALL"
            "   SELECT hour, target, role, probes, ok_probes, fail, sum_ms, min_ms, max_ms,"
            "          sum_jitter, sum_loss"
            "   FROM (SELECT hour, target, role, probes, ok AS ok_probes, fail, sum_ms,"
            "                min_ms, max_ms, sum_jitter, sum_loss FROM rollups"
            "         WHERE kind = 'latency'" + roll_bounds + ")"
            " ) GROUP BY hour, target, role ORDER BY hour ASC",
            (*raw_params, *roll_params),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def hourly_rounds(self, since: float, until: float | None = None) -> list[dict]:
        """Per-hour round counts and attribution, across both tiers."""
        raw_bounds, raw_params = _ts_bounds(since, until)
        roll_bounds, roll_params = _hour_bounds(since, until)
        cursor = await self.db.execute(
            "SELECT hour, SUM(rounds) rounds, SUM(down_rounds) down_rounds,"
            " SUM(dns_rounds) dns_rounds, SUM(scope_isp) scope_isp,"
            " SUM(scope_local) scope_local, SUM(scope_internet) scope_internet"
            " FROM ("
            + _ROUNDS_HOURLY.replace("{bounds}", raw_bounds)
            + "   UNION ALL"
            "   SELECT hour, rounds, down_rounds, dns_rounds, scope_isp, scope_local,"
            "          scope_internet FROM round_rollups WHERE 1 = 1" + roll_bounds +
            " ) GROUP BY hour ORDER BY hour ASC",
            (*raw_params, *roll_params),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    def db_bytes(self) -> int:
        """Size of the database file, for the coverage panel."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    async def stats(self, since: float, until: float) -> dict:
        """Everything the statistics page shows, in one payload.

        Aggregated here rather than in a dozen endpoints: the hourly rows are few
        even for a year (24 x 365 x targets), so the daily and per-target views
        are cheap to derive in Python and far easier to test than window SQL.
        """
        hours = await self.hourly_latency(since, until)
        rounds = await self.hourly_rounds(since, until)
        # One query for both per-device views: the hourly strip and the totals
        # above it. They used to be two scans of the same rows.
        device_hours = await self.source_hours(since, until)
        return {
            "hours": hours,
            "rounds": rounds,
            "incidents": await self.incidents(since, limit=5000, until=until),
            "speed": await self._speed_rows(since, until),
            "failures": await self._failure_stats(since, until),
            "bursts": await self.bursts(since, until, limit=500),
            "sources": self.fold_sources(device_hours),
            "agents": await self.agents(),
            "source_hours": device_hours,
            # Only the newest few: the panel compares the last drop with the
            # last healthy path, and hop lists are the biggest rows here.
            "traces": await self.traces(since, until, limit=12),
            "coverage": {
                "first_ts": await self.first_ts(),
                "last_ts": await self.latest_sample_ts("latency"),
                "raw_cutoff": await self.raw_cutoff(),
                "samples": await self.count(),
                "rolled_hours": len(rounds),
                "db_bytes": self.db_bytes(),
            },
        }

    async def _speed_rows(self, since: float, until: float) -> list[dict]:
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            "SELECT ts, tier, trigger, download_mbps, upload_mbps,"
            " COALESCE(download_bytes, 0) download_bytes,"
            " COALESCE(upload_bytes, 0) upload_bytes"
            " FROM samples WHERE kind = 'speed' AND ok = 1" + bounds + " ORDER BY ts ASC",
            tuple(params),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def _failure_stats(self, since: float, until: float) -> list[dict]:
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            "SELECT ts, target, role, COALESCE(error, '(no error)') error FROM ("
            "  SELECT ts, target, role, error FROM failures WHERE 1 = 1" + bounds +
            "  UNION ALL"
            "  SELECT ts, target, role, error FROM samples"
            "  WHERE kind = 'latency' AND ok = 0" + bounds +
            ") ORDER BY ts ASC",
            (*params, *params),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def checkpoint(self, mode: str = "PASSIVE") -> None:
        """Fold the write-ahead log back into the database file.

        Without this the WAL sits at its auto-checkpoint threshold (megabytes of
        mostly-stale pages) on a long run, because a busy reader can starve the
        passive checkpoint SQLite performs on commit.
        """
        async with self._lock:
            await self.db.execute(f"PRAGMA wal_checkpoint({mode})")

    # ---------------------------------------------------------------- reads
    async def count(
        self, kind: str | None = None, since: float | None = None,
        until: float | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM samples"
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        cursor = await self.db.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def latest_sample_ts(self, kind: str, tier: str | None = None) -> float | None:
        """When the newest sample of a kind (optionally a single tier) landed."""
        sql = "SELECT MAX(ts) FROM samples WHERE kind = ?"
        params: list[Any] = [kind]
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        cursor = await self.db.execute(sql, params)
        row = await cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def latest_sample(self, kind: str, tier: str | None = None) -> dict | None:
        """The newest sample of a kind, as a plain dict.

        Used to put the last throughput test back on the dashboard after a
        restart, rather than leaving the per-second panel empty until the next
        one comes round an hour later.
        """
        sql = "SELECT * FROM samples WHERE kind = ?"
        params: list[Any] = [kind]
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        sql += " ORDER BY ts DESC LIMIT 1"
        cursor = await self.db.execute(sql, params)
        row = await cursor.fetchone()
        # Same decoding the API uses: the interval series are JSON text in the
        # table and arrays on the wire.
        return _row_to_dict(row) or None

    async def first_ts(self) -> float | None:
        cursor = await self.db.execute("SELECT MIN(ts) FROM samples")
        row = await cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def recent(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM samples"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        cursor = await self.db.execute(sql, params)
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def recent_rounds(self, limit: int = 4) -> list[dict]:
        """The most recent rounds with every target's sample, newest first."""
        cursor = await self.db.execute(
            "SELECT round_id FROM samples WHERE kind = 'latency' "
            "GROUP BY round_id ORDER BY MAX(ts) DESC LIMIT ?",
            (int(limit),),
        )
        round_ids = [row[0] for row in await cursor.fetchall() if row[0] is not None]
        rounds: list[dict] = []
        for round_id in round_ids:
            cursor = await self.db.execute(
                "SELECT * FROM samples WHERE round_id = ? ORDER BY target ASC", (round_id,)
            )
            rounds.append({"round_id": round_id, "samples": [_row_to_dict(r) for r in await cursor.fetchall()]})
        return rounds

    async def target_names(self, kind: str, since: float, until: float | None = None) -> list[str]:
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            f"SELECT DISTINCT target FROM samples WHERE kind = ? AND target IS NOT NULL{bounds}"
            " ORDER BY target ASC",
            (kind, *params),
        )
        return [row[0] for row in await cursor.fetchall()]

    async def series(
        self, kind: str, since: float, max_points: int = 720, until: float | None = None
    ) -> dict:
        """Per-target series, each strided down to at most ``max_points``.

        A range that starts before the raw window is served as hourly averages
        from the rollups -- uniformly, rather than mixing one resolution with
        another, so the x axis means one thing. ``resolution`` says which it is.
        """
        if kind == "latency":
            cutoff = await self.raw_cutoff()
            if cutoff and since < cutoff:
                return await self._series_hourly(since, max_points, until)
        targets = await self.target_names(kind, since, until)
        out: dict[str, list[dict]] = {}
        total = 0
        step = 1
        for target in targets:
            points, count, stride = await self._series_for(
                kind, since, target, max_points, until
            )
            out[target] = points
            total += count
            step = max(step, stride)
        return {"kind": kind, "total": total, "step": step, "targets": targets, "series": out}

    async def _series_hourly(
        self, since: float, max_points: int = 720, until: float | None = None
    ) -> dict:
        """One point per target per hour: the average, with the range alongside."""
        bounds, params = _hour_bounds(since, until)
        cursor = await self.db.execute(
            # `ok` is aliased: _row_to_dict coerces a column of that name to a
            # bool, and this one is a count.
            "SELECT hour, target, role, probes, ok AS ok_n, fail, sum_ms, min_ms, max_ms "
            "FROM rollups WHERE kind = 'latency'" + bounds + " ORDER BY hour ASC",
            tuple(params),
        )
        rows = [_row_to_dict(r) for r in await cursor.fetchall()]
        out: dict[str, list[dict]] = {}
        total = 0
        for row in rows:
            total += row["probes"]
            avg = (row["sum_ms"] / row["ok_n"]) if row["ok_n"] else None
            out.setdefault(row["target"], []).append({
                "ts": row["hour"],
                "hourly": True,
                "ok": 1 if row["ok_n"] else 0,
                "probe_ms": round(avg, 3) if avg is not None else None,
                "min_ms": row["min_ms"],
                "max_ms": row["max_ms"],
                "probes": row["probes"],
                "failed": row["fail"],
            })
        step = 1
        if max_points > 0:
            longest = max((len(v) for v in out.values()), default=0)
            if longest > max_points:
                step = (longest + max_points - 1) // max_points
                out = {k: v[::step] for k, v in out.items()}
        return {
            "kind": "latency",
            "resolution": "hourly",
            "total": total,
            "step": step,
            "targets": sorted(out),
            "series": self._with_missing_targets(out, rows),
        }

    @staticmethod
    def _with_missing_targets(out: dict, rows: list[dict]) -> dict:
        for row in rows:
            out.setdefault(row["target"], [])
        return out

    async def _series_for(
        self, kind: str, since: float, target: str, max_points: int,
        until: float | None = None,
    ) -> tuple[list[dict], int, int]:
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            f"SELECT COUNT(*) FROM samples WHERE kind = ? AND target = ?{bounds}",
            (kind, target, *params),
        )
        row = await cursor.fetchone()
        count = int(row[0]) if row else 0
        if count == 0:
            return [], 0, 1
        if max_points <= 0 or count <= max_points:
            step = 1
        else:
            step = (count + max_points - 1) // max_points

        columns = ", ".join(_SERIES_COLUMNS.get(kind, ("ts", "ok")))

        if step == 1 or not _HAS_WINDOW_FUNCTIONS:
            cursor = await self.db.execute(
                f"SELECT {columns} FROM samples WHERE kind = ? AND target = ?{bounds} "
                "ORDER BY ts ASC, id ASC",
                (kind, target, *params),
            )
            rows = [_row_to_dict(r) for r in await cursor.fetchall()]
            if step > 1:  # pragma: no cover - only on very old SQLite
                rows = rows[::step]
            return rows, count, step

        cursor = await self.db.execute(
            f"SELECT {columns} FROM ("
            f"  SELECT {columns}, ROW_NUMBER() OVER (ORDER BY ts ASC, id ASC) AS rn"
            f"  FROM samples WHERE kind = ? AND target = ?{bounds}"
            ") WHERE ((rn - 1) % ?) = 0 OR rn = ? ORDER BY rn ASC",
            (kind, target, *params, step, count),
        )
        rows = [_row_to_dict(r) for r in await cursor.fetchall()]
        return rows, count, step

    async def round_states(
        self, since: float, max_points: int = 600, until: float | None = None
    ) -> dict:
        """One entry per round, for the connection timeline strip.

        Downsampling never drops a failed round: an outage that gets strided
        away would defeat the entire point of the tool.
        """
        cursor = await self.db.execute(
            "SELECT round_id, MAX(ts) AS ts,"
            " MAX(CASE WHEN role = 'internet' THEN ok ELSE 0 END) AS up,"
            " COUNT(*) AS probes,"
            " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_n,"
            " GROUP_CONCAT(CASE WHEN ok = 0 THEN target END) AS failed,"
            " AVG(CASE WHEN role = 'internet' AND ok = 1 THEN probe_ms END) AS avg_ms"
            " FROM samples WHERE kind = 'latency' AND source = 'local'"
            " AND round_id IS NOT NULL"
            + _ts_bounds(since, until)[0]
            + " GROUP BY round_id ORDER BY ts ASC",
            tuple(_ts_bounds(since, until)[1]),
        )
        rows = [_row_to_dict(r) for r in await cursor.fetchall()]
        resolution = "raw"
        cutoff = await self.raw_cutoff()
        if cutoff and since < cutoff:
            # Older rounds are gone; the hour is the finest block left. One block
            # per hour, red when any round in it failed, so an outage that
            # predates the raw window is still visible on the strip.
            resolution = "hourly"
            roll_bounds, roll_params = _hour_bounds(since, min(until, cutoff) if until else cutoff)
            cursor = await self.db.execute(
                "SELECT hour, rounds, down_rounds, dns_rounds FROM round_rollups "
                "WHERE 1 = 1" + roll_bounds + " ORDER BY hour ASC",
                tuple(roll_params),
            )
            older = [
                {
                    "round_id": -row["hour"],
                    "ts": row["hour"],
                    "hourly": True,
                    "up": 0 if row["down_rounds"] else 1,
                    "probes": row["rounds"],
                    "failed_n": row["down_rounds"],
                    "failed": "",
                    "avg_ms": None,
                    "dns_failed": row["dns_rounds"],
                }
                for row in (await cursor.fetchall())
            ]
            rows = older + rows
        total = len(rows)
        step = 1
        if max_points > 0 and total > max_points:
            step = (total + max_points - 1) // max_points
            keep = {r["round_id"]: r for r in rows[::step]}
            for row in rows:  # outages are always retained
                if not row["up"]:
                    keep[row["round_id"]] = row
            rows = [keep[key] for key in sorted(keep)]
        return {"total": total, "step": step, "resolution": resolution, "rounds": rows}

    # ------------------------------------------------------------ aggregates
    async def _rollup_column(self, column: str, since: float, until: float | None,
                             role: str | None = None, target: str | None = None) -> dict | None:
        """Summed hourly statistics for one column, or None when nothing rolled."""
        spec = {
            "probe_ms":   ("ok",     "sum_ms",     "min_ms",     "max_ms"),
            "jitter_ms":  ("probes", "sum_jitter", "min_jitter", "max_jitter"),
            "loss_pct":   ("probes", "sum_loss",   None,         "max_loss"),
        }.get(column)
        if not spec or since <= 0:
            return None
        weight, total, lo, hi = spec
        clauses = ["kind = 'latency'"]
        params: list[Any] = []
        if role:
            clauses.append("role = ?")
            params.append(role)
        if target:
            clauses.append("target = ?")
            params.append(target)
        bounds, bound_params = _hour_bounds(since, until)
        cursor = await self.db.execute(
            f"SELECT SUM({weight}), SUM({total}), "
            f"{'MIN(' + lo + ')' if lo else 'NULL'}, "
            f"{'MAX(' + hi + ')' if hi else 'NULL'} "
            f"FROM rollups WHERE {' AND '.join(clauses)}{bounds}",
            (*params, *bound_params),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        return {
            "n": int(row[0]),
            "sum": float(row[1] or 0.0),
            "min": float(row[2]) if row[2] is not None else None,
            "max": float(row[3]) if row[3] is not None else None,
        }

    async def _metric(
        self,
        kind: str,
        since: float,
        column: str,
        role: str | None = None,
        target: str | None = None,
        tier: str | None = None,
        until: float | None = None,
    ) -> dict | None:
        """avg / min / max / p95 / n for one column, across both storage tiers.

        Ranges that reach back past the raw window are answered by combining the
        probes still on disk with the hourly sums. The sums are what make that
        correct: an average of hourly averages would weight an hour with two
        probes the same as an hour with 720. p95 is the one number that cannot be
        reconstructed, so it is reported as None rather than faked from partial
        data.
        """
        clauses = [f"{column} IS NOT NULL", "source = 'local'"]
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if role:
            clauses.append("role = ?")
            params.append(role)
        if target:
            clauses.append("target = ?")
            params.append(target)
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        raw_since = since
        rolled = None
        if kind == "latency" and column in ("probe_ms", "jitter_ms", "loss_pct"):
            cutoff = await self.raw_cutoff()
            if cutoff and since < cutoff:
                rolled = await self._rollup_column(column, since, until, role, target)
                raw_since = cutoff
        where = " AND ".join(clauses) + _ts_bounds(raw_since, until)[0]
        params.extend(_ts_bounds(raw_since, until)[1])

        cursor = await self.db.execute(
            f"SELECT COUNT(*), AVG({column}), MIN({column}), MAX({column}), SUM({column}) "
            f"FROM samples WHERE {where}",
            params,
        )
        row = await cursor.fetchone()
        raw_n = int(row[0]) if row and row[0] else 0
        count = raw_n + (rolled["n"] if rolled else 0)
        if not count:
            return None
        total = (float(row[4] or 0.0) if raw_n else 0.0) + (rolled["sum"] if rolled else 0.0)
        average = total / count
        minimum = min(
            [v for v in (float(row[2]) if raw_n else None,
                         rolled["min"] if rolled else None) if v is not None]
        )
        maximum = max(
            [v for v in (float(row[3]) if raw_n else None,
                         rolled["max"] if rolled else None) if v is not None]
        )

        p95 = None
        if not rolled:
            # p95 via OFFSET: SQLite has no PERCENTILE function.
            offset = max(0, math.ceil(count * 0.95) - 1)
            cursor = await self.db.execute(
                f"SELECT {column} FROM samples WHERE {where} ORDER BY {column} ASC LIMIT 1 OFFSET ?",
                [*params, offset],
            )
            p95_row = await cursor.fetchone()
            p95 = float(p95_row[0]) if p95_row else maximum

        cursor = await self.db.execute(
            f"SELECT {column} FROM samples WHERE {where} ORDER BY ts DESC, id DESC LIMIT 1",
            params,
        )
        last_row = await cursor.fetchone()

        return {
            "avg": round(average, 3),
            "min": round(minimum, 3),
            "max": round(maximum, 3),
            "p95": round(p95, 3) if p95 is not None else None,
            "last": round(float(last_row[0]), 3) if last_row else None,
            "n": count,
            "sampled": raw_n,
        }

    async def metrics_by_target(self, since: float, until: float | None = None) -> dict[str, dict]:
        """avg/min/max/last of ``probe_ms`` per target, for the dashboard.

        The target table asks one question -- the average per target -- and it
        used to be answered with one aggregate query per target, each with its own
        p95 sort and its own "last row" lookup: fourteen targets meant forty-two
        queries and most of a day-wide page load.

        Now it is one grouped pass over both storage tiers, plus one index lookup
        per target for the newest value (``idx_samples_target_ts`` makes that
        nearly free). The p95 is deliberately *not* here: nothing displays a
        per-target p95, and computing one costs a sort per target, which is
        exactly the cost this method exists to remove. The cards keep their p95,
        because that one is shown.
        """
        raw_bounds, raw_params = _ts_bounds(max(since, await self._raw_floor()), until)
        roll_bounds, roll_params = _hour_bounds(since, until)
        cursor = await self.db.execute(
            "SELECT target, SUM(n) n, SUM(sum_ms) sum_ms, MIN(min_ms) min_ms, MAX(max_ms) max_ms"
            " FROM ("
            "   SELECT target, COUNT(*) n, SUM(probe_ms) sum_ms,"
            "          MIN(probe_ms) min_ms, MAX(probe_ms) max_ms"
            "   FROM samples WHERE kind = 'latency' AND source = 'local'"
            "    AND probe_ms IS NOT NULL" + raw_bounds +
            "   GROUP BY target"
            "   UNION ALL"
            "   SELECT target, ok AS n, sum_ms, min_ms, max_ms FROM rollups"
            "   WHERE kind = 'latency' AND ok IS NOT NULL" + roll_bounds +
            " ) GROUP BY target",
            (*raw_params, *roll_params),
        )
        folded = [dict(row) for row in await cursor.fetchall()]

        cutoff = await self.raw_cutoff()
        raw_since = max(since, cutoff) if cutoff else since
        bounds, params = _ts_bounds(raw_since, until)
        out: dict[str, dict] = {}
        for row in folded:
            count = int(row.get("n") or 0)
            if not count:
                continue
            target = row["target"]
            cursor = await self.db.execute(
                "SELECT probe_ms FROM samples WHERE kind = 'latency' AND source = 'local'"
                " AND target = ? AND probe_ms IS NOT NULL" + bounds +
                " ORDER BY ts DESC, id DESC LIMIT 1",
                (target, *params),
            )
            last = await cursor.fetchone()
            out[target] = {
                "avg": round(float(row["sum_ms"] or 0.0) / count, 3),
                "min": _round(row.get("min_ms")),
                "max": _round(row.get("max_ms")),
                "last": round(float(last[0]), 3) if last is not None else None,
                "n": count,
            }
        return out

    async def metrics_by_tier(self, since: float, until: float | None = None) -> dict[str, dict]:
        """Download and upload per tier, for the throughput cards.

        Same shape the four single-metric calls produced, from one grouped pass
        per direction plus a windowed pass for the p95 and the newest test. Speed
        tests are rare (a couple of hundred rows a day), but they were costing
        0.19s of a day-wide dashboard load in query overhead alone.
        """
        bounds, params = _ts_bounds(since, until)
        out: dict[str, dict] = {}
        for tier in ("quick", "sustained"):
            entry: dict[str, dict] = {}
            for column in SPEED_METRICS:
                cursor = await self.db.execute(
                    f"SELECT COUNT(*) n, SUM({column}) total, MIN({column}) low, MAX({column}) high"
                    f" FROM samples WHERE kind = 'speed' AND source = 'local' AND tier = ?"
                    f" AND {column} IS NOT NULL" + bounds,
                    (tier, *params),
                )
                row = await cursor.fetchone()
                count = int(row["n"] or 0)
                if not count:
                    entry[column] = None
                    continue
                cursor = await self.db.execute(
                    f"SELECT {column} FROM samples WHERE kind = 'speed' AND source = 'local'"
                    f" AND tier = ? AND {column} IS NOT NULL" + bounds +
                    " ORDER BY ts DESC, id DESC LIMIT 1",
                    (tier, *params),
                )
                last = await cursor.fetchone()
                offset = max(0, math.ceil(count * 0.95) - 1)
                cursor = await self.db.execute(
                    f"SELECT {column} FROM samples WHERE kind = 'speed' AND source = 'local'"
                    f" AND tier = ? AND {column} IS NOT NULL" + bounds +
                    f" ORDER BY {column} ASC LIMIT 1 OFFSET ?",
                    (tier, *params, offset),
                )
                p95 = await cursor.fetchone()
                entry[column] = {
                    "avg": round(float(row["total"] or 0.0) / count, 3),
                    "min": _round(row["low"]),
                    "max": _round(row["high"]),
                    "p95": round(float(p95[0]), 3) if p95 is not None else None,
                    "last": round(float(last[0]), 3) if last is not None else None,
                    "n": count,
                    "sampled": count,
                }
            out[tier] = entry
        return out

    async def _raw_floor(self) -> float:
        """Where the raw probes start, so a wide window does not rescan them all."""
        return float(await self.raw_cutoff() or 0.0)

    async def uptime(self, since: float, until: float | None = None) -> dict:
        """Round-level uptime: a round is up if any internet target answered."""
        cutoff = await self.raw_cutoff()
        raw_since = max(since, cutoff)
        bounds, params = _ts_bounds(raw_since, until)
        cursor = await self.db.execute(
            "SELECT COUNT(*), SUM(up), SUM(CASE WHEN up = 0 THEN 1 ELSE 0 END) FROM ("
            "  SELECT round_id, MAX(ok) AS up FROM samples"
            "  WHERE kind = 'latency' AND source = 'local'"
            " AND role = 'internet' AND round_id IS NOT NULL"
            + bounds + " "
            + "  GROUP BY round_id"
            ")",
            tuple(params),
        )
        row = await cursor.fetchone()
        rounds = int(row[0] or 0) if row else 0
        up = int(row[1] or 0) if row else 0
        down = int(row[2] or 0) if row else 0

        # Rounds older than the raw window were counted when they were folded.
        if cutoff and since < cutoff:
            roll_bounds, roll_params = _hour_bounds(since, until)
            cursor = await self.db.execute(
                "SELECT SUM(rounds), SUM(down_rounds) FROM round_rollups WHERE hour IS NOT NULL"
                + roll_bounds,
                tuple(roll_params),
            )
            roll = await cursor.fetchone()
            if roll and roll[0]:
                rounds += int(roll[0])
                down += int(roll[1] or 0)
                up += int(roll[0]) - int(roll[1] or 0)

        probe_count = 0
        cursor = await self.db.execute(
            f"SELECT COUNT(*) FROM samples WHERE kind = 'latency'{_ts_bounds(raw_since, until)[0]}",
            tuple(_ts_bounds(raw_since, until)[1]),
        )
        probe_count += int((await cursor.fetchone())[0])
        if cutoff and since < cutoff:
            roll_bounds, roll_params = _hour_bounds(since, until)
            cursor = await self.db.execute(
                "SELECT SUM(probes) FROM rollups WHERE kind = 'latency'" + roll_bounds,
                tuple(roll_params),
            )
            probe_count += int((await cursor.fetchone())[0] or 0)

        return {
            "rounds": rounds,
            "rounds_up": up,
            "rounds_down": down,
            "up_pct": round(100.0 * up / rounds, 3) if rounds else None,
            "probes": probe_count,
        }

    async def summary(self, since: float, until: float | None = None) -> dict:
        """Everything the dashboard cards and tables need, in one payload."""
        internet = {
            metric: await self._metric("latency", since, metric, role="internet", until=until)
            for metric in LATENCY_METRICS
        }
        # One grouped pass instead of a query per target; see the method.
        by_target = await self.metrics_by_target(since, until)

        # Burst and sustained numbers answer different questions and must never
        # be averaged together.
        speed = await self.metrics_by_tier(since, until)

        counts = {
            "latency": await self.count("latency", since, until),
            "speed": await self.count("speed", since, until),
        }
        counts["total"] = counts["latency"] + counts["speed"]

        bytes_down = await self._sum("speed", since, "download_bytes", until)
        bytes_up = await self._sum("speed", since, "upload_bytes", until)

        uptime = await self.uptime(since, until)
        incidents = await self.incident_stats(since, until)

        return {
            "since": since,
            "counts": counts,
            "probe_ms": internet["probe_ms"],
            "jitter_ms": internet["jitter_ms"],
            "loss_pct": internet["loss_pct"],
            "speed": speed,
            "download_mbps": speed["sustained"]["download_mbps"],
            "upload_mbps": speed["sustained"]["upload_mbps"],
            "by_target": by_target,
            "bytes": {"downloaded": bytes_down, "uploaded": bytes_up},
            "uptime": uptime,
            "incidents": incidents,
        }

    async def _sum(
        self, kind: str, since: float, column: str, until: float | None = None
    ) -> int:
        bounds, params = _ts_bounds(since, until)
        cursor = await self.db.execute(
            f"SELECT SUM({column}) FROM samples WHERE kind = ? AND {column} IS NOT NULL{bounds}",
            (kind, *params),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] else 0

    # ------------------------------------------------------------- incidents
    async def open_incident(
        self,
        kind: str,
        scope: str,
        started_at: float,
        targets_total: int,
        targets_failed: int,
        failed_targets: Sequence[str],
        detail: dict | None = None,
        rounds: int = 1,
        start_uncertainty_s: float | None = None,
    ) -> int:
        async with self._lock:
            cursor = await self.db.execute(
                "INSERT INTO outages (kind, scope, started_at, rounds, targets_total,"
                " targets_failed, failed_targets, detail, ongoing, interrupted,"
                " start_uncertainty_s)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)",
                (
                    kind,
                    scope,
                    started_at,
                    max(1, int(rounds)),
                    targets_total,
                    targets_failed,
                    ",".join(failed_targets),
                    json.dumps(detail or {}),
                    start_uncertainty_s,
                ),
            )
            await self.db.commit()
            return int(cursor.lastrowid)

    async def extend_incident(
        self,
        incident_id: int,
        rounds: int,
        targets_failed: int,
        failed_targets: Sequence[str],
        detail: dict | None = None,
    ) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE outages SET rounds = ?, targets_failed = ?, failed_targets = ?,"
                " detail = ? WHERE id = ?",
                (rounds, targets_failed, ",".join(failed_targets), json.dumps(detail or {}), incident_id),
            )
            await self.db.commit()

    async def close_incident(
        self, incident_id: int, ended_at: float, end_uncertainty_s: float | None = None
    ) -> dict | None:
        async with self._lock:
            cursor = await self.db.execute("SELECT started_at FROM outages WHERE id = ?", (incident_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            duration = max(0.0, ended_at - float(row[0]))
            await self.db.execute(
                "UPDATE outages SET ended_at = ?, duration_s = ?, ongoing = 0,"
                " end_uncertainty_s = COALESCE(?, end_uncertainty_s) WHERE id = ?",
                (ended_at, duration, end_uncertainty_s, incident_id),
            )
            await self.db.commit()
            cursor = await self.db.execute("SELECT * FROM outages WHERE id = ?", (incident_id,))
            return _row_to_dict(await cursor.fetchone())

    async def close_dangling(self, now_ts: float) -> int:
        """Close incidents left open by a crash/restart, flagged as interrupted."""
        async with self._lock:
            cursor = await self.db.execute(
                "UPDATE outages SET ongoing = 0, interrupted = 1, ended_at = ?,"
                " duration_s = MAX(0, ? - started_at),"
                " detail = COALESCE(detail, '{}') WHERE ongoing = 1",
                (now_ts, now_ts),
            )
            await self.db.commit()
            return cursor.rowcount or 0

    async def current_incidents(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM outages WHERE ongoing = 1 ORDER BY started_at ASC"
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def incidents(
        self, since: float = 0.0, limit: int = 200, until: float | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM outages WHERE started_at >= ?"
        params: list[Any] = [since]
        if until:
            sql += " AND started_at <= ?"
            params.append(until)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        cursor = await self.db.execute(sql, params)
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def incident_stats(self, since: float = 0.0, until: float | None = None) -> dict:
        cursor = await self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration_s), 0), COALESCE(MAX(duration_s), 0),"
            " COALESCE(AVG(duration_s), 0), COALESCE(SUM(rounds), 0)"
            " FROM outages WHERE started_at >= ? AND ongoing = 0"
            + (" AND started_at <= ?" if until else ""),
            (since, until) if until else (since,),
        )
        row = await cursor.fetchone()
        total = int(row[0] or 0)
        # Seed every known kind so consumers can rely on the keys existing.
        by_kind: dict[str, dict] = {
            kind: {"count": 0, "downtime_s": 0.0} for kind in INCIDENT_KINDS
        }
        cursor = await self.db.execute(
            "SELECT kind, COUNT(*), COALESCE(SUM(duration_s), 0) FROM outages"
            " WHERE started_at >= ? AND ongoing = 0"
            + (" AND started_at <= ?" if until else "")
            + " GROUP BY kind",
            (since, until) if until else (since,),
        )
        for kind_row in await cursor.fetchall():
            by_kind[kind_row[0]] = {
                "count": int(kind_row[1]),
                "downtime_s": round(float(kind_row[2]), 1),
            }
        cursor = await self.db.execute(
            "SELECT COALESCE(scope, 'unknown'), COUNT(*) FROM outages"
            " WHERE started_at >= ? AND ongoing = 0"
            + (" AND started_at <= ?" if until else "")
            + " GROUP BY scope",
            (since, until) if until else (since,),
        )
        by_scope = {r[0]: int(r[1]) for r in await cursor.fetchall()}
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM outages WHERE started_at >= ? AND ongoing = 1"
            + (" AND started_at <= ?" if until else ""),
            (since, until) if until else (since,),
        )
        ongoing = int((await cursor.fetchone())[0] or 0)
        return {
            "count": total,
            "ongoing": ongoing,
            "downtime_s": round(float(row[1]), 1),
            "longest_s": round(float(row[2]), 1),
            "average_s": round(float(row[3]), 1),
            "rounds_lost": int(row[4] or 0),
            "by_kind": by_kind,
            "by_scope": by_scope,
        }

    # ---------------------------------------------------------------- export
    async def to_csv(
        self, since: float | None = None, kind: str | None = None,
        until: float | None = None,
    ) -> str:
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        sql = "SELECT * FROM samples"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts ASC, id ASC"
        cursor = await self.db.execute(sql, params)
        rows = await cursor.fetchall()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("id", *SAMPLE_FIELDS))
        for row in rows:
            data = _row_to_dict(row)
            writer.writerow([_csv_cell(data.get(k)) for k in ("id", *SAMPLE_FIELDS)])
        return buffer.getvalue()

    async def incidents_to_csv(self, since: float = 0.0, until: float | None = None) -> str:
        rows = await self.incidents(since, limit=100000, until=until)
        columns = (
            "id", "kind", "scope", "started_at", "ended_at", "duration_s", "rounds",
            "targets_total", "targets_failed", "failed_targets", "detail", "ongoing",
            "interrupted", "start_uncertainty_s", "end_uncertainty_s",
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow((*columns, "started_iso", "ended_iso"))
        for row in rows:
            writer.writerow(
                [
                    *[_csv_cell(row.get(c)) for c in columns],
                    _iso(row.get("started_at")),
                    _iso(row.get("ended_at")),
                ]
            )
        return buffer.getvalue()


def _round(value: Any, digits: int = 3) -> float | None:
    """Round a nullable SQL value, keeping None as None."""
    return None if value is None else round(float(value), digits)


def _row_to_dict(row: aiosqlite.Row | None) -> dict:
    if row is None:
        return {}
    data = dict(row)
    for flag in ("ok", "ongoing", "interrupted", "capped", "reached"):
        if flag in data and data[flag] is not None:
            data[flag] = bool(data[flag])
    for series in ("download_intervals", "upload_intervals", "hop_list"):
        if isinstance(data.get(series), str):
            try:
                data[series] = json.loads(data[series])
            except (TypeError, ValueError):
                data[series] = None
    return data


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return value


def _iso(ts: Any) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(ts)))
