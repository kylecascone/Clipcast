"""
database.py
===========
SQLite database layer for ClipCast Studio.

Tracks clips, compiled packages, and the posting queue from discovery
through to successful TikTok post.

SaaS Migration Note:
    This module currently uses the built-in sqlite3 library and stores
    data in a local clipcast.db file. To migrate to PostgreSQL for SaaS:

    1. Replace sqlite3 with psycopg2 (pip install psycopg2-binary) or
       use SQLAlchemy as an abstraction layer (pip install sqlalchemy).
    2. Change DATABASE_PATH to read a DATABASE_URL from environment
       variables:  os.environ["DATABASE_URL"]
    3. Swap "?" query placeholders for "%s" (psycopg2 style) or use
       SQLAlchemy's parameter binding.
    4. Run database schema migrations with Alembic instead of
       CREATE TABLE IF NOT EXISTS.
    5. The user_id column is already on every table — no schema changes
       needed to support multi-tenancy.

Schema overview:
    clips                    — One row per source clip (Twitch, YouTube, or manual).
    packages                 — One row per compiled video (1–3 clips combined).
    posting_queue            — Schedule of packages waiting to be posted.
    performance              — Per-post TikTok analytics snapshots.
    errors                   — Pipeline error log.
    consents                 — First-run ToS acceptance records.
    quota_usage              — Daily YouTube API quota counters.
    audit_log                — Silent pipeline audit trail.
    custom_edits             — Interactive editor sessions (--edit).
    shared_clips             — Global deduplicated clip pool (populated by pool_fetcher).
    shared_pool_runs         — Audit trail of pool refresh runs (for freshness checks).
    shared_clip_reservations — Per-user 48-hour locks on shared clips.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Database location ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATABASE_PATH = BASE_DIR / "clipcast.db"

logger = logging.getLogger(__name__)

# ── Valid status values (kept here as the single source of truth) ──────────────
CLIP_STATUSES = ("queued", "scored", "compiled", "processed", "posted", "skipped")
PACKAGE_STATUSES = ("pending", "compiled", "processed", "posted", "failed")
QUEUE_STATUSES = ("pending", "processing", "posted", "failed", "cancelled")

# Clip permission values — reserved for a future streamer-consent system where
# users can mark which streamers have given explicit permission to clip & repost.
CLIP_PERMISSIONS = ("unknown", "granted", "denied")


# ══════════════════════════════════════════════════════════════════════════════
# Connection helper
# ══════════════════════════════════════════════════════════════════════════════

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Open and return a database connection.

    Rows are returned as sqlite3.Row objects which behave like dicts
    (you can access columns by name: row["title"]).

    Args:
        db_path: Override the default DATABASE_PATH (useful for tests).

    Returns:
        An open sqlite3.Connection.
    """
    path = str(db_path or DATABASE_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row          # Access columns by name
    conn.execute("PRAGMA foreign_keys = ON") # Enforce FK constraints
    conn.execute("PRAGMA journal_mode = WAL") # Better read/write concurrency
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# Database initialization
# ══════════════════════════════════════════════════════════════════════════════

def initialize_database(db_path: Optional[Path] = None) -> None:
    """
    Create all tables, indexes, and triggers if they don't already exist.

    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS so
    existing data is never touched.

    Args:
        db_path: Override the default DATABASE_PATH (useful for tests).
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()

        # ── clips ──────────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                clip_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL DEFAULT 1,
                    -- ↑ Reserved for SaaS multi-user. Always 1 for single-user.
                source          TEXT NOT NULL
                                    CHECK(source IN ('twitch', 'youtube', 'manual')),
                title           TEXT NOT NULL,
                creator_name    TEXT,
                url             TEXT,            -- Original clip URL
                local_path      TEXT,            -- Path after download to clips/raw/
                duration        REAL,            -- Duration in seconds (float)
                score           REAL DEFAULT 0,  -- Quality score 0–100 from scorer.py
                is_solo_worthy  INTEGER DEFAULT 0, -- 1 if clip can stand alone as a post
                template_used   INTEGER,         -- Template number (1–4) applied
                caption_used    INTEGER,         -- Caption style number (1–4) applied
                mode            TEXT NOT NULL
                                    CHECK(mode IN ('auto', 'manual')),
                status          TEXT NOT NULL DEFAULT 'queued'
                                    CHECK(status IN ('queued','scored','compiled',
                                                     'processed','posted','skipped')),
                has_music       INTEGER NOT NULL DEFAULT 0,
                    -- 1 if music_detector.py flagged this clip. DMCA risk if 1.
                permissions     TEXT NOT NULL DEFAULT 'unknown',
                    -- Reserved for future streamer-consent system.
                    -- Values: 'unknown' | 'granted' | 'denied'
                post_date       TEXT,            -- ISO 8601 datetime when posted
                tiktok_post_id  TEXT,            -- TikTok's returned post ID
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── packages ───────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                package_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL DEFAULT 1,
                clip_ids        TEXT NOT NULL,   -- JSON array: [1, 2, 3]
                template        INTEGER NOT NULL, -- Template number (1–4)
                caption_style   INTEGER NOT NULL, -- Caption style number (1–4)
                caption_text    TEXT,            -- Final TikTok caption string
                mode            TEXT NOT NULL
                                    CHECK(mode IN ('auto', 'manual')),
                status          TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending','compiled','processed',
                                                     'posted','failed')),
                compiled_path   TEXT,            -- Path to final MP4 in clips/processed/
                preview_pending INTEGER NOT NULL DEFAULT 0,
                    -- 1 = awaiting user approval before queuing (clip_preview_required=true)
                tiktok_post_id  TEXT,
                yt_shorts_post_id   TEXT,        -- YouTube Shorts video ID after upload
                instagram_post_id   TEXT,        -- Instagram Reel media ID after upload
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── posting_queue ──────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS posting_queue (
                queue_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL DEFAULT 1,
                package_id          INTEGER NOT NULL
                                        REFERENCES packages(package_id),
                scheduled_time      TEXT NOT NULL,   -- ISO 8601 datetime
                posted_at           TEXT,            -- ISO 8601 datetime (set on success)
                status              TEXT NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending','processing',
                                                         'posted','failed','cancelled')),
                platform            TEXT DEFAULT 'tiktok', -- 'tiktok'|'youtube_shorts'|'instagram_reels'
                template_used       INTEGER,   -- 1–4; template applied to this post
                caption_style_used  INTEGER    -- 1–4; caption style applied to this post
            )
        """)

        # ── performance ────────────────────────────────────────────────────────
        # Stores per-platform analytics pulled from the posting API.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS performance (
                perf_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL DEFAULT 1,
                package_id      INTEGER NOT NULL
                                    REFERENCES packages(package_id),
                platform        TEXT NOT NULL,   -- 'tiktok' | 'youtube_shorts' | 'instagram_reels'
                post_id         TEXT,            -- Platform post/video ID
                view_count      INTEGER DEFAULT 0,
                like_count      INTEGER DEFAULT 0,
                comment_count   INTEGER DEFAULT 0,
                share_count     INTEGER DEFAULT 0,
                fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── errors ─────────────────────────────────────────────────────────────
        # Stores pipeline errors for the --errors CLI flag and dashboard alerts.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS errors (
                error_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL DEFAULT 1,
                step            TEXT,            -- 'fetch' | 'edit' | 'upload' | 'other'
                message         TEXT NOT NULL,
                package_id      INTEGER,         -- NULL if not package-specific
                occurred_at     TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── consents ───────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS consents (
                consent_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL DEFAULT 1,
                version     TEXT    NOT NULL DEFAULT '1.0',
                agreed_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── quota_usage ────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quota_usage (
                quota_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL DEFAULT 1,
                service     TEXT    NOT NULL,  -- 'youtube' | 'tiktok_posts'
                date        TEXT    NOT NULL,  -- YYYY-MM-DD
                units_used  INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_service_date "
            "ON quota_usage(user_id, service, date)"
        )

        # ── audit_log ──────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL DEFAULT 1,
                action      TEXT    NOT NULL,  -- 'fetch'|'process'|'post'|'blocked'|'attribution'|'quota'
                detail      TEXT,
                source      TEXT,              -- 'twitch'|'youtube'|'tiktok'|etc.
                package_id  INTEGER,
                clip_id     INTEGER,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_created "
            "ON audit_log(user_id, created_at)"
        )

        # ── indexes ────────────────────────────────────────────────────────────
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clips_user_status "
                    "ON clips(user_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clips_url "
                    "ON clips(url)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_packages_status "
                    "ON packages(user_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_queue_scheduled "
                    "ON posting_queue(user_id, status, scheduled_time)")

        # ── custom_edits ───────────────────────────────────────────────────────
        # Stores interactive editor sessions created via `python main.py --edit`.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS custom_edits (
                edit_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL DEFAULT 1,
                clip_path       TEXT    NOT NULL,  -- input clip path
                output_path     TEXT,              -- final export path
                operations_json TEXT    NOT NULL DEFAULT '[]',
                    -- JSON array of applied operations (trim, crop, caption, etc.)
                template        INTEGER DEFAULT 1,
                status          TEXT    NOT NULL DEFAULT 'draft'
                                    CHECK(status IN ('draft','exported','queued','posted')),
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_custom_edits_user "
            "ON custom_edits(user_id, status)"
        )

        # ── shared_clips ───────────────────────────────────────────────────────
        # Global deduplicated clip pool populated by pool_fetcher.py once per
        # refresh cycle. All users draw from this table instead of fetching
        # individually, keeping YouTube quota consumption to ≤4 calls/day.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_clips (
                shared_clip_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id         TEXT    UNIQUE NOT NULL,
                source          TEXT    NOT NULL CHECK(source IN ('twitch','youtube')),
                creator_name    TEXT,
                title           TEXT    NOT NULL DEFAULT 'Untitled',
                url             TEXT    NOT NULL,
                duration_sec    REAL,
                view_count      INTEGER DEFAULT 0,
                score           REAL    DEFAULT 0,
                has_music       INTEGER DEFAULT 0,
                is_blocked      INTEGER DEFAULT 0,
                fetched_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                expires_at      TEXT    NOT NULL,
                region          TEXT    NOT NULL DEFAULT 'global'
            )
        """)

        # ── shared_pool_runs ───────────────────────────────────────────────────
        # Audit trail of each pool refresh run. check_pool_freshness() queries
        # this table to decide whether a new refresh is needed.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_pool_runs (
                run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT    NOT NULL,
                started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                completed_at    TEXT,
                clips_added     INTEGER DEFAULT 0,
                clips_expired   INTEGER DEFAULT 0,
                status          TEXT    NOT NULL DEFAULT 'running'
            )
        """)

        # ── shared_clip_reservations ───────────────────────────────────────────
        # Two-tier reservation table:
        #   Tier 1 (viral, view_count ≥ 10000): Multiple users may reserve the
        #     same clip, but each user gets a unique template+caption_style combo
        #     so every post looks different. template_used and caption_style_used
        #     track which presentation has been assigned.
        #   Tier 2 (regular, view_count < 10000): Exclusive 48-hour lock.
        #     template_used and caption_style_used are NULL for these rows.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_clip_reservations (
                reservation_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                shared_clip_id      INTEGER NOT NULL
                                        REFERENCES shared_clips(shared_clip_id),
                user_id             INTEGER NOT NULL DEFAULT 1,
                reserved_at         TEXT    NOT NULL DEFAULT (datetime('now')),
                expires_at          TEXT    NOT NULL,
                template_used       INTEGER,   -- 1–4; NULL for regular-tier reservations
                caption_style_used  INTEGER    -- 1–4; NULL for regular-tier reservations
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_clips_score "
            "ON shared_clips(score DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_clips_expires "
            "ON shared_clips(source, expires_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_reservations_user "
            "ON shared_clip_reservations(user_id, shared_clip_id, expires_at)"
        )

        # ── auto-update triggers ───────────────────────────────────────────────
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_clips_updated_at
            AFTER UPDATE ON clips
            BEGIN
                UPDATE clips SET updated_at = datetime('now')
                WHERE clip_id = NEW.clip_id;
            END
        """)
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_packages_updated_at
            AFTER UPDATE ON packages
            BEGIN
                UPDATE packages SET updated_at = datetime('now')
                WHERE package_id = NEW.package_id;
            END
        """)
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_custom_edits_updated_at
            AFTER UPDATE ON custom_edits
            BEGIN
                UPDATE custom_edits SET updated_at = datetime('now')
                WHERE edit_id = NEW.edit_id;
            END
        """)

        conn.commit()
        _run_migrations(conn)
        logger.info("Database initialized at %s", db_path or DATABASE_PATH)

    finally:
        conn.close()

    # One-time data fixes — fast no-ops when already complete
    backfill_missing_scores(db_path=db_path)
    remove_unsafe_existing_clips(db_path=db_path)
    remove_non_english_clips(db_path=db_path)


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    Apply incremental schema migrations for existing databases.

    Uses try/except around each ALTER TABLE so it is safe to call on both
    fresh and pre-existing databases. Silently skips columns that already exist.
    """
    migrations = [
        # clips additions
        "ALTER TABLE clips ADD COLUMN has_music INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE clips ADD COLUMN permissions TEXT NOT NULL DEFAULT 'unknown'",
        # packages additions
        "ALTER TABLE packages ADD COLUMN preview_pending INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE packages ADD COLUMN yt_shorts_post_id TEXT",
        "ALTER TABLE packages ADD COLUMN instagram_post_id TEXT",
        # shared_clip_reservations additions (two-tier viral presentation tracking)
        "ALTER TABLE shared_clip_reservations ADD COLUMN template_used INTEGER",
        "ALTER TABLE shared_clip_reservations ADD COLUMN caption_style_used INTEGER",
        # posting_queue additions (for performance_learner feedback loop)
        "ALTER TABLE posting_queue ADD COLUMN platform TEXT DEFAULT 'tiktok'",
        "ALTER TABLE posting_queue ADD COLUMN template_used INTEGER",
        "ALTER TABLE posting_queue ADD COLUMN caption_style_used INTEGER",
        # shared_clips additions (category for layout routing)
        "ALTER TABLE shared_clips ADD COLUMN category TEXT DEFAULT ''",
        # shared_clips additions (thumbnail + hashtags)
        "ALTER TABLE shared_clips ADD COLUMN thumbnail_path TEXT",
        "ALTER TABLE shared_clips ADD COLUMN hashtags_tiktok TEXT",
        "ALTER TABLE shared_clips ADD COLUMN hashtags_youtube TEXT",
        "ALTER TABLE shared_clips ADD COLUMN hashtags_instagram TEXT",
        # content category for schedule-based filtering (Phase 2)
        "ALTER TABLE shared_clips ADD COLUMN content_category TEXT DEFAULT ''",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists — skip


# ══════════════════════════════════════════════════════════════════════════════
# Clip operations
# ══════════════════════════════════════════════════════════════════════════════

def insert_clip(clip_data: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """
    Insert a new clip record.

    Required keys in clip_data:
        source (str), title (str), mode (str)

    Optional keys:
        user_id, creator_name, url, local_path, duration, score,
        is_solo_worthy, template_used, caption_used, status,
        post_date, tiktok_post_id

    Returns:
        The new clip_id (int).
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO clips
                (user_id, source, title, creator_name, url, local_path,
                 duration, score, is_solo_worthy, template_used, caption_used,
                 mode, status, post_date, tiktok_post_id)
            VALUES
                (:user_id, :source, :title, :creator_name, :url, :local_path,
                 :duration, :score, :is_solo_worthy, :template_used, :caption_used,
                 :mode, :status, :post_date, :tiktok_post_id)
        """, {
            "user_id":         clip_data.get("user_id", 1),
            "source":          clip_data["source"],
            "title":           clip_data["title"],
            "creator_name":    clip_data.get("creator_name"),
            "url":             clip_data.get("url"),
            "local_path":      clip_data.get("local_path"),
            "duration":        clip_data.get("duration"),
            "score":           clip_data.get("score", 0),
            "is_solo_worthy":  int(clip_data.get("is_solo_worthy", False)),
            "template_used":   clip_data.get("template_used"),
            "caption_used":    clip_data.get("caption_used"),
            "mode":            clip_data.get("mode", "auto"),
            "status":          clip_data.get("status", "queued"),
            "post_date":       clip_data.get("post_date"),
            "tiktok_post_id":  clip_data.get("tiktok_post_id"),
        })
        conn.commit()
        clip_id = cur.lastrowid
        if not clip_id:
            # Row already existed (INSERT OR IGNORE skipped) — fetch existing ID
            row = cur.execute(
                "SELECT clip_id FROM clips WHERE url = ?", (clip_data.get("url"),)
            ).fetchone()
            clip_id = row["clip_id"] if row else 0
            logger.debug("Clip already exists, reusing clip_id=%d  url='%s'", clip_id, clip_data.get("url"))
        else:
            logger.debug("Inserted clip_id=%d  title='%s'", clip_id, clip_data["title"])
        return clip_id
    finally:
        conn.close()


def get_clip(clip_id: int, db_path: Optional[Path] = None) -> Optional[Dict]:
    """
    Fetch a single clip by its ID.

    Returns:
        Dict representation of the row, or None if not found.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM clips WHERE clip_id = ?", (clip_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_clips_by_status(
    status: str,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """
    Return all clips with the given status for a user, ordered by score desc.

    Args:
        status: One of CLIP_STATUSES.
        user_id: User filter (default 1).

    Returns:
        List of clip dicts.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM clips WHERE status = ? AND user_id = ? ORDER BY score DESC",
            (status, user_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clip_url_exists(url: str, db_path: Optional[Path] = None) -> bool:
    """
    Return True if a non-skipped clip with this URL already exists in the DB.
    Used to avoid re-processing the same clip in automated mode.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT clip_id FROM clips WHERE url = ? AND status != 'skipped'",
            (url,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def update_clip_status(
    clip_id: int,
    status: str,
    db_path: Optional[Path] = None,
) -> None:
    """Update the status field of a clip."""
    _update_clip_field(clip_id, "status", status, db_path)


def update_clip_field(
    clip_id: int,
    field: str,
    value: Any,
    db_path: Optional[Path] = None,
) -> None:
    """
    Update a single writable field on a clip.

    Allowed fields: local_path, score, status, is_solo_worthy,
                    template_used, caption_used, post_date, tiktok_post_id
    """
    _CLIP_WRITABLE = {
        "local_path", "score", "status", "is_solo_worthy",
        "template_used", "caption_used", "post_date", "tiktok_post_id",
        "has_music", "permissions",
    }
    if field not in _CLIP_WRITABLE:
        raise ValueError(
            f"Field '{field}' is not in the allowed set for update_clip_field: "
            f"{sorted(_CLIP_WRITABLE)}"
        )
    _update_clip_field(clip_id, field, value, db_path)


def _update_clip_field(
    clip_id: int,
    field: str,
    value: Any,
    db_path: Optional[Path] = None,
) -> None:
    """Internal helper — no field whitelist check."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"UPDATE clips SET {field} = ? WHERE clip_id = ?",
            (value, clip_id),
        )
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Package operations
# ══════════════════════════════════════════════════════════════════════════════

def insert_package(
    package_data: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> int:
    """
    Insert a new package record.

    Required keys: clip_ids (list[int]), template (int), caption_style (int), mode (str)
    Optional keys: user_id, caption_text, status, compiled_path

    Returns:
        The new package_id (int).
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO packages
                (user_id, clip_ids, template, caption_style, caption_text,
                 mode, status, compiled_path)
            VALUES
                (:user_id, :clip_ids, :template, :caption_style, :caption_text,
                 :mode, :status, :compiled_path)
        """, {
            "user_id":       package_data.get("user_id", 1),
            "clip_ids":      json.dumps(package_data["clip_ids"]),
            "template":      package_data["template"],
            "caption_style": package_data["caption_style"],
            "caption_text":  package_data.get("caption_text"),
            "mode":          package_data.get("mode", "auto"),
            "status":        package_data.get("status", "pending"),
            "compiled_path": package_data.get("compiled_path"),
        })
        conn.commit()
        pkg_id = cur.lastrowid
        logger.debug("Inserted package_id=%d  clips=%s", pkg_id, package_data["clip_ids"])
        return pkg_id
    finally:
        conn.close()


def get_package(
    package_id: int,
    db_path: Optional[Path] = None,
) -> Optional[Dict]:
    """
    Fetch a package by ID. Deserializes clip_ids from JSON to a Python list.

    Returns:
        Dict with clip_ids as list[int], or None if not found.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM packages WHERE package_id = ?", (package_id,)
        ).fetchone()
        if not row:
            return None
        pkg = dict(row)
        pkg["clip_ids"] = json.loads(pkg["clip_ids"])
        return pkg
    finally:
        conn.close()


def update_package_field(
    package_id: int,
    field: str,
    value: Any,
    db_path: Optional[Path] = None,
) -> None:
    """
    Update a single writable field on a package.

    Allowed fields: status, compiled_path, caption_text, tiktok_post_id
    """
    _PKG_WRITABLE = {
        "status", "compiled_path", "caption_text",
        "tiktok_post_id", "yt_shorts_post_id", "instagram_post_id",
        "preview_pending",
    }
    if field not in _PKG_WRITABLE:
        raise ValueError(
            f"Field '{field}' is not in the allowed set: {sorted(_PKG_WRITABLE)}"
        )
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"UPDATE packages SET {field} = ? WHERE package_id = ?",
            (value, package_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_packages_by_status(
    status: str,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """Return all packages with the given status for a user."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM packages WHERE status = ? AND user_id = ? ORDER BY created_at ASC",
            (status, user_id),
        ).fetchall()
        result = []
        for row in rows:
            pkg = dict(row)
            pkg["clip_ids"] = json.loads(pkg["clip_ids"])
            result.append(pkg)
        return result
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Posting queue operations
# ══════════════════════════════════════════════════════════════════════════════

def enqueue_package(
    package_id: int,
    scheduled_time: datetime,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> int:
    """
    Add a package to the posting queue at a specific scheduled time.

    Args:
        package_id: ID of the package to schedule.
        scheduled_time: When to post (datetime object).
        user_id: User ID (default 1).

    Returns:
        The new queue_id (int).
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO posting_queue (user_id, package_id, scheduled_time, status) "
            "VALUES (?, ?, ?, 'pending')",
            (user_id, package_id, scheduled_time.isoformat()),
        )
        conn.commit()
        qid = cur.lastrowid
        logger.debug(
            "Enqueued package_id=%d at %s (queue_id=%d)",
            package_id, scheduled_time.isoformat(), qid,
        )
        return qid
    finally:
        conn.close()


def get_pending_queue(
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """
    Return all pending queue items joined with their package info,
    ordered soonest first.

    Returns:
        List of dicts with queue + package fields. clip_ids is a Python list.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT
                q.queue_id,
                q.package_id,
                q.scheduled_time,
                q.status          AS queue_status,
                p.compiled_path,
                p.caption_text,
                p.template,
                p.caption_style,
                p.clip_ids,
                p.mode
            FROM posting_queue q
            JOIN packages p ON q.package_id = p.package_id
            WHERE q.user_id = ? AND q.status = 'pending'
            ORDER BY q.scheduled_time ASC
        """, (user_id,)).fetchall()

        result = []
        for row in rows:
            item = dict(row)
            item["clip_ids"] = json.loads(item["clip_ids"])
            result.append(item)
        return result
    finally:
        conn.close()


def update_queue_status(
    queue_id: int,
    status: str,
    posted_at: Optional[datetime] = None,
    db_path: Optional[Path] = None,
) -> None:
    """
    Update the status of a queue item. Optionally record the posted_at timestamp.

    Args:
        queue_id: The queue row to update.
        status: New status string (one of QUEUE_STATUSES).
        posted_at: Timestamp to write when marking as posted.
    """
    conn = get_connection(db_path)
    try:
        if posted_at:
            conn.execute(
                "UPDATE posting_queue SET status = ?, posted_at = ? WHERE queue_id = ?",
                (status, posted_at.isoformat(), queue_id),
            )
        else:
            conn.execute(
                "UPDATE posting_queue SET status = ? WHERE queue_id = ?",
                (status, queue_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_today_post_count(
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> int:
    """Return how many posts have been successfully made today."""
    conn = get_connection(db_path)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT COUNT(*) AS cnt
            FROM posting_queue
            WHERE user_id = ? AND status = 'posted' AND posted_at LIKE ?
        """, (user_id, f"{today}%")).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def get_last_post_time(
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Return the ISO 8601 posted_at string of the most recent successful post."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("""
            SELECT posted_at
            FROM posting_queue
            WHERE user_id = ? AND status = 'posted'
            ORDER BY posted_at DESC
            LIMIT 1
        """, (user_id,)).fetchone()
        return row["posted_at"] if row else None
    finally:
        conn.close()


def get_next_scheduled_post(
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Return the scheduled_time of the next pending post."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("""
            SELECT scheduled_time
            FROM posting_queue
            WHERE user_id = ? AND status = 'pending'
              AND scheduled_time > datetime('now')
            ORDER BY scheduled_time ASC
            LIMIT 1
        """, (user_id,)).fetchone()
        return row["scheduled_time"] if row else None
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Error logging
# ══════════════════════════════════════════════════════════════════════════════

def log_error(
    message: str,
    step: str = "other",
    package_id: Optional[int] = None,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> None:
    """
    Record a pipeline error to the errors table.

    Args:
        message: Human-readable error description.
        step: Pipeline stage — 'fetch' | 'edit' | 'upload' | 'other'.
        package_id: Associated package if applicable (None otherwise).
        user_id: User ID (default 1).
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO errors (user_id, step, message, package_id) VALUES (?, ?, ?, ?)",
            (user_id, step, message, package_id),
        )
        conn.commit()
        logger.debug("Error logged: [%s] %s", step, message[:80])
    except Exception as e:
        # Never let error logging itself crash the pipeline
        logger.warning("Failed to log error to database: %s", e)
    finally:
        conn.close()


def get_recent_errors(
    limit: int = 20,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """Return the most recent pipeline errors, newest first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM errors WHERE user_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Preview / approval
# ══════════════════════════════════════════════════════════════════════════════

def get_pending_approval_packages(
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """
    Return all processed packages waiting for user approval before queuing.
    These are packages where preview_pending=1.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT * FROM packages
            WHERE user_id = ? AND status = 'processed' AND preview_pending = 1
            ORDER BY created_at ASC
        """, (user_id,)).fetchall()
        result = []
        for row in rows:
            pkg = dict(row)
            pkg["clip_ids"] = json.loads(pkg["clip_ids"])
            result.append(pkg)
        return result
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Performance analytics
# ══════════════════════════════════════════════════════════════════════════════

def upsert_performance(
    package_id: int,
    platform: str,
    post_id: str,
    view_count: int,
    like_count: int,
    comment_count: int,
    share_count: int,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> None:
    """
    Insert or update a performance row for a package + platform combination.
    Always inserts a new row (snapshot history).
    """
    conn = get_connection(db_path)
    try:
        conn.execute("""
            INSERT INTO performance
                (user_id, package_id, platform, post_id,
                 view_count, like_count, comment_count, share_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, package_id, platform, post_id,
              view_count, like_count, comment_count, share_count))
        conn.commit()
    finally:
        conn.close()


def get_performance_summary(
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """
    Return the latest performance snapshot per (package_id, platform),
    sorted by view_count descending.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT p.perf_id, p.package_id, p.platform, p.post_id,
                   p.view_count, p.like_count, p.comment_count, p.share_count,
                   p.fetched_at, pkg.caption_text, pkg.compiled_path
            FROM performance p
            JOIN packages pkg ON p.package_id = pkg.package_id
            WHERE p.user_id = ?
              AND p.perf_id IN (
                  SELECT MAX(perf_id)
                  FROM performance
                  WHERE user_id = ?
                  GROUP BY package_id, platform
              )
            ORDER BY p.view_count DESC
        """, (user_id, user_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Consent
# ══════════════════════════════════════════════════════════════════════════════

def record_consent(
    user_id: int = 1,
    version: str = "1.0",
    db_path: Optional[Path] = None,
) -> None:
    """Record that a user has accepted the Terms of Service."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO consents (user_id, version) VALUES (?, ?)",
            (user_id, version),
        )
        conn.commit()
        logger.info("Consent recorded: user_id=%d version=%s", user_id, version)
    finally:
        conn.close()


def has_consented(
    user_id: int = 1,
    version: str = "1.0",
    db_path: Optional[Path] = None,
) -> bool:
    """Return True if the user has consented to the given Terms version."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM consents WHERE user_id = ? AND version = ? LIMIT 1",
            (user_id, version),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Quota usage
# ══════════════════════════════════════════════════════════════════════════════

def add_quota_usage(
    service: str,
    units: int,
    date_str: Optional[str] = None,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> None:
    """
    Increment the daily quota counter for a service.

    Args:
        service:  Service name ('youtube', 'tiktok_posts').
        units:    Units to add.
        date_str: Date in YYYY-MM-DD format (defaults to today).
        user_id:  User ID.
    """
    from datetime import date as _date
    today = date_str or _date.today().isoformat()
    conn = get_connection(db_path)
    try:
        # Try to update existing row for today
        rows = conn.execute(
            "UPDATE quota_usage SET units_used = units_used + ? "
            "WHERE user_id = ? AND service = ? AND date = ?",
            (units, user_id, service, today),
        ).rowcount
        if rows == 0:
            conn.execute(
                "INSERT INTO quota_usage (user_id, service, date, units_used) VALUES (?, ?, ?, ?)",
                (user_id, service, today, units),
            )
        conn.commit()
    finally:
        conn.close()


def get_daily_quota(
    service: str,
    date_str: Optional[str] = None,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> int:
    """
    Return total units used for a service on a given day.

    Args:
        service:  Service name ('youtube', 'tiktok_posts').
        date_str: Date in YYYY-MM-DD format (defaults to today).
        user_id:  User ID.

    Returns:
        Total units used (0 if none recorded).
    """
    from datetime import date as _date
    today = date_str or _date.today().isoformat()
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(units_used), 0) FROM quota_usage "
            "WHERE user_id = ? AND service = ? AND date = ?",
            (user_id, service, today),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Audit log
# ══════════════════════════════════════════════════════════════════════════════

def log_audit(
    action: str,
    detail: Optional[str] = None,
    source: Optional[str] = None,
    package_id: Optional[int] = None,
    clip_id: Optional[int] = None,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> None:
    """
    Record an audit event. Never raises — silently ignores any DB errors.

    Args:
        action:     Action type ('fetch', 'process', 'post', 'blocked',
                    'attribution', 'quota', 'fetch_batch').
        detail:     Human-readable detail string.
        source:     Source platform or service.
        package_id: Associated package ID if applicable.
        clip_id:    Associated clip ID if applicable.
        user_id:    User ID.
    """
    try:
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO audit_log "
                "(user_id, action, detail, source, package_id, clip_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, action, detail, source, package_id, clip_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Audit logging must never crash the pipeline
        logger.debug("audit log write failed (non-fatal): %s", e)


def get_audit_logs(
    days: int = 30,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """
    Return audit log entries from the last N days, newest first.

    Args:
        days:    How many days back to look.
        user_id: User ID.

    Returns:
        List of audit log dicts.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log "
            "WHERE user_id = ? "
            "  AND created_at >= datetime('now', ? || ' days') "
            "ORDER BY created_at DESC",
            (user_id, f"-{days}"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Custom editor sessions
# ══════════════════════════════════════════════════════════════════════════════

def insert_custom_edit(
    clip_path: str,
    user_id: int = 1,
    template: int = 1,
    operations: Optional[List] = None,
    db_path: Optional[Path] = None,
) -> int:
    """
    Create a new custom editor session row.

    Returns:
        The new edit_id (int).
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO custom_edits (user_id, clip_path, template, operations_json)
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            clip_path,
            template,
            json.dumps(operations or []),
        ))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_custom_edit(
    edit_id: int,
    operations: Optional[List] = None,
    output_path: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Update an existing custom editor session."""
    conn = get_connection(db_path)
    try:
        if operations is not None:
            conn.execute(
                "UPDATE custom_edits SET operations_json = ? WHERE edit_id = ?",
                (json.dumps(operations), edit_id),
            )
        if output_path is not None:
            conn.execute(
                "UPDATE custom_edits SET output_path = ? WHERE edit_id = ?",
                (output_path, edit_id),
            )
        if status is not None:
            conn.execute(
                "UPDATE custom_edits SET status = ? WHERE edit_id = ?",
                (status, edit_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_custom_edit(edit_id: int, db_path: Optional[Path] = None) -> Optional[Dict]:
    """Fetch a custom edit session by ID."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM custom_edits WHERE edit_id = ?", (edit_id,)
        ).fetchone()
        if not row:
            return None
        edit = dict(row)
        edit["operations"] = json.loads(edit.get("operations_json", "[]"))
        return edit
    finally:
        conn.close()


def get_custom_edits_by_status(
    status: str,
    user_id: int = 1,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """Return all custom edit sessions with the given status."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM custom_edits WHERE status = ? AND user_id = ? "
            "ORDER BY created_at DESC",
            (status, user_id),
        ).fetchall()
        result = []
        for row in rows:
            edit = dict(row)
            edit["operations"] = json.loads(edit.get("operations_json", "[]"))
            result.append(edit)
        return result
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# One-time data fixes (called from initialize_database on every startup;
# fast no-ops once the work is done)
# ══════════════════════════════════════════════════════════════════════════════

def backfill_missing_scores(db_path: Optional[Path] = None) -> None:
    """
    Compute final_score for any shared_clips row where it is 0 or NULL.

    This handles clips that were inserted before the two-layer quality filter
    existed.  Each clip gets a viral_potential_score from its discovery source
    and engagement metrics; ai_quality_score defaults to 40.0 when absent.

    Fast no-op when all clips already have a score.
    """
    try:
        from scorer import calculate_final_score, calculate_viral_potential_score
    except ImportError:
        return  # scorer not available — skip silently

    conn = get_connection(db_path)
    try:
        unscored = conn.execute(
            "SELECT * FROM shared_clips WHERE final_score = 0.0 OR final_score IS NULL"
        ).fetchall()

        if not unscored:
            return

        print(f"Backfilling scores for {len(unscored)} clips…")
        updated = 0
        for row in unscored:
            clip_dict = dict(row)
            try:
                viral_score = calculate_viral_potential_score(clip_dict)
                clip_dict["score"] = viral_score
                if not clip_dict.get("ai_quality_score"):
                    clip_dict["ai_quality_score"] = 40.0
                final = calculate_final_score(clip_dict)
                conn.execute(
                    "UPDATE shared_clips SET final_score = ?, score = ? "
                    "WHERE shared_clip_id = ?",
                    (final, viral_score, clip_dict["shared_clip_id"]),
                )
                updated += 1
            except Exception as exc:
                logger.debug("backfill_missing_scores: clip %s error: %s",
                             clip_dict.get("shared_clip_id"), exc)

        conn.commit()
        print(f"Backfill complete — {updated} clips scored")
    except Exception as exc:
        logger.warning("backfill_missing_scores error (non-fatal): %s", exc)
    finally:
        conn.close()


def remove_unsafe_existing_clips(db_path: Optional[Path] = None) -> None:
    """
    Delete any shared_clips rows whose title contains content-safety banned terms.

    Runs once on startup after backfill; fast no-op if the pool is already clean.
    """
    try:
        from scorer import content_safety_filter
    except ImportError:
        return

    conn = get_connection(db_path)
    try:
        all_clips = conn.execute(
            "SELECT shared_clip_id, title, viral_title FROM shared_clips"
        ).fetchall()

        removed = 0
        for row in all_clips:
            title = row["viral_title"] or row["title"] or ""
            if not content_safety_filter({"title": title}):
                # Delete FK-referencing reservations first, then the clip
                conn.execute(
                    "DELETE FROM shared_clip_reservations WHERE shared_clip_id = ?",
                    (row["shared_clip_id"],),
                )
                conn.execute(
                    "DELETE FROM shared_clips WHERE shared_clip_id = ?",
                    (row["shared_clip_id"],),
                )
                removed += 1
                logger.info("Safety removed: %s", title[:60])

        if removed:
            conn.commit()
            print(f"Safety cleanup: removed {removed} unsafe clips from pool")
    except Exception as exc:
        logger.warning("remove_unsafe_existing_clips error (non-fatal): %s", exc)
    finally:
        conn.close()


def remove_non_english_clips(db_path: Optional[Path] = None) -> None:
    """
    Delete any shared_clips rows whose title/creator contain non-English content.

    Runs once on startup; fast no-op if the pool is already clean.
    """
    try:
        from pool_fetcher import is_english_content
    except ImportError:
        return

    conn = get_connection(db_path)
    try:
        all_clips = conn.execute(
            "SELECT shared_clip_id, title, creator_name, language FROM shared_clips"
        ).fetchall()

        removed = 0
        for row in all_clips:
            clip_dict = {
                "title":        row["title"] or "",
                "creator_name": row["creator_name"] or "",
                "language":     row["language"] or "",
            }
            if not is_english_content(clip_dict):
                conn.execute(
                    "DELETE FROM shared_clip_reservations WHERE shared_clip_id = ?",
                    (row["shared_clip_id"],),
                )
                conn.execute(
                    "DELETE FROM shared_clips WHERE shared_clip_id = ?",
                    (row["shared_clip_id"],),
                )
                removed += 1
                logger.info("Non-English removed: %s", (row["title"] or "")[:60])

        if removed:
            conn.commit()
            print(f"Language cleanup: removed {removed} non-English clips from pool")
    except Exception as exc:
        logger.warning("remove_non_english_clips error (non-fatal): %s", exc)
    finally:
        conn.close()


# ── Self-test when run directly ────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    test_db = Path("/tmp/clipcast_test.db")
    if test_db.exists():
        test_db.unlink()

    print("Initializing test database...")
    initialize_database(test_db)
    print("✓ Tables created")

    print("\nInserting test clip...")
    clip_id = insert_clip({
        "source": "twitch",
        "title": "Amazing play by TestStreamer",
        "creator_name": "TestStreamer",
        "url": "https://clips.twitch.tv/test123",
        "duration": 45.0,
        "score": 72.5,
        "mode": "auto",
    }, db_path=test_db)
    print(f"✓ Inserted clip_id={clip_id}")

    clip = get_clip(clip_id, db_path=test_db)
    print(f"✓ Fetched clip: '{clip['title']}' — status={clip['status']}, score={clip['score']}")

    update_clip_status(clip_id, "scored", db_path=test_db)
    clip = get_clip(clip_id, db_path=test_db)
    print(f"✓ Updated status → {clip['status']}")

    print("\nInserting test package...")
    pkg_id = insert_package({
        "clip_ids": [clip_id],
        "template": 1,
        "caption_style": 1,
        "caption_text": "Amazing play | #gaming #fyp",
        "mode": "auto",
    }, db_path=test_db)
    print(f"✓ Inserted package_id={pkg_id}")

    pkg = get_package(pkg_id, db_path=test_db)
    print(f"✓ Fetched package: clip_ids={pkg['clip_ids']}, template={pkg['template']}")

    print("\nEnqueueing package...")
    from datetime import timedelta
    queue_id = enqueue_package(pkg_id, datetime.now() + timedelta(hours=1), db_path=test_db)
    print(f"✓ Enqueued as queue_id={queue_id}")

    pending = get_pending_queue(db_path=test_db)
    print(f"✓ Pending queue has {len(pending)} item(s)")

    print("\nAll database tests passed!")
    test_db.unlink()
    print(f"(Test database {test_db} cleaned up)")
