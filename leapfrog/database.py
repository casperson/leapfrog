"""SQLite database access layer using aiosqlite."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .domain import (
    DEFAULT_PROFANITY_ALLOWLIST,
    DEFAULT_PROFANITY_TERMS,
    SCAN_STAGE_ORDER,
    Segment,
    SUPPORTED_CATEGORIES,
)
from .logger import get_logger
from .paths import get_legacy_thumbnails_dir, get_thumbnails_dir

logger = get_logger(__name__)

_DB_PATH: Path | None = None


def set_db_path(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path


def get_db_path() -> Path:
    if _DB_PATH is None:
        raise RuntimeError("Database path not configured. Call set_db_path() first.")
    return _DB_PATH


@asynccontextmanager
async def get_connection():
    async with aiosqlite.connect(str(get_db_path())) as conn:
        conn.row_factory = aiosqlite.Row
        # foreign_keys is connection-scoped and must be set per connection.
        # journal_mode=WAL is persistent after init_db(); no need to repeat it here.
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid     TEXT    NOT NULL,
    media_id      TEXT    DEFAULT '',
    title         TEXT,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    category      TEXT    DEFAULT 'nudity',
    source        TEXT    DEFAULT 'nudenet',
    confidence    REAL,
    labels        TEXT    DEFAULT '',
    text_excerpt  TEXT,
    thumbnail_path TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    review_status TEXT    DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_segments_guid ON segments(plex_guid);
CREATE INDEX IF NOT EXISTS idx_segments_media_category ON segments(media_id, category);
CREATE INDEX IF NOT EXISTS idx_segments_guid_category ON segments(plex_guid, category);

CREATE TABLE IF NOT EXISTS user_filters (
    plex_username TEXT PRIMARY KEY,
    enabled       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_preferences (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    enabled       INTEGER DEFAULT 1,
    threshold     REAL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, category)
);
CREATE INDEX IF NOT EXISTS idx_user_preferences_user ON user_preferences(user_id);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid   TEXT    UNIQUE NOT NULL,
    title       TEXT,
    file_path   TEXT,
    rating_key  TEXT,
    library_id  TEXT,
    library_title TEXT,
    media_type  TEXT DEFAULT 'movie',
    content_rating TEXT DEFAULT '',
    year        INTEGER,
    status      TEXT DEFAULT 'pending',
    progress    REAL DEFAULT 0,
    force_scan  INTEGER DEFAULT 0,
    ignored     INTEGER DEFAULT 0,
    queue_state TEXT    DEFAULT 'idle',
    queue_position INTEGER,
    queue_priority INTEGER DEFAULT 0,
    cancel_requested INTEGER DEFAULT 0,
    queue_reason TEXT DEFAULT '',
    queued_at   TIMESTAMP,
    queue_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    error_msg   TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS segment_library_entries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash         TEXT    NOT NULL,
    file_name         TEXT    NOT NULL,
    file_size         INTEGER,
    duration_ms       INTEGER,
    segments_json     TEXT    NOT NULL,
    cloud_version     INTEGER DEFAULT 0,
    source_instance   TEXT    NOT NULL,
    confidence_level  TEXT    DEFAULT 'local',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_hash, source_instance)
);
CREATE INDEX IF NOT EXISTS idx_segment_library_hash ON segment_library_entries(file_hash);

CREATE TABLE IF NOT EXISTS sync_metadata (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_name             TEXT    NOT NULL UNIQUE,
    github_repo               TEXT,
    github_token              TEXT,
    last_sync_time            TIMESTAMP,
    sync_enabled              INTEGER DEFAULT 0,
    conflict_resolution       TEXT    DEFAULT 'consensus',
    verified_threshold        INTEGER DEFAULT 2,
    timing_tolerance_ms       INTEGER DEFAULT 2000,
    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bg_jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type           TEXT    NOT NULL,
    status             TEXT    DEFAULT 'queued',
    progress_percent   INTEGER DEFAULT 0,
    result_data        TEXT,
    error_message      TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at         TIMESTAMP,
    completed_at       TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bg_jobs_status ON bg_jobs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS media_scan_status (
    media_id       TEXT    NOT NULL,
    category       TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    source         TEXT    DEFAULT '',
    detail         TEXT    DEFAULT '',
    segment_count  INTEGER DEFAULT 0,
    progress       REAL    DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(media_id, category)
);
CREATE INDEX IF NOT EXISTS idx_media_scan_status_media ON media_scan_status(media_id);

CREATE TABLE IF NOT EXISTS media_scan_stage_status (
    media_id       TEXT    NOT NULL,
    stage_key      TEXT    NOT NULL,
    category       TEXT,
    stage_order    INTEGER DEFAULT 0,
    status         TEXT    NOT NULL,
    source         TEXT    DEFAULT '',
    detail         TEXT    DEFAULT '',
    progress       REAL    DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(media_id, stage_key)
);
CREATE INDEX IF NOT EXISTS idx_media_scan_stage_status_media
ON media_scan_stage_status(media_id, stage_order, stage_key);
"""

DEFAULT_SETTINGS = {
    "plex_url": "",
    "plex_token": "",
    "poll_interval": "5",
    "confidence_threshold": "0.6",
    "skip_buffer_ms": "3000",
    "scan_step_ms": "5000",
    "scan_workers": "2",
    "segment_gap_ms": "12000",
    "segment_min_hits": "1",
    "scan_window_start": "23:00",
    "scan_window_end": "06:00",
    "log_level": "INFO",
    "excluded_library_ids": "[]",
    "scan_ratings": "[]",  # empty = scan all ratings
    "scan_labels": "[\"FEMALE_BREAST_EXPOSED\",\"FEMALE_GENITALIA_EXPOSED\",\"MALE_GENITALIA_EXPOSED\",\"ANUS_EXPOSED\",\"BUTTOCKS_EXPOSED\"]",
    "nudenet_model": "320n",
    "nudenet_model_path": "",
    "semantic_model_repo": "Xenova/clip-vit-base-patch32",
    "semantic_processor_repo": "openai/clip-vit-base-patch32",
    "semantic_model_variant": "int8",
    "profanity_terms": json.dumps(DEFAULT_PROFANITY_TERMS),
    "profanity_allowlist": json.dumps(DEFAULT_PROFANITY_ALLOWLIST),
    "default_profanity_threshold": "0.5",
    "sexual_content_detection_threshold": "0.30",
    "violence_detection_threshold": "0.28",
    "drugs_detection_threshold": "0.30",
    "profanity_merge_gap_ms": "1500",
    "whisper_enabled": "1",
    "whisper_model": "base",
    "log_buffer_capacity": "1000",
    # Segment library sharing settings
    "sync_enabled": "0",
    "sync_instance_name": "",
    "sync_github_repo": "",
    "sync_conflict_resolution": "consensus",
    "sync_verified_threshold": "2",
    "sync_timing_tolerance_ms": "2000",
}


def _allocate_thumbnail_destination(target_path: Path) -> Path:
    """Return a non-conflicting destination path for a migrated thumbnail file."""
    if not target_path.exists():
        return target_path

    suffix = target_path.suffix
    stem = target_path.stem
    counter = 1
    while True:
        candidate = target_path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


async def _migrate_legacy_thumbnail_paths(conn: aiosqlite.Connection) -> None:
    """Move historical thumbnail files into the configured data dir and update DB rows."""
    legacy_dir = get_legacy_thumbnails_dir()
    current_dir = get_thumbnails_dir()
    if legacy_dir == current_dir:
        return

    rows = await conn.execute_fetchall(
        """
        SELECT id, thumbnail_path
        FROM segments
        WHERE COALESCE(thumbnail_path, '') != ''
        """
    )
    if not rows:
        return

    current_dir.mkdir(parents=True, exist_ok=True)
    migrated = 0

    for row in rows:
        raw_path = str(row["thumbnail_path"] or "").strip()
        if not raw_path:
            continue

        thumbnail_path = Path(raw_path).expanduser()
        if legacy_dir not in thumbnail_path.parents:
            continue

        destination = current_dir / thumbnail_path.name
        updated_path: Path | None = None

        if thumbnail_path.exists():
            destination = _allocate_thumbnail_destination(destination)
            shutil.move(str(thumbnail_path), str(destination))
            updated_path = destination
        elif destination.exists():
            updated_path = destination

        if updated_path is None:
            continue

        await conn.execute(
            "UPDATE segments SET thumbnail_path=? WHERE id=?",
            (str(updated_path), row["id"]),
        )
        migrated += 1

    if migrated:
        logger.info(
            "Migrated %s legacy thumbnail path(s) into %s",
            migrated,
            current_dir,
        )


async def init_db() -> None:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with get_connection() as conn:
        # WAL mode is a persistent DB property; set it once at init rather than
        # on every connection to avoid redundant per-connection overhead.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        # Additive schema migrations — each ALTER TABLE is idempotent.
        # Catch only the specific OperationalError SQLite raises for duplicate columns;
        # any other error (e.g. corrupt DB, permission failure) is re-raised immediately.
        migrations = [
            "ALTER TABLE scan_jobs ADD COLUMN content_rating TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN media_type TEXT DEFAULT 'movie'",
            "ALTER TABLE scan_jobs ADD COLUMN force_scan INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN year INTEGER",
            "ALTER TABLE segments ADD COLUMN media_id TEXT DEFAULT ''",
            "ALTER TABLE segments ADD COLUMN category TEXT DEFAULT 'nudity'",
            "ALTER TABLE segments ADD COLUMN source TEXT DEFAULT 'nudenet'",
            "ALTER TABLE segments ADD COLUMN labels TEXT DEFAULT ''",
            "ALTER TABLE segments ADD COLUMN text_excerpt TEXT",
            "ALTER TABLE segments ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE segments ADD COLUMN review_status TEXT DEFAULT 'pending'",
            "ALTER TABLE scan_jobs ADD COLUMN ignored INTEGER DEFAULT 0",
            # show_guid stores the Plex grandparentGuid for episodes so the
            # scan queue can group all episodes of a show together.
            "ALTER TABLE scan_jobs ADD COLUMN show_guid TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN queue_state TEXT DEFAULT 'idle'",
            "ALTER TABLE scan_jobs ADD COLUMN queue_position INTEGER",
            "ALTER TABLE scan_jobs ADD COLUMN queue_priority INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN cancel_requested INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN queue_reason TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN queued_at TIMESTAMP",
            "ALTER TABLE media_scan_status ADD COLUMN segment_count INTEGER DEFAULT 0",
            "ALTER TABLE media_scan_status ADD COLUMN progress REAL DEFAULT 0",
        ]
        for stmt in migrations:
            try:
                await conn.execute(stmt)
                await conn.commit()
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    # Unexpected error — surface it rather than silently continuing.
                    logger.error("Unexpected migration failure: %s — %s", stmt, exc)
                    raise
        # SQLite only allows ALTER TABLE ADD COLUMN with constant defaults, so
        # queue_updated_at must be added without DEFAULT and then backfilled.
        try:
            await conn.execute("ALTER TABLE scan_jobs ADD COLUMN queue_updated_at TIMESTAMP")
            await conn.execute(
                """
                UPDATE scan_jobs
                SET queue_updated_at = CURRENT_TIMESTAMP
                WHERE queue_updated_at IS NULL
                """
            )
            await conn.commit()
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                logger.error(
                    "Unexpected migration failure: ALTER TABLE scan_jobs ADD COLUMN queue_updated_at TIMESTAMP — %s",
                    exc,
                )
                raise
        await conn.execute(
            """
            UPDATE scan_jobs
            SET queue_updated_at = COALESCE(queue_updated_at, CURRENT_TIMESTAMP)
            """
        )
        # Existing databases may not have the queue columns until the additive
        # migrations above run, so create this index only after migration.
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scan_jobs_queue
            ON scan_jobs(queue_state, queue_priority DESC, queue_position ASC, queued_at ASC, created_at ASC)
            """
        )
        old_pref_table = await (
            await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='user_category_preferences'"
            )
        ).fetchone()
        if old_pref_table:
            await conn.execute(
                """
                INSERT INTO user_preferences(user_id, category, enabled, threshold, created_at, updated_at)
                SELECT user_id, category, enabled, threshold, created_at, updated_at
                FROM user_category_preferences
                WHERE 1=1
                ON CONFLICT(user_id, category) DO UPDATE SET
                    enabled=excluded.enabled,
                    threshold=excluded.threshold,
                    updated_at=excluded.updated_at
                """
            )
        await conn.execute(
            "UPDATE segments SET media_id = plex_guid WHERE COALESCE(media_id, '') = ''"
        )
        await _migrate_legacy_thumbnail_paths(conn)
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()
    logger.info("Database initialised at %s", db_path)


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_all_settings() -> dict[str, str]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}


async def get_setting(key: str, default: str = "") -> str:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
        return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await conn.commit()


async def update_settings(data: dict[str, str]) -> None:
    async with get_connection() as conn:
        for key, value in data.items():
            await conn.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        await conn.commit()


# ── User Filters ──────────────────────────────────────────────────────────────


def _normalize_segment_row(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    raw = dict(row)
    segment = Segment.from_row(raw)
    normalized = segment.to_record(plex_guid=raw.get("plex_guid"))
    normalized["id"] = raw.get("id")
    return normalized

async def get_all_user_filters() -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT plex_username, enabled FROM user_filters ORDER BY plex_username")
        return [dict(r) for r in rows]


async def get_user_filter(username: str) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT plex_username, enabled FROM user_filters WHERE plex_username=?", (username,)
        )).fetchone()
        return dict(row) if row else None


async def upsert_user_filter(username: str, enabled: bool) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO user_filters(plex_username, enabled) VALUES(?,?) "
            "ON CONFLICT(plex_username) DO UPDATE SET enabled=excluded.enabled",
            (username, 1 if enabled else 0),
        )
        await conn.commit()


async def get_all_user_category_preferences() -> list[dict]:
    """Backward-compatible wrapper for code paths that still use the old name."""
    return await get_all_user_preferences()


async def get_all_user_preferences() -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, user_id, category, enabled, threshold, created_at, updated_at "
            "FROM user_preferences ORDER BY user_id, category"
        )
        return [dict(row) for row in rows]


async def get_user_preferences(user_id: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, user_id, category, enabled, threshold, created_at, updated_at "
            "FROM user_preferences WHERE user_id=? ORDER BY category",
            (user_id,),
        )
        return [dict(row) for row in rows]


async def get_user_category_preferences(user_id: str) -> list[dict]:
    """Backward-compatible wrapper for code paths that still use the old name."""
    return await get_user_preferences(user_id)


async def set_user_preference(
    user_id: str,
    category: str,
    *,
    enabled: bool,
    threshold: float | None,
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO user_preferences(user_id, category, enabled, threshold)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id, category)
            DO UPDATE SET
                enabled=excluded.enabled,
                threshold=excluded.threshold,
                updated_at=CURRENT_TIMESTAMP
            """,
            (user_id, category, 1 if enabled else 0, threshold),
        )
        await conn.commit()


async def upsert_user_category_preference(
    user_id: str,
    category: str,
    *,
    enabled: bool,
    threshold: float | None,
) -> None:
    """Backward-compatible wrapper for code paths that still use the old name."""
    await set_user_preference(
        user_id,
        category,
        enabled=enabled,
        threshold=threshold,
    )


# ── Segments ──────────────────────────────────────────────────────────────────

async def get_segments_for_guid(plex_guid: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        return [_normalize_segment_row(row) for row in rows]


async def get_segments_for_guid_with_setting(
    plex_guid: str, setting_key: str, setting_default: str
) -> tuple[list[dict], str]:
    """Return segments for a guid and a setting value in a single connection.

    Avoids the cost of opening two aiosqlite connections sequentially when
    both pieces of data are needed together (e.g. segments + scan_labels).
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        row = await (
            await conn.execute("SELECT value FROM settings WHERE key=?", (setting_key,))
        ).fetchone()
        setting_val = row["value"] if row else setting_default
        return ([_normalize_segment_row(row) for row in rows], setting_val)


async def get_segments_by_rating_key(rating_key: str) -> list[dict]:
    """Look up segments by scan_jobs.rating_key — fallback when session GUID differs from stored GUID."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """SELECT s.* FROM segments s
               JOIN scan_jobs j ON j.plex_guid = s.plex_guid
               WHERE j.rating_key = ?
               ORDER BY s.start_ms""",
            (rating_key,),
        )
        return [_normalize_segment_row(row) for row in rows]


async def count_segments_for_guid(plex_guid: str) -> int:
    """Return segment count for a title using SELECT COUNT — avoids loading full rows."""
    async with get_connection() as conn:
        row = await (
            await conn.execute("SELECT COUNT(*) FROM segments WHERE plex_guid=?", (plex_guid,))
        ).fetchone()
        return row[0] if row else 0


async def delete_segments_for_guid(plex_guid: str) -> int:
    """Delete all stored segments for a title and return deleted row count."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE plex_guid=?", (plex_guid,))
        await conn.commit()
        return cursor.rowcount


async def delete_segments_for_guid_category(plex_guid: str, category: str) -> int:
    """Delete category-specific segments for a title."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM segments WHERE plex_guid=? AND category=?",
            (plex_guid, category),
        )
        await conn.commit()
        return cursor.rowcount


async def get_thumbnail_paths_for_guid(plex_guid: str) -> list[str]:
    """Return non-empty thumbnail paths for all stored segments on one title."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT thumbnail_path
            FROM segments
            WHERE plex_guid=? AND COALESCE(thumbnail_path, '') != ''
            """,
            (plex_guid,),
        )
        return [str(row["thumbnail_path"]) for row in rows if row["thumbnail_path"]]


async def get_thumbnail_paths_for_guid_category(plex_guid: str, category: str) -> list[str]:
    """Return non-empty thumbnail paths for one title/category segment subset."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT thumbnail_path
            FROM segments
            WHERE plex_guid=? AND category=? AND COALESCE(thumbnail_path, '') != ''
            """,
            (plex_guid, category),
        )
        return [str(row["thumbnail_path"]) for row in rows if row["thumbnail_path"]]


async def get_all_segments(limit: int = 200, offset: int = 0) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [_normalize_segment_row(row) for row in rows]


async def insert_segment(
    plex_guid: str,
    title: str,
    start_ms: int,
    end_ms: int,
    confidence: float | None = None,
    thumbnail_path: str | None = None,
    labels: str = "",
    media_id: str | None = None,
    category: str = "nudity",
    source: str = "nudenet",
    text_excerpt: str | None = None,
    review_status: str = "pending",
) -> int:
    segment = Segment(
        media_id=media_id or plex_guid,
        start_ms=start_ms,
        end_ms=end_ms,
        category=category,
        source=source,
        confidence=confidence,
        text_excerpt=text_excerpt,
        title=title,
        thumbnail_path=thumbnail_path,
        labels=labels,
        review_status=review_status,
    )
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO segments(
                plex_guid, media_id, title, start_ms, end_ms, category, source,
                confidence, labels, text_excerpt, thumbnail_path, review_status, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (
                plex_guid,
                segment.media_id,
                segment.title,
                segment.start_ms,
                segment.end_ms,
                segment.category,
                segment.source,
                segment.confidence,
                segment.labels,
                segment.text_excerpt,
                segment.thumbnail_path,
                segment.review_status,
            ),
        )
        await conn.commit()
        return cursor.lastrowid


def _coerce_segment(plex_guid: str, segment: Segment | dict[str, Any]) -> Segment:
    if isinstance(segment, Segment):
        return segment
    record = dict(segment)
    record.setdefault("media_id", plex_guid)
    record.setdefault("category", "nudity")
    record.setdefault("source", "nudenet")
    return Segment(
        media_id=str(record.get("media_id") or plex_guid),
        start_ms=int(record.get("start_ms") or 0),
        end_ms=int(record.get("end_ms") or 0),
        category=str(record.get("category") or "nudity"),
        source=str(record.get("source") or "nudenet"),
        confidence=record.get("confidence"),
        text_excerpt=record.get("text_excerpt"),
        title=str(record.get("title") or ""),
        thumbnail_path=record.get("thumbnail_path"),
        labels=str(record.get("labels") or ""),
        review_status=str(record.get("review_status") or "pending"),
    )


async def insert_segments(plex_guid: str, segments: list[Segment | dict[str, Any]]) -> None:
    """Insert multiple segments in one transaction."""
    if not segments:
        return
    normalized_segments = [_coerce_segment(plex_guid, segment) for segment in segments]
    async with get_connection() as conn:
        await conn.executemany(
            """
            INSERT INTO segments(
                plex_guid, media_id, title, start_ms, end_ms, category, source,
                confidence, labels, text_excerpt, thumbnail_path, review_status, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """,
            [
                (
                    plex_guid,
                    segment.media_id,
                    segment.title,
                    segment.start_ms,
                    segment.end_ms,
                    segment.category,
                    segment.source,
                    segment.confidence,
                    segment.labels,
                    segment.text_excerpt,
                    segment.thumbnail_path,
                    segment.review_status,
                )
                for segment in normalized_segments
            ],
        )
        await conn.commit()


async def delete_segment(segment_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE id=?", (segment_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_segment_by_id(segment_id: int) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,))).fetchone()
        return _normalize_segment_row(row) if row else None


async def get_segments_grouped_by_title() -> list[dict]:
    """Return distinct (plex_guid, title) pairs that have segments."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT plex_guid, title, COUNT(*) as segment_count FROM segments GROUP BY plex_guid ORDER BY title"
        )
        return [dict(r) for r in rows]


async def get_segment_counts_by_category_for_library(library_id: str) -> dict[str, dict[str, int]]:
    """Return {plex_guid: {category: count}} for all titles in a library."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT s.plex_guid, s.category, COUNT(*) as cnt
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.library_id = ?
            GROUP BY s.plex_guid, s.category
            """,
            (library_id,),
        )
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["plex_guid"], {})[row["category"] or "nudity"] = row["cnt"]
        return counts


# ── Scan Jobs ─────────────────────────────────────────────────────────────────

async def upsert_scan_job(
    plex_guid: str,
    title: str,
    file_path: str,
    rating_key: str,
    library_id: str,
    library_title: str,
    content_rating: str = "",
    media_type: str = "movie",
    year: int | None = None,
    show_guid: str = "",
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO scan_jobs"
            "("
            "plex_guid, title, file_path, rating_key, library_id, library_title, "
            "content_rating, media_type, year, show_guid"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                plex_guid,
                title,
                file_path,
                rating_key,
                library_id,
                library_title,
                content_rating,
                media_type,
                year,
                show_guid,
            ),
        )
        for category in SUPPORTED_CATEGORIES:
            await conn.execute(
                """
                INSERT OR IGNORE INTO media_scan_status(
                    media_id, category, status, source, detail, segment_count, progress
                )
                VALUES(?, ?, 'pending', '', '', 0, 0)
                """,
                (plex_guid, category),
            )
        await conn.commit()


async def _get_ordered_queue_guids_conn(conn: aiosqlite.Connection) -> list[str]:
    rows = await conn.execute_fetchall(
        """
        SELECT plex_guid
        FROM scan_jobs
        WHERE queue_state='queued'
        ORDER BY queue_priority DESC, queue_position ASC, queued_at ASC, created_at ASC, plex_guid ASC
        """
    )
    return [str(row["plex_guid"]) for row in rows]


async def _apply_queue_order_conn(
    conn: aiosqlite.Connection,
    ordered_guids: list[str],
) -> None:
    await conn.executemany(
        """
        UPDATE scan_jobs
        SET
            queue_state='queued',
            queue_position=?,
            queued_at=COALESCE(queued_at, CURRENT_TIMESTAMP),
            queue_updated_at=CURRENT_TIMESTAMP
        WHERE plex_guid=?
        """,
        [(index, plex_guid) for index, plex_guid in enumerate(ordered_guids, start=1)],
    )


async def get_queue_snapshot() -> list[dict[str, Any]]:
    """Return the current stable queued-job snapshot for the UI."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT *
            FROM scan_jobs
            WHERE queue_state='queued'
            ORDER BY queue_priority DESC, queue_position ASC, queued_at ASC, created_at ASC, plex_guid ASC
            """
        )
        return [dict(row) for row in rows]


async def queue_scan_job(
    plex_guid: str,
    *,
    to_front: bool = False,
    priority: int = 0,
    reason: str = "",
) -> dict[str, Any] | None:
    """Insert or update one job in the persisted queue."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        job = await (
            await conn.execute("SELECT * FROM scan_jobs WHERE plex_guid=?", (plex_guid,))
        ).fetchone()
        if not job:
            await conn.rollback()
            return None
        current_order = await _get_ordered_queue_guids_conn(conn)
        if plex_guid in current_order:
            current_order.remove(plex_guid)
        if to_front:
            current_order.insert(0, plex_guid)
        else:
            current_order.append(plex_guid)
        await conn.execute(
            """
            UPDATE scan_jobs
            SET
                queue_state='queued',
                queue_priority=?,
                queue_reason=?,
                cancel_requested=0,
                queued_at=COALESCE(queued_at, CURRENT_TIMESTAMP),
                queue_updated_at=CURRENT_TIMESTAMP
            WHERE plex_guid=?
            """,
            (priority, reason, plex_guid),
        )
        await _apply_queue_order_conn(conn, current_order)
        await conn.commit()
        row = await (
            await conn.execute("SELECT * FROM scan_jobs WHERE plex_guid=?", (plex_guid,))
        ).fetchone()
        return dict(row) if row else None


async def replace_queue_snapshot(
    ordered_guids: list[str],
    *,
    preserve_non_listed: bool = True,
) -> list[dict[str, Any]]:
    """Rewrite the queued ordering to match the provided guid list."""
    normalized = [str(guid).strip() for guid in ordered_guids if str(guid).strip()]
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        current_order = await _get_ordered_queue_guids_conn(conn)
        if preserve_non_listed:
            tail = [guid for guid in current_order if guid not in normalized]
            next_order = normalized + tail
        else:
            next_order = normalized
        queued_guids = set(current_order)
        if queued_guids:
            placeholders = ",".join("?" * len(queued_guids))
            await conn.execute(
                f"""
                UPDATE scan_jobs
                SET queue_position=NULL, queue_updated_at=CURRENT_TIMESTAMP
                WHERE plex_guid IN ({placeholders})
                """,
                list(queued_guids),
            )
        if next_order:
            await _apply_queue_order_conn(conn, next_order)
        await conn.commit()
    return await get_queue_snapshot()


async def move_queue_item(plex_guid: str, direction: str) -> list[dict[str, Any]]:
    """Move one queued item within the ordered snapshot."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        order = await _get_ordered_queue_guids_conn(conn)
        if plex_guid not in order:
            await conn.rollback()
            return await get_queue_snapshot()
        current_index = order.index(plex_guid)
        order.pop(current_index)
        if direction == "top":
            new_index = 0
        elif direction == "bottom":
            new_index = len(order)
        elif direction == "up":
            new_index = max(0, current_index - 1)
        elif direction == "down":
            new_index = min(len(order), current_index + 1)
        else:
            await conn.rollback()
            raise ValueError(f"Unsupported queue direction: {direction}")
        order.insert(new_index, plex_guid)
        await _apply_queue_order_conn(conn, order)
        await conn.commit()
    return await get_queue_snapshot()


async def cancel_queue_item(plex_guid: str) -> bool:
    """Remove one queued item from the persisted queue."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        order = await _get_ordered_queue_guids_conn(conn)
        if plex_guid not in order:
            await conn.rollback()
            return False
        order.remove(plex_guid)
        await conn.execute(
            """
            UPDATE scan_jobs
            SET
                queue_state='idle',
                queue_position=NULL,
                queue_priority=0,
                queue_reason='',
                cancel_requested=1,
                queue_updated_at=CURRENT_TIMESTAMP
            WHERE plex_guid=?
            """,
            (plex_guid,),
        )
        await _apply_queue_order_conn(conn, order)
        await conn.commit()
        return True


async def cancel_queue_items(plex_guids: list[str]) -> int:
    """Remove several queued items at once and return the canceled count."""
    normalized = [str(guid).strip() for guid in plex_guids if str(guid).strip()]
    if not normalized:
        return 0
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        order = await _get_ordered_queue_guids_conn(conn)
        current = set(order)
        canceled = [guid for guid in normalized if guid in current]
        remaining = [guid for guid in order if guid not in set(canceled)]
        if canceled:
            placeholders = ",".join("?" * len(canceled))
            await conn.execute(
                f"""
                UPDATE scan_jobs
                SET
                    queue_state='idle',
                    queue_position=NULL,
                    queue_priority=0,
                    queue_reason='',
                    cancel_requested=1,
                    queue_updated_at=CURRENT_TIMESTAMP
                WHERE plex_guid IN ({placeholders})
                """,
                canceled,
            )
            await _apply_queue_order_conn(conn, remaining)
        await conn.commit()
        return len(canceled)


async def cancel_all_queued_items() -> int:
    """Remove every queued item and return the count."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        order = await _get_ordered_queue_guids_conn(conn)
        if not order:
            await conn.rollback()
            return 0
        placeholders = ",".join("?" * len(order))
        await conn.execute(
            f"""
            UPDATE scan_jobs
            SET
                queue_state='idle',
                queue_position=NULL,
                queue_priority=0,
                queue_reason='',
                cancel_requested=1,
                queue_updated_at=CURRENT_TIMESTAMP
            WHERE plex_guid IN ({placeholders})
            """,
            order,
        )
        await conn.commit()
        return len(order)


async def get_next_ready_queue_job(*, allow_windowed_only: bool) -> dict[str, Any] | None:
    """Atomically claim the next queued job that is ready to execute."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        where_clause = "queue_state='queued'"
        params: list[Any] = []
        if allow_windowed_only:
            where_clause += " AND (force_scan=1 OR 1=1)"
        else:
            where_clause += " AND force_scan=1"
        row = await (
            await conn.execute(
                f"""
                SELECT *
                FROM scan_jobs
                WHERE {where_clause}
                ORDER BY queue_priority DESC, queue_position ASC, queued_at ASC, created_at ASC, plex_guid ASC
                LIMIT 1
                """,
                params,
            )
        ).fetchone()
        if not row:
            await conn.rollback()
            return None
        plex_guid = str(row["plex_guid"])
        await conn.execute(
            """
            UPDATE scan_jobs
            SET
                queue_state='idle',
                queue_position=NULL,
                queue_reason='',
                queue_updated_at=CURRENT_TIMESTAMP,
                cancel_requested=0
            WHERE plex_guid=?
            """,
            (plex_guid,),
        )
        remaining = await _get_ordered_queue_guids_conn(conn)
        if remaining:
            await _apply_queue_order_conn(conn, remaining)
        await conn.commit()
        updated = await (
            await conn.execute("SELECT * FROM scan_jobs WHERE plex_guid=?", (plex_guid,))
        ).fetchone()
        return dict(updated) if updated else dict(row)


async def get_existing_guids(guids: list[str]) -> set[str]:
    """Return the subset of guids that already exist as scan jobs (single query)."""
    if not guids:
        return set()
    placeholders = ",".join("?" * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT plex_guid FROM scan_jobs WHERE plex_guid IN ({placeholders})", guids
        )
        return {row["plex_guid"] for row in rows}


async def refresh_scan_job_metadata_batch(
    items: list[tuple[str, str, str, str, str, int | None]],
) -> None:
    """Update mutable Plex metadata for multiple existing scan jobs in a single transaction.

    Each item is (plex_guid, title, file_path, rating_key, content_rating, year).
    Does not touch scan status or progress.
    """
    if not items:
        return
    async with get_connection() as conn:
        await conn.executemany(
            "UPDATE scan_jobs SET title=?, file_path=?, rating_key=?, content_rating=?, year=?"
            " WHERE plex_guid=?",
            [(title, file_path, rating_key, content_rating, year, plex_guid)
             for plex_guid, title, file_path, rating_key, content_rating, year in items],
        )
        await conn.commit()


async def delete_scan_jobs_not_in(library_id: str, keep_guids: list[str]) -> int:
    """Delete scan jobs for a library whose plex_guid is not in keep_guids. Returns deleted count."""
    if not keep_guids:
        return 0
    placeholders = ",".join("?" * len(keep_guids))
    async with get_connection() as conn:
        cursor = await conn.execute(
            f"DELETE FROM scan_jobs WHERE library_id=? AND plex_guid NOT IN ({placeholders})",
            [library_id, *keep_guids],
        )
        await conn.commit()
        return cursor.rowcount


async def get_scan_jobs(status: str | None = None) -> list[dict]:
    async with get_connection() as conn:
        if status:
            rows = await conn.execute_fetchall(
                "SELECT * FROM scan_jobs WHERE status=? ORDER BY created_at DESC", (status,)
            )
        else:
            rows = await conn.execute_fetchall("SELECT * FROM scan_jobs ORDER BY created_at DESC")
        return [dict(r) for r in rows]


async def get_scan_job_by_guid(plex_guid: str) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM scan_jobs WHERE plex_guid=?", (plex_guid,))).fetchone()
        return dict(row) if row else None


async def get_scan_job_by_rating_key(rating_key: str) -> dict | None:
    """Return one scan job by Plex rating_key."""
    async with get_connection() as conn:
        row = await (
            await conn.execute("SELECT * FROM scan_jobs WHERE rating_key=?", (rating_key,))
        ).fetchone()
        return dict(row) if row else None


async def get_scan_job_by_file_path(file_path: str) -> dict | None:
    """Return one scan job by exact media file path."""
    async with get_connection() as conn:
        row = await (
            await conn.execute("SELECT * FROM scan_jobs WHERE file_path=?", (file_path,))
        ).fetchone()
        return dict(row) if row else None


async def update_scan_job_status(
    plex_guid: str,
    status: str,
    progress: float = 0.0,
    error_msg: str | None = None,
) -> None:
    async with get_connection() as conn:
        if status == "scanning":
            await conn.execute(
                """
                UPDATE scan_jobs
                SET
                    status=?,
                    progress=?,
                    started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                    cancel_requested=0
                WHERE plex_guid=?
                """,
                (status, progress, plex_guid),
            )
        elif status in ("done", "failed"):
            await conn.execute(
                """
                UPDATE scan_jobs
                SET
                    status=?,
                    progress=?,
                    finished_at=CURRENT_TIMESTAMP,
                    error_msg=?,
                    cancel_requested=0
                WHERE plex_guid=?
                """,
                (status, progress, error_msg, plex_guid),
            )
        else:
            await conn.execute(
                """
                UPDATE scan_jobs
                SET
                    status=?,
                    progress=?
                WHERE plex_guid=?
                """,
                (status, progress, plex_guid),
            )
        await conn.commit()


async def reset_scan_job(plex_guid: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE scan_jobs
            SET
                status='pending',
                progress=0,
                started_at=NULL,
                finished_at=NULL,
                error_msg=NULL,
                cancel_requested=0,
                queue_state='idle',
                queue_position=NULL,
                queue_priority=0,
                queue_reason='',
                queued_at=NULL,
                queue_updated_at=CURRENT_TIMESTAMP
            WHERE plex_guid=?
            """,
            (plex_guid,),
        )
        await conn.commit()


async def set_force_scan(plex_guid: str, force: bool) -> None:
    """Set or unset the force_scan flag for a specific job."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET force_scan=? WHERE plex_guid=?",
            (1 if force else 0, plex_guid),
        )
        await conn.commit()


async def set_ignored(plex_guid: str, ignored: bool) -> None:
    """Set or unset the ignored flag for a specific job."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET ignored=? WHERE plex_guid=?",
            (1 if ignored else 0, plex_guid),
        )
        await conn.commit()


async def request_scan_cancel(plex_guid: str) -> bool:
    """Mark a job for cancellation and return whether a row was updated."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE scan_jobs
            SET cancel_requested=1, queue_updated_at=CURRENT_TIMESTAMP
            WHERE plex_guid=?
            """,
            (plex_guid,),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def clear_scan_cancel_requested(plex_guid: str) -> None:
    """Clear any previously requested scan cancellation flag."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET cancel_requested=0 WHERE plex_guid=?",
            (plex_guid,),
        )
        await conn.commit()


async def get_scan_jobs_by_guids(guids: list[str]) -> dict[str, dict]:
    """Return {plex_guid: job} for all provided guids in a single IN query."""
    if not guids:
        return {}
    placeholders = ",".join(["?"] * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM scan_jobs WHERE plex_guid IN ({placeholders})",
            guids,
        )
        return {row["plex_guid"]: dict(row) for row in rows}


async def get_scan_jobs_by_library(library_id: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM scan_jobs WHERE library_id=? ORDER BY title", (library_id,)
        )
        return [dict(r) for r in rows]


async def get_segment_counts_for_library(library_id: str) -> dict[str, int]:
    """Return {plex_guid: segment_count} for all titles in a library (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT s.plex_guid, COUNT(*) as cnt
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.library_id = ?
            GROUP BY s.plex_guid
            """,
            (library_id,),
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


async def get_media_scan_statuses_for_media(media_id: str) -> list[dict]:
    """Return per-category scan status records for a single media item."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT media_id, category, status, source, detail, segment_count, progress,
                   created_at, updated_at
            """
            "FROM media_scan_status WHERE media_id=? ORDER BY category",
            (media_id,),
        )
        return [dict(row) for row in rows]


async def get_media_scan_statuses(media_ids: list[str]) -> dict[str, list[dict]]:
    """Return {media_id: [status rows]} for all requested media items."""
    if not media_ids:
        return {}
    placeholders = ",".join("?" * len(media_ids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"""
            SELECT media_id, category, status, source, detail, segment_count, progress,
                   created_at, updated_at
            FROM media_scan_status
            WHERE media_id IN ({placeholders})
            ORDER BY media_id, category
            """,
            media_ids,
        )
        grouped: dict[str, list[dict]] = {media_id: [] for media_id in media_ids}
        for row in rows:
            grouped.setdefault(row["media_id"], []).append(dict(row))
        return grouped


async def upsert_media_scan_status(
    media_id: str,
    category: str,
    status: str,
    *,
    source: str = "",
    detail: str = "",
    segment_count: int = 0,
    progress: float = 0.0,
) -> None:
    """Insert or update category scan status for a title."""
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO media_scan_status(
                media_id, category, status, source, detail, segment_count, progress
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id, category)
            DO UPDATE SET
                status=excluded.status,
                source=excluded.source,
                detail=excluded.detail,
                segment_count=excluded.segment_count,
                progress=excluded.progress,
                updated_at=CURRENT_TIMESTAMP
            """,
            (media_id, category, status, source, detail, segment_count, progress),
        )
        await conn.commit()


async def seed_media_scan_statuses(media_id: str) -> None:
    """Ensure every supported category has an initial scan-status record."""
    async with get_connection() as conn:
        for category in SUPPORTED_CATEGORIES:
            await conn.execute(
                """
                INSERT OR IGNORE INTO media_scan_status(
                    media_id, category, status, source, detail, segment_count, progress
                )
                VALUES(?, ?, 'pending', '', '', 0, 0)
                """,
                (media_id, category),
            )
        await conn.commit()


async def reset_media_scan_state(media_id: str) -> None:
    """Reset per-title category and stage status before a fresh scan."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM media_scan_stage_status WHERE media_id=?",
            (media_id,),
        )
        for category in SUPPORTED_CATEGORIES:
            await conn.execute(
                """
                INSERT INTO media_scan_status(
                    media_id, category, status, source, detail, segment_count, progress
                )
                VALUES(?, ?, 'pending', '', '', 0, 0)
                ON CONFLICT(media_id, category)
                DO UPDATE SET
                    status='pending',
                    source='',
                    detail='',
                    segment_count=0,
                    progress=0,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (media_id, category),
            )
        await conn.commit()


async def get_media_scan_stage_statuses_for_media(media_id: str) -> list[dict[str, Any]]:
    """Return ordered scan-stage rows for one media item."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT media_id, stage_key, category, stage_order, status, source, detail, progress,
                   created_at, updated_at
            FROM media_scan_stage_status
            WHERE media_id=?
            ORDER BY stage_order, stage_key
            """,
            (media_id,),
        )
        return [dict(row) for row in rows]


async def upsert_media_scan_stage_status(
    media_id: str,
    stage_key: str,
    status: str,
    *,
    category: str | None = None,
    source: str = "",
    detail: str = "",
    progress: float = 0.0,
) -> None:
    """Insert or update one ordered scan-stage status row."""
    stage_order = int(SCAN_STAGE_ORDER.get(stage_key, 999))
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO media_scan_stage_status(
                media_id, stage_key, category, stage_order, status, source, detail, progress
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id, stage_key)
            DO UPDATE SET
                category=excluded.category,
                stage_order=excluded.stage_order,
                status=excluded.status,
                source=excluded.source,
                detail=excluded.detail,
                progress=excluded.progress,
                updated_at=CURRENT_TIMESTAMP
            """,
            (media_id, stage_key, category, stage_order, status, source, detail, progress),
        )
        await conn.commit()


# ── Segment Library Sharing ────────────────────────────────────────────────────

async def upsert_segment_library_entry(
    file_hash: str,
    file_name: str,
    file_size: int,
    duration_ms: int,
    segments_json: str,
    source_instance: str,
    confidence_level: str = "local",
) -> int:
    """Insert or update a segment library entry with source tracking."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO segment_library_entries(
                file_hash, file_name, file_size, duration_ms, 
                segments_json, source_instance, confidence_level, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(file_hash, source_instance) 
            DO UPDATE SET 
                segments_json=excluded.segments_json,
                confidence_level=excluded.confidence_level,
                updated_at=CURRENT_TIMESTAMP
            """,
            (file_hash, file_name, file_size, duration_ms, segments_json, source_instance, confidence_level),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_segment_library_entries_by_hashes(file_hashes: list[str]) -> list[dict]:
    """Get all library entries matching the given file hashes, grouped by hash."""
    if not file_hashes:
        return []
    
    placeholders = ",".join(["?"] * len(file_hashes))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM segment_library_entries WHERE file_hash IN ({placeholders}) ORDER BY file_hash, created_at DESC",
            file_hashes,
        )
        return [dict(r) for r in rows]


async def get_segment_library_entries_by_hash(file_hash: str) -> list[dict]:
    """Get all sources for a single file hash."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segment_library_entries WHERE file_hash=? ORDER BY created_at DESC",
            (file_hash,),
        )
        return [dict(r) for r in rows]


async def delete_segment_library_entry(file_hash: str, source_instance: str) -> bool:
    """Delete a specific entry (useful for removing outdated sources)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM segment_library_entries WHERE file_hash=? AND source_instance=?",
            (file_hash, source_instance),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_sync_metadata() -> dict | None:
    """Get sync configuration for this instance."""
    async with get_connection() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM sync_metadata LIMIT 1"
            )
        ).fetchone()
        return dict(row) if row else None


async def upsert_sync_metadata(
    instance_name: str,
    github_repo: str | None = None,
    github_token: str | None = None,
    sync_enabled: bool = False,
    conflict_resolution: str = "consensus",
    verified_threshold: int = 2,
    timing_tolerance_ms: int = 2000,
) -> int:
    """Update or create sync metadata for this instance."""
    async with get_connection() as conn:
        # Delete existing if it exists (to update)
        await conn.execute("DELETE FROM sync_metadata")
        
        cursor = await conn.execute(
            """
            INSERT INTO sync_metadata(
                instance_name, github_repo, github_token, sync_enabled,
                conflict_resolution, verified_threshold, timing_tolerance_ms,
                last_sync_time
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                instance_name,
                github_repo,
                github_token,
                1 if sync_enabled else 0,
                conflict_resolution,
                verified_threshold,
                timing_tolerance_ms,
            ),
        )
        await conn.commit()
        return cursor.lastrowid


async def update_sync_last_time() -> None:
    """Update last_sync_time to current timestamp."""
    async with get_connection() as conn:
        await conn.execute("UPDATE sync_metadata SET last_sync_time=CURRENT_TIMESTAMP")
        await conn.commit()


# ── Background Job Management ──────────────────────────────────────────────────

async def create_bg_job(job_type: str) -> int:
    """Create a new background job. Returns job_id."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO bg_jobs(job_type, status, started_at) VALUES(?, ?, CURRENT_TIMESTAMP)",
            (job_type, 'running'),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_bg_job(job_id: int) -> dict | None:
    """Get background job status."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM bg_jobs WHERE id = ?", (job_id,))).fetchone()
        return dict(row) if row else None


async def update_bg_job(job_id: int, status: str = None, progress: int = None, error: str = None, result: str = None) -> None:
    """Update background job status."""
    async with get_connection() as conn:
        updates = []
        params = []
        
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            if status == 'completed':
                updates.append("completed_at = CURRENT_TIMESTAMP")
        
        if progress is not None:
            updates.append("progress_percent = ?")
            params.append(progress)
        
        if error is not None:
            updates.append("error_message = ?")
            params.append(error)
        
        if result is not None:
            updates.append("result_data = ?")
            params.append(result)
        
        if updates:
            params.append(job_id)
            query = f"UPDATE bg_jobs SET {', '.join(updates)} WHERE id = ?"
            await conn.execute(query, params)
            await conn.commit()


async def get_local_library_for_sync() -> list[dict]:
    """Return all locally-scanned titles with their segments, grouped by plex_guid.

    Uses a single JOIN query instead of one query per title to avoid N+1 DB round-trips.
    Returns: [{plex_guid, title, file_path, segments_count, segments: [{...}]}]
    """
    async with get_connection() as conn:
        # Fetch all segment rows for done jobs in a single pass.
        seg_rows = await conn.execute_fetchall(
            """
            SELECT s.*, j.file_path, j.title AS job_title
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.status = 'done' AND j.file_path IS NOT NULL
            ORDER BY j.title, s.start_ms
            """
        )
        # Also fetch done jobs with no segments so they still appear in the result.
        job_rows = await conn.execute_fetchall(
            """
            SELECT plex_guid, title, file_path
            FROM scan_jobs
            WHERE status = 'done' AND file_path IS NOT NULL
            ORDER BY title
            """
        )

    # Build {plex_guid: {title, file_path, segments: []}} in Python — O(1) per row.
    job_map: dict[str, dict] = {
        r["plex_guid"]: {
            "plex_guid": r["plex_guid"],
            "title": r["title"],
            "file_path": r["file_path"],
            "segments": [],
        }
        for r in job_rows
    }
    for row in seg_rows:
        guid = row["plex_guid"]
        if guid in job_map:
            job_map[guid]["segments"].append(dict(row))

    result = []
    for item in job_map.values():
        item["segments_count"] = len(item["segments"])
        result.append(item)
    return result
