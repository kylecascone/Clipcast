"""
shared_pool.py
==============
Shared clip pool for ClipCast Studio.

Maintains a central SQLite table of recently discovered clips that is
populated once per fetch cycle and drawn from by all users. This prevents
redundant API calls and YouTube quota burn: no matter how many users run
the pipeline, the expensive discovery calls happen at most 4 times per day
for the whole instance.

Tables
------
shared_clips              — Deduplicated clip records with expiry timestamps.
shared_pool_runs          — Audit trail of each fetch run (for freshness checks).
shared_clip_reservations  — Two-tier reservation system:
                            Tier 1 (viral, ≥10k views): multiple users can share
                              the same clip, each assigned a unique template+caption
                              combo so every post looks different.
                            Tier 2 (regular, <10k views): exclusive 48-hour lock;
                              no other user can compile the clip until it expires.

SaaS Note:
    All per-user functions accept user_id (default=1). In single-user mode
    user_id is always 1. Reservations are already scoped per user.

Usage
-----
    import shared_pool
    shared_pool.initialize_shared_pool_tables()
    clips = shared_pool.get_clips_for_user(user_prefs=prefs, user_id=1)
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import database

logger = logging.getLogger(__name__)

# ── Expiry constants ───────────────────────────────────────────────────────────
TWITCH_EXPIRY_DAYS         = 7
YOUTUBE_EXPIRY_DAYS        = 14
DEFAULT_RESERVATION_HOURS  = 48

# ── Viral tier threshold ───────────────────────────────────────────────────────
# Clips at or above this view count are "viral" and may be shared across users,
# each receiving a distinct template+caption_style presentation.
VIRAL_VIEW_THRESHOLD = 10_000

# All 16 possible template × caption_style presentations, in assignment order.
# Cycles through templates first so consecutive users get different looks.
ALL_PRESENTATION_COMBOS: list = [
    (t, c) for t in range(1, 5) for c in range(1, 5)
]


# ══════════════════════════════════════════════════════════════════════════════
# Table initialization
# ══════════════════════════════════════════════════════════════════════════════

def initialize_shared_pool_tables(db_path: Optional[Path] = None) -> None:
    """
    Create all shared-pool tables and indexes if they do not already exist.

    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS so
    existing data is never touched.

    Args:
        db_path: Override the default DATABASE_PATH (useful for tests).
    """
    conn = database.get_connection(db_path)
    try:
        cur = conn.cursor()

        # ── shared_clips ──────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_clips (
                shared_clip_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id         TEXT    UNIQUE NOT NULL,
                source          TEXT    NOT NULL CHECK(source IN ('twitch','youtube','kick')),
                creator_name    TEXT,
                title           TEXT    NOT NULL DEFAULT 'Untitled',
                url             TEXT    NOT NULL,
                duration_sec    REAL,
                view_count      INTEGER DEFAULT 0,
                score           REAL    DEFAULT 0,
                has_music       INTEGER DEFAULT 0,
                is_blocked      INTEGER DEFAULT 0,
                language        TEXT    DEFAULT 'en',
                fetched_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                expires_at      TEXT    NOT NULL,
                region          TEXT    NOT NULL DEFAULT 'global'
            )
        """)

        # ── shared_pool_runs ──────────────────────────────────────────────────
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

        # ── shared_clip_reservations ──────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_clip_reservations (
                reservation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                shared_clip_id  INTEGER NOT NULL
                                    REFERENCES shared_clips(shared_clip_id),
                user_id         INTEGER NOT NULL DEFAULT 1,
                reserved_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                expires_at      TEXT    NOT NULL
            )
        """)

        # ── indexes ───────────────────────────────────────────────────────────
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

        conn.commit()
        logger.debug("Shared pool tables initialized.")

    except Exception as exc:
        logger.error("Failed to initialize shared pool tables: %s", exc)
    finally:
        conn.close()

    # Upgrade any existing database that was created before kick/language support
    _migrate_shared_clips_if_needed(db_path)


# ══════════════════════════════════════════════════════════════════════════════
# Schema migration
# ══════════════════════════════════════════════════════════════════════════════

def _migrate_shared_clips_if_needed(db_path: Optional[Path] = None) -> None:
    """
    Upgrade an older shared_clips table to the current schema.

    Handles two scenarios:
    1. Missing ``language`` column — added via ALTER TABLE (safe, no data loss).
    2. Missing 'kick' in the source CHECK constraint — requires recreating the
       table. Since pool data is ephemeral, the old rows are copied over and
       any that conflict on clip_id are silently ignored.

    Safe to call on every startup (no-ops if already up to date).
    """
    conn = database.get_connection(db_path)
    try:
        cur = conn.cursor()

        # ── 1. Add language column if not present ─────────────────────────────
        try:
            cur.execute("ALTER TABLE shared_clips ADD COLUMN language TEXT DEFAULT 'en'")
            conn.commit()
            logger.info("shared_clips migration: added 'language' column.")
        except Exception:
            pass  # Column already exists — no action needed

        # ── 1b. Add new columns if missing ────────────────────────────────────
        for col_def in [
            ("discovery_source", "TEXT"),
            ("viral_title",      "TEXT"),
            ("theme",            "TEXT"),
            ("ai_quality_score", "REAL DEFAULT 50.0"),
            ("ai_analyzed",      "INTEGER DEFAULT 0"),
            ("final_score",      "REAL DEFAULT 0.0"),
            ("freshness_score",  "REAL DEFAULT 0.5"),
            ("thumbnail_path",   "TEXT"),
        ]:
            try:
                cur.execute(
                    f"ALTER TABLE shared_clips ADD COLUMN {col_def[0]} {col_def[1]}"
                )
                conn.commit()
                logger.info("shared_clips migration: added '%s' column.", col_def[0])
            except Exception:
                pass  # Column already exists

        # ── 2. Check if 'kick' is in the source CHECK constraint ──────────────
        row = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='shared_clips'"
        ).fetchone()
        table_sql = (row[0] if row else "") or ""

        # Also check if shared_clip_reservations still points to _shared_clips_old
        # (left over from a partial migration on an older database).
        res_row = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='shared_clip_reservations'"
        ).fetchone()
        res_sql = (res_row[0] if res_row else "") or ""
        reservations_broken = "_shared_clips_old" in res_sql

        # ── Helper: (re)build shared_clip_reservations with correct FK ────────
        def _fix_reservations():
            conn.execute("DROP TABLE IF EXISTS shared_clip_reservations")
            conn.execute("""
                CREATE TABLE shared_clip_reservations (
                    reservation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    shared_clip_id  INTEGER NOT NULL
                                        REFERENCES shared_clips(shared_clip_id),
                    user_id         INTEGER NOT NULL DEFAULT 1,
                    reserved_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                    expires_at      TEXT    NOT NULL,
                    template_used       INTEGER,
                    caption_style_used  INTEGER
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reservations_user "
                "ON shared_clip_reservations(user_id, shared_clip_id, expires_at)"
            )

        # shared_clips already has 'kick' — only the reservations FK is broken
        if "'kick'" in table_sql and reservations_broken:
            logger.info(
                "shared_clips migration: fixing shared_clip_reservations FK…"
            )
            conn.execute("PRAGMA foreign_keys = OFF")
            _fix_reservations()
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            logger.info("shared_clips migration complete (reservations fixed).")
            return

        if "'kick'" in table_sql:
            return  # Already fully migrated

        # shared_clips needs the CHECK constraint updated (full table recreation).
        # Disable FK enforcement during the swap.
        logger.info(
            "shared_clips migration: recreating table to add 'kick' source support…"
        )
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE shared_clips RENAME TO _shared_clips_old")
        conn.execute("""
            CREATE TABLE shared_clips (
                shared_clip_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id         TEXT    UNIQUE NOT NULL,
                source          TEXT    NOT NULL CHECK(source IN ('twitch','youtube','kick')),
                creator_name    TEXT,
                title           TEXT    NOT NULL DEFAULT 'Untitled',
                url             TEXT    NOT NULL,
                duration_sec    REAL,
                view_count      INTEGER DEFAULT 0,
                score           REAL    DEFAULT 0,
                has_music       INTEGER DEFAULT 0,
                is_blocked      INTEGER DEFAULT 0,
                language        TEXT    DEFAULT 'en',
                fetched_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                expires_at      TEXT    NOT NULL,
                region          TEXT    NOT NULL DEFAULT 'global'
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO shared_clips
                (clip_id, source, creator_name, title, url, duration_sec,
                 view_count, score, has_music, is_blocked, fetched_at, expires_at, region)
            SELECT clip_id, source, creator_name, title, url, duration_sec,
                   view_count, score, has_music, is_blocked, fetched_at, expires_at, region
            FROM _shared_clips_old
        """)
        conn.execute("DROP TABLE _shared_clips_old")
        # Rebuild reservations with FK pointing to the new shared_clips
        _fix_reservations()
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        logger.info("shared_clips migration complete.")

    except Exception as exc:
        logger.warning("shared_clips migration error (non-fatal): %s", exc)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Clip insertion
# ══════════════════════════════════════════════════════════════════════════════

def add_clip_to_pool(
    clip: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> bool:
    """
    Insert a clip into the shared pool.

    Uses INSERT OR IGNORE so duplicate clip_ids are silently skipped.

    The expires_at timestamp is set based on source:
        - twitch  → now + TWITCH_EXPIRY_DAYS  (7 days)
        - youtube → now + YOUTUBE_EXPIRY_DAYS (14 days)

    Expected clip dict keys:
        clip_id, source, creator_name, title, url,
        duration (or duration_sec), view_count, score, has_music.

    Args:
        clip:    Clip dict in ClipCast standard format.
        db_path: Override database path (useful for tests).

    Returns:
        True if the clip was newly inserted; False if it already existed.
    """
    source = clip.get("source", "")
    if source == "youtube":
        expiry_days = YOUTUBE_EXPIRY_DAYS
    else:
        expiry_days = TWITCH_EXPIRY_DAYS
    now_utc = datetime.now(timezone.utc)
    expires_at = (now_utc + timedelta(days=expiry_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Accept either 'duration' or 'duration_sec' key
    duration_sec = clip.get("duration_sec") or clip.get("duration")

    conn = database.get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO shared_clips
                (clip_id, source, creator_name, title, url,
                 duration_sec, view_count, score, has_music, language, category,
                 discovery_source, viral_title, theme,
                 ai_quality_score, ai_analyzed, final_score,
                 expires_at)
            VALUES
                (:clip_id, :source, :creator_name, :title, :url,
                 :duration_sec, :view_count, :score, :has_music, :language, :category,
                 :discovery_source, :viral_title, :theme,
                 :ai_quality_score, :ai_analyzed, :final_score,
                 :expires_at)
        """, {
            "clip_id":          clip.get("clip_id", ""),
            "source":           source,
            "creator_name":     clip.get("creator_name"),
            "title":            clip.get("title") or "Untitled",
            "url":              clip.get("url", ""),
            "duration_sec":     duration_sec,
            "view_count":       int(clip.get("view_count") or 0),
            "score":            float(clip.get("score") or 0),
            "has_music":        int(bool(clip.get("has_music", False))),
            "language":         clip.get("language", "en"),
            "category":         clip.get("game") or clip.get("category") or "",
            "discovery_source": clip.get("discovery_source") or None,
            "viral_title":      clip.get("viral_title") or None,
            "theme":            clip.get("theme") or None,
            "ai_quality_score": float(clip.get("ai_quality_score") or 50.0),
            "ai_analyzed":      int(bool(clip.get("ai_analyzed", False))),
            # Fall back to score so non-AI clips still sort correctly
            "final_score":      float(clip.get("final_score") or clip.get("score") or 0.0),
            "expires_at":       expires_at,
        })
        conn.commit()
        inserted = cur.rowcount > 0
        if inserted:
            logger.debug(
                "add_clip_to_pool: inserted clip_id='%s' source=%s",
                clip.get("clip_id"), source,
            )
        return inserted

    except Exception as exc:
        logger.error("add_clip_to_pool failed for clip_id='%s': %s", clip.get("clip_id"), exc)
        return False
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Clip retrieval
# ══════════════════════════════════════════════════════════════════════════════

def _get_unused_presentation(shared_clip_id: int, conn) -> tuple:
    """
    Return the next unused (template, caption_style) combo for a viral clip.

    Queries shared_clip_reservations for all combos already assigned to this
    clip, then returns the first combo from ALL_PRESENTATION_COMBOS that has
    not been used. If all 16 combos have been used, returns the least recently
    used combo (recycling from oldest reservation).

    Args:
        shared_clip_id: PK of the shared_clips row.
        conn:           Open database connection (caller manages lifecycle).

    Returns:
        (template, caption_style) tuple, both ints 1–4.
    """
    try:
        rows = conn.execute("""
            SELECT template_used, caption_style_used, reserved_at
            FROM shared_clip_reservations
            WHERE shared_clip_id = ?
              AND template_used IS NOT NULL
              AND caption_style_used IS NOT NULL
            ORDER BY reserved_at ASC
        """, (shared_clip_id,)).fetchall()
    except Exception:
        return (1, 1)  # Safe default on error

    used = {(r["template_used"], r["caption_style_used"]) for r in rows}

    # Find first unused combo
    for combo in ALL_PRESENTATION_COMBOS:
        if combo not in used:
            return combo

    # All 16 combos exhausted — recycle the least recently used
    if rows:
        return (rows[0]["template_used"], rows[0]["caption_style_used"])
    return (1, 1)


def get_clips_for_user(
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
    limit: int = 100,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    """
    Return clips from the shared pool available to this user, applying two-tier
    reservation logic.

    Tier 1 — Viral clips (view_count >= VIRAL_VIEW_THRESHOLD):
        Available to multiple users simultaneously. Excluded only if THIS user
        already has an active reservation. Each returned viral clip includes
        ``suggested_template`` and ``suggested_caption_style`` — the next unused
        presentation combo so the user's video looks different from every other
        user posting the same clip. If all 16 combos are taken, the least
        recently used combo is recycled.

    Tier 2 — Regular clips (view_count < VIRAL_VIEW_THRESHOLD):
        Exclusive 48-hour lock. Excluded if ANY user has an active reservation
        (including this user).

    Common exclusions (both tiers):
        - Clips whose URL already exists in the user's clips table.
        - Blocked clips (is_blocked=1).
        - Expired clips (expires_at <= now).
        - Clips below minimum_views from user_prefs.

    Each returned clip dict uses normalized keys:
        shared_clip_id, clip_id, source, creator_name, title, url,
        duration, view_count, score, has_music, created_at,
        is_viral (bool), suggested_template (int|None), suggested_caption_style (int|None).

    Args:
        user_prefs: User preferences dict. Pass None to skip preference filters.
        user_id:    User ID for reservation and clips-table checks.
        limit:      Maximum number of clips to return (default 100).
        db_path:    Override database path.

    Returns:
        List of clip dicts ordered by score DESC.
    """
    min_views = 0
    preferred_language = ""
    if user_prefs:
        min_views = int(user_prefs.get("minimum_views", 0))
        preferred_language = str(user_prefs.get("preferred_language", "")).strip().lower()

    conn = database.get_connection(db_path)
    try:
        # ── Tier 1: viral clips (excluded only if THIS user already reserved) ──
        viral_rows = conn.execute("""
            SELECT
                sc.shared_clip_id,
                sc.clip_id,
                sc.source,
                sc.creator_name,
                sc.title,
                sc.url,
                sc.duration_sec,
                sc.view_count,
                sc.score,
                COALESCE(sc.final_score, sc.score, 0) AS final_score,
                sc.has_music,
                sc.fetched_at,
                COALESCE(sc.category, '') AS category
            FROM shared_clips sc
            WHERE sc.is_blocked = 0
              AND sc.expires_at > datetime('now')
              AND sc.view_count >= :viral_threshold
              AND sc.view_count >= :min_views
              AND (:lang = '' OR sc.language IS NULL OR sc.language = :lang)
              AND sc.shared_clip_id NOT IN (
                  SELECT shared_clip_id
                  FROM shared_clip_reservations
                  WHERE user_id = :user_id
                    AND expires_at > datetime('now')
              )
            ORDER BY COALESCE(sc.final_score, sc.score, 0) DESC
            LIMIT :limit
        """, {
            "viral_threshold": VIRAL_VIEW_THRESHOLD,
            "min_views":       min_views,
            "lang":            preferred_language,
            "user_id":         user_id,
            "limit":           limit * 3,
        }).fetchall()

        # ── Tier 2: regular clips (excluded if ANY user has active reservation) ─
        regular_rows = conn.execute("""
            SELECT
                sc.shared_clip_id,
                sc.clip_id,
                sc.source,
                sc.creator_name,
                sc.title,
                sc.url,
                sc.duration_sec,
                sc.view_count,
                sc.score,
                COALESCE(sc.final_score, sc.score, 0) AS final_score,
                sc.has_music,
                sc.fetched_at,
                COALESCE(sc.category, '') AS category
            FROM shared_clips sc
            WHERE sc.is_blocked = 0
              AND sc.expires_at > datetime('now')
              AND sc.view_count < :viral_threshold
              AND sc.view_count >= :min_views
              AND (:lang = '' OR sc.language IS NULL OR sc.language = :lang)
              AND sc.shared_clip_id NOT IN (
                  SELECT shared_clip_id
                  FROM shared_clip_reservations
                  WHERE expires_at > datetime('now')
              )
            ORDER BY COALESCE(sc.final_score, sc.score, 0) DESC
            LIMIT :limit
        """, {
            "viral_threshold": VIRAL_VIEW_THRESHOLD,
            "min_views":       min_views,
            "lang":            preferred_language,
            "limit":           limit * 3,
        }).fetchall()

        # ── Assign presentation combos to viral clips while connection is open ─
        viral_presentations: Dict[int, tuple] = {}
        for row in viral_rows:
            combo = _get_unused_presentation(row["shared_clip_id"], conn)
            viral_presentations[row["shared_clip_id"]] = combo

    except Exception as exc:
        logger.error("get_clips_for_user: DB query failed: %s", exc)
        return []
    finally:
        conn.close()

    # ── Merge and deduplicate (viral first — higher scores bubble up) ──────────
    seen_ids: set = set()
    all_rows = [(row, True) for row in viral_rows] + \
               [(row, False) for row in regular_rows]

    results: List[Dict] = []
    for row, is_viral in all_rows:
        if row["shared_clip_id"] in seen_ids:
            continue
        seen_ids.add(row["shared_clip_id"])

        # Filter out clips whose URL is already in the user's clips table
        try:
            if database.clip_url_exists(row["url"], db_path=db_path):
                continue
        except Exception as exc:
            logger.debug("clip_url_exists check failed for url='%s': %s", row["url"], exc)

        # Determine suggested presentation for viral clips
        suggested_template     = None
        suggested_caption_style = None
        if is_viral:
            combo = viral_presentations.get(row["shared_clip_id"])
            if combo:
                suggested_template, suggested_caption_style = combo

        clip: Dict[str, Any] = {
            "shared_clip_id":          row["shared_clip_id"],
            "clip_id":                 row["clip_id"],
            "source":                  row["source"],
            "creator_name":            row["creator_name"],
            "title":                   row["title"],
            "url":                     row["url"],
            "duration":                row["duration_sec"],
            "view_count":              row["view_count"],
            "score":                   row["score"],
            "final_score":             row["final_score"],
            "has_music":               bool(row["has_music"]),
            "created_at":              row["fetched_at"],
            "game":                    row["category"],   # drives layout routing in editor
            "is_viral":                is_viral,
            "suggested_template":      suggested_template,
            "suggested_caption_style": suggested_caption_style,
        }
        results.append(clip)

        if len(results) >= limit:
            break

    # Sort by final_score DESC (falls back to score for clips without AI analysis)
    results.sort(
        key=lambda c: (c.get("final_score") or c.get("score") or 0),
        reverse=True,
    )

    logger.debug(
        "get_clips_for_user: user_id=%d returned %d clip(s) "
        "(%d viral, %d regular) from shared pool.",
        user_id,
        len(results),
        sum(1 for c in results if c["is_viral"]),
        sum(1 for c in results if not c["is_viral"]),
    )
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Reservations
# ══════════════════════════════════════════════════════════════════════════════

def mark_clip_reserved(
    shared_clip_id: int,
    user_id: int = 1,
    hours: int = DEFAULT_RESERVATION_HOURS,
    template: Optional[int] = None,
    caption_style: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> None:
    """
    Reserve a shared clip for a user, recording the presentation combo used.

    For viral clips (view_count >= VIRAL_VIEW_THRESHOLD): Pass the
    ``template`` and ``caption_style`` that were assigned to this user so the
    system can track which presentation combos have been used globally.

    For regular clips: ``template`` and ``caption_style`` may be None — the
    row still locks the clip exclusively for 48 hours.

    Args:
        shared_clip_id: PK of the shared_clips row to reserve.
        user_id:        User ID (default 1).
        hours:          How many hours the reservation is valid (default 48).
        template:       Template number (1–4) assigned; None for regular clips.
        caption_style:  Caption style (1–4) assigned; None for regular clips.
        db_path:        Override database path.
    """
    now_utc = datetime.now(timezone.utc)
    expires_at = (now_utc + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = database.get_connection(db_path)
    try:
        conn.execute("""
            INSERT INTO shared_clip_reservations
                (shared_clip_id, user_id, expires_at, template_used, caption_style_used)
            VALUES (?, ?, ?, ?, ?)
        """, (shared_clip_id, user_id, expires_at, template, caption_style))
        conn.commit()
        logger.debug(
            "Reserved shared_clip_id=%d for user_id=%d until %s "
            "(template=%s caption_style=%s)",
            shared_clip_id, user_id, expires_at, template, caption_style,
        )
    except Exception as exc:
        logger.error(
            "mark_clip_reserved failed for shared_clip_id=%d: %s",
            shared_clip_id, exc,
        )
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Expiry
# ══════════════════════════════════════════════════════════════════════════════

def expire_old_clips(db_path: Optional[Path] = None) -> int:
    """
    Delete clips whose expires_at timestamp is in the past.

    Also deletes expired reservation rows so the reservations table stays
    tidy.

    Args:
        db_path: Override database path.

    Returns:
        Number of clip rows deleted.
    """
    conn = database.get_connection(db_path)
    try:
        # Delete expired reservations first (foreign key constraint)
        conn.execute(
            "DELETE FROM shared_clip_reservations WHERE expires_at <= datetime('now')"
        )

        # Delete expired clips
        cur = conn.execute(
            "DELETE FROM shared_clips WHERE expires_at <= datetime('now')"
        )
        deleted = cur.rowcount
        conn.commit()

        if deleted > 0:
            logger.info("expire_old_clips: removed %d expired clip(s).", deleted)
        else:
            logger.debug("expire_old_clips: no expired clips found.")

        return deleted

    except Exception as exc:
        logger.error("expire_old_clips failed: %s", exc)
        return 0
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Statistics
# ══════════════════════════════════════════════════════════════════════════════

def get_pool_stats(db_path: Optional[Path] = None) -> Dict:
    """
    Return a summary of the current shared pool state.

    Returns a dict with:
        total_clips         (int)   — Active, non-blocked clips in the pool.
        by_source           (dict)  — {source: count} for each source platform.
        avg_score           (float) — Average score rounded to 1 decimal place.
        oldest_clip         (str|None) — fetched_at of the oldest active clip.
        newest_clip         (str|None) — fetched_at of the most recently added clip.
        active_reservations (int)   — Reservations that have not yet expired.
        last_runs           (list)  — Last 5 shared_pool_runs rows as dicts.

    Args:
        db_path: Override database path.

    Returns:
        Stats dict. Never raises — returns zeroed dict on error.
    """
    empty: Dict[str, Any] = {
        "total_clips": 0,
        "by_source": {},
        "avg_score": 0.0,
        "oldest_clip": None,
        "newest_clip": None,
        "active_reservations": 0,
        "last_runs": [],
    }

    conn = database.get_connection(db_path)
    try:
        # Total active non-blocked clips
        total_row = conn.execute(
            "SELECT COUNT(*) FROM shared_clips "
            "WHERE is_blocked = 0 AND expires_at > datetime('now')"
        ).fetchone()
        total_clips = int(total_row[0]) if total_row else 0

        # By source
        source_rows = conn.execute(
            "SELECT source, COUNT(*) AS cnt FROM shared_clips "
            "WHERE is_blocked = 0 AND expires_at > datetime('now') "
            "GROUP BY source"
        ).fetchall()
        by_source = {row["source"]: row["cnt"] for row in source_rows}

        # Average score
        avg_row = conn.execute(
            "SELECT AVG(score) FROM shared_clips "
            "WHERE is_blocked = 0 AND expires_at > datetime('now')"
        ).fetchone()
        avg_score = round(float(avg_row[0] or 0), 1)

        # Oldest / newest fetched_at
        bounds_row = conn.execute(
            "SELECT MIN(fetched_at), MAX(fetched_at) FROM shared_clips "
            "WHERE is_blocked = 0 AND expires_at > datetime('now')"
        ).fetchone()
        oldest_clip = bounds_row[0] if bounds_row else None
        newest_clip = bounds_row[1] if bounds_row else None

        # Active reservations
        reserv_row = conn.execute(
            "SELECT COUNT(*) FROM shared_clip_reservations "
            "WHERE expires_at > datetime('now')"
        ).fetchone()
        active_reservations = int(reserv_row[0]) if reserv_row else 0

        # Last 5 pool runs
        run_rows = conn.execute(
            "SELECT * FROM shared_pool_runs ORDER BY run_id DESC LIMIT 5"
        ).fetchall()
        last_runs = [dict(r) for r in run_rows]

        return {
            "total_clips": total_clips,
            "by_source": by_source,
            "avg_score": avg_score,
            "oldest_clip": oldest_clip,
            "newest_clip": newest_clip,
            "active_reservations": active_reservations,
            "last_runs": last_runs,
        }

    except Exception as exc:
        logger.error("get_pool_stats failed: %s", exc)
        return empty
    finally:
        conn.close()


def get_clip_presentation_stats(
    shared_clip_id: int,
    db_path: Optional[Path] = None,
) -> Dict:
    """
    Return presentation usage stats for a single shared clip.

    Useful for operators monitoring viral clips via ``--pool``.

    Returns a dict with:
        shared_clip_id   (int)   — The queried clip ID.
        title            (str)   — Clip title.
        view_count       (int)   — Current view count.
        is_viral         (bool)  — True if view_count >= VIRAL_VIEW_THRESHOLD.
        times_used       (int)   — Total active reservations for this clip.
        combos_used      (list)  — Each assigned (template, caption_style, reserved_at).
        combos_available (int)   — How many of the 16 combos remain unused.

    Args:
        shared_clip_id: PK of the shared_clips row.
        db_path:        Override database path.

    Returns:
        Stats dict. Returns an empty dict with times_used=0 on error.
    """
    conn = database.get_connection(db_path)
    try:
        clip_row = conn.execute(
            "SELECT title, view_count FROM shared_clips WHERE shared_clip_id = ?",
            (shared_clip_id,),
        ).fetchone()

        if not clip_row:
            return {"shared_clip_id": shared_clip_id, "times_used": 0, "combos_used": [],
                    "combos_available": 16, "is_viral": False, "title": "", "view_count": 0}

        reservation_rows = conn.execute("""
            SELECT template_used, caption_style_used, reserved_at, user_id
            FROM shared_clip_reservations
            WHERE shared_clip_id = ?
              AND expires_at > datetime('now')
            ORDER BY reserved_at ASC
        """, (shared_clip_id,)).fetchall()

        combos_used = [
            {
                "template":       r["template_used"],
                "caption_style":  r["caption_style_used"],
                "reserved_at":    r["reserved_at"],
                "user_id":        r["user_id"],
            }
            for r in reservation_rows
            if r["template_used"] is not None and r["caption_style_used"] is not None
        ]

        used_set = {(c["template"], c["caption_style"]) for c in combos_used}
        combos_available = sum(1 for combo in ALL_PRESENTATION_COMBOS if combo not in used_set)

        return {
            "shared_clip_id":  shared_clip_id,
            "title":           clip_row["title"],
            "view_count":      clip_row["view_count"],
            "is_viral":        clip_row["view_count"] >= VIRAL_VIEW_THRESHOLD,
            "times_used":      len(reservation_rows),
            "combos_used":     combos_used,
            "combos_available": combos_available,
        }

    except Exception as exc:
        logger.error("get_clip_presentation_stats failed for clip %d: %s", shared_clip_id, exc)
        return {"shared_clip_id": shared_clip_id, "times_used": 0, "combos_used": [],
                "combos_available": 16, "is_viral": False, "title": "", "view_count": 0}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Pool run tracking
# ══════════════════════════════════════════════════════════════════════════════

def log_pool_run_start(
    source: str,
    db_path: Optional[Path] = None,
) -> int:
    """
    Insert a new shared_pool_runs row with status='running'.

    Args:
        source:  Platform being fetched ('twitch' or 'youtube').
        db_path: Override database path.

    Returns:
        The new run_id (int). Returns -1 on error.
    """
    conn = database.get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO shared_pool_runs (source, status) VALUES (?, 'running')",
            (source,),
        )
        conn.commit()
        run_id = cur.lastrowid
        logger.debug("Pool run started: run_id=%d source=%s", run_id, source)
        return run_id

    except Exception as exc:
        logger.error("log_pool_run_start failed for source='%s': %s", source, exc)
        return -1
    finally:
        conn.close()


def complete_pool_run(
    run_id: int,
    clips_added: int,
    clips_expired: int,
    status: str = "completed",
    db_path: Optional[Path] = None,
) -> None:
    """
    Mark a pool run as finished by setting completed_at and final counts.

    Args:
        run_id:        The run_id returned by log_pool_run_start().
        clips_added:   Number of new clips inserted during this run.
        clips_expired: Number of clips deleted during this run's expiry pass.
        status:        Final status string (e.g. 'completed', 'error',
                       'completed_partial').
        db_path:       Override database path.
    """
    conn = database.get_connection(db_path)
    try:
        conn.execute("""
            UPDATE shared_pool_runs
               SET completed_at  = datetime('now'),
                   clips_added   = ?,
                   clips_expired = ?,
                   status        = ?
             WHERE run_id = ?
        """, (clips_added, clips_expired, status, run_id))
        conn.commit()
        logger.debug(
            "Pool run completed: run_id=%d status=%s added=%d expired=%d",
            run_id, status, clips_added, clips_expired,
        )

    except Exception as exc:
        logger.error("complete_pool_run failed for run_id=%d: %s", run_id, exc)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Freshness check
# ══════════════════════════════════════════════════════════════════════════════

def check_pool_freshness(
    hours: int = 6,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Return True if the shared pool was successfully refreshed within the last
    `hours` hours.

    Checks for any shared_pool_runs row where:
        - status = 'completed' or 'completed_partial'
        - started_at >= now - hours

    Args:
        hours:   Look-back window in hours (default 6).
        db_path: Override database path.

    Returns:
        True if the pool is considered fresh; False if a refresh is needed.
    """
    conn = database.get_connection(db_path)
    try:
        row = conn.execute("""
            SELECT 1 FROM shared_pool_runs
             WHERE status IN ('completed', 'completed_partial')
               AND started_at >= datetime('now', :offset)
             LIMIT 1
        """, {"offset": f"-{hours} hours"}).fetchone()
        return row is not None

    except Exception as exc:
        logger.error("check_pool_freshness failed: %s", exc)
        return False
    finally:
        conn.close()


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(name)s  %(message)s")

    print("=" * 60)
    print("shared_pool.py  —  self-test")
    print("=" * 60)

    # Use a temporary database so the test never touches clipcast.db
    tmp_db = Path(tempfile.mktemp(suffix="_shared_pool_test.db"))
    print(f"\nUsing temp DB: {tmp_db}\n")

    try:
        # 1. Initialize database (base tables) + shared pool tables
        print("1. Initializing database tables...")
        database.initialize_database(db_path=tmp_db)
        initialize_shared_pool_tables(db_path=tmp_db)
        print("   OK — all tables created")

        # 2. Add test clips
        print("\n2. Adding 3 test clips (2 twitch, 1 youtube)...")

        clip_twitch_1 = {
            "clip_id": "twitch-abc123",
            "source": "twitch",
            "creator_name": "StreamerA",
            "title": "Insane play by StreamerA",
            "url": "https://clips.twitch.tv/abc123",
            "duration": 62.5,
            "view_count": 15000,
            "score": 78.4,
            "has_music": False,
        }
        clip_twitch_2 = {
            "clip_id": "twitch-def456",
            "source": "twitch",
            "creator_name": "StreamerB",
            "title": "Amazing clutch",
            "url": "https://clips.twitch.tv/def456",
            "duration_sec": 45.0,
            "view_count": 5000,
            "score": 52.1,
            "has_music": True,
        }
        clip_youtube_1 = {
            "clip_id": "yt-xyz789",
            "source": "youtube",
            "creator_name": "GamingChannel",
            "title": "Top 10 gaming moments",
            "url": "https://www.youtube.com/watch?v=xyz789",
            "duration": 90.0,
            "view_count": 250000,
            "score": 91.0,
            "has_music": False,
        }

        r1 = add_clip_to_pool(clip_twitch_1, db_path=tmp_db)
        r2 = add_clip_to_pool(clip_twitch_2, db_path=tmp_db)
        r3 = add_clip_to_pool(clip_youtube_1, db_path=tmp_db)
        print(f"   twitch-abc123 inserted: {r1}  (expected True)")
        print(f"   twitch-def456 inserted: {r2}  (expected True)")
        print(f"   yt-xyz789     inserted: {r3}  (expected True)")

        # Duplicate insert — must return False
        r_dup = add_clip_to_pool(clip_twitch_1, db_path=tmp_db)
        print(f"   Duplicate insert result: {r_dup}  (expected False)")
        assert not r_dup, "Duplicate insert should return False"

        # 3. get_pool_stats
        print("\n3. Checking pool stats...")
        stats = get_pool_stats(db_path=tmp_db)
        print(f"   total_clips:         {stats['total_clips']}  (expected 3)")
        print(f"   by_source:           {stats['by_source']}")
        print(f"   avg_score:           {stats['avg_score']}")
        print(f"   oldest_clip:         {stats['oldest_clip']}")
        print(f"   newest_clip:         {stats['newest_clip']}")
        print(f"   active_reservations: {stats['active_reservations']}  (expected 0)")
        assert stats["total_clips"] == 3, f"Expected 3 clips, got {stats['total_clips']}"

        # 4. get_clips_for_user — two-tier behaviour
        print("\n4. Fetching clips for user_id=1...")
        clips = get_clips_for_user(user_id=1, db_path=tmp_db)
        print(f"   Returned {len(clips)} clip(s)  (expected 3)")
        assert len(clips) == 3
        print(f"   First clip keys: {sorted(clips[0].keys())}")

        # Verify new keys are present
        assert "is_viral" in clips[0], "is_viral key missing"
        assert "suggested_template" in clips[0], "suggested_template key missing"
        assert "suggested_caption_style" in clips[0], "suggested_caption_style key missing"

        # Identify viral vs regular clips
        viral_clips   = [c for c in clips if c["is_viral"]]
        regular_clips = [c for c in clips if not c["is_viral"]]
        print(f"   Viral clips:   {len(viral_clips)}  "
              f"(view_count >= {VIRAL_VIEW_THRESHOLD})")
        print(f"   Regular clips: {len(regular_clips)}  "
              f"(view_count < {VIRAL_VIEW_THRESHOLD})")

        # Viral clips should have suggested combos
        for vc in viral_clips:
            assert vc["suggested_template"] in (1, 2, 3, 4), \
                f"Bad suggested_template: {vc['suggested_template']}"
            assert vc["suggested_caption_style"] in (1, 2, 3, 4), \
                f"Bad suggested_caption_style: {vc['suggested_caption_style']}"
            print(f"   Viral '{vc['title'][:30]}': "
                  f"suggested T{vc['suggested_template']} C{vc['suggested_caption_style']}")

        # 5. Reservation: two-tier behaviour
        print("\n5. Reservation system — two-tier test...")

        # 5a. Reserve a viral clip for user_id=1 with a presentation combo
        if viral_clips:
            viral_clip = viral_clips[0]
            vid = viral_clip["shared_clip_id"]
            t1, c1 = viral_clip["suggested_template"], viral_clip["suggested_caption_style"]
            mark_clip_reserved(vid, user_id=1, hours=48,
                               template=t1, caption_style=c1, db_path=tmp_db)
            print(f"   Reserved viral clip {vid} for user_id=1 "
                  f"with T{t1}C{c1}")

            # user_id=1 should no longer see this viral clip
            clips_u1 = get_clips_for_user(user_id=1, db_path=tmp_db)
            u1_viral_ids = {c["shared_clip_id"] for c in clips_u1 if c["is_viral"]}
            assert vid not in u1_viral_ids, \
                "Viral clip should be hidden from user_id=1 after reservation"
            print(f"   user_id=1 can no longer see clip {vid}  OK")

            # user_id=2 should still see the viral clip (different user)
            clips_u2 = get_clips_for_user(user_id=2, db_path=tmp_db)
            u2_viral_ids = {c["shared_clip_id"] for c in clips_u2 if c["is_viral"]}
            assert vid in u2_viral_ids, \
                "Viral clip should still be visible to user_id=2"
            # And user_id=2 should get a DIFFERENT combo
            u2_viral = next(c for c in clips_u2 if c["shared_clip_id"] == vid)
            t2, c2 = u2_viral["suggested_template"], u2_viral["suggested_caption_style"]
            assert (t2, c2) != (t1, c1), \
                f"user_id=2 should get a different combo than user_id=1's ({t1},{c1})"
            print(f"   user_id=2 sees clip {vid} with T{t2}C{c2}  (different — OK)")

        # 5b. Reserve a regular clip for any user — exclusive lock
        if regular_clips:
            reg_clip = regular_clips[0]
            rid = reg_clip["shared_clip_id"]
            mark_clip_reserved(rid, user_id=2, hours=48, db_path=tmp_db)
            print(f"   Reserved regular clip {rid} for user_id=2 (exclusive lock)")

            # Neither user_id=1 nor user_id=3 should see this regular clip
            clips_u3 = get_clips_for_user(user_id=3, db_path=tmp_db)
            u3_regular_ids = {c["shared_clip_id"] for c in clips_u3 if not c["is_viral"]}
            assert rid not in u3_regular_ids, \
                "Regular clip should be hidden from ALL users after exclusive reservation"
            print(f"   user_id=3 cannot see clip {rid}  OK (exclusive lock)")

        stats2 = get_pool_stats(db_path=tmp_db)
        print(f"   active_reservations: {stats2['active_reservations']}")

        # 5c. get_clip_presentation_stats for the viral clip
        if viral_clips:
            ps = get_clip_presentation_stats(vid, db_path=tmp_db)
            print(f"\n   Presentation stats for clip {vid}:")
            print(f"     title:            {ps['title']}")
            print(f"     is_viral:         {ps['is_viral']}  (expected True)")
            print(f"     times_used:       {ps['times_used']}  (expected 1)")
            print(f"     combos_available: {ps['combos_available']}  (expected 15)")
            assert ps["is_viral"], "Expected is_viral=True"
            assert ps["times_used"] == 1, f"Expected times_used=1, got {ps['times_used']}"
            assert ps["combos_available"] == 15, \
                f"Expected 15 combos available, got {ps['combos_available']}"

        # 6. expire_old_clips — add a clip that is already expired
        print("\n6. Testing expire_old_clips with a pre-expired clip...")

        # Directly insert an already-expired clip
        import sqlite3
        raw_conn = sqlite3.connect(str(tmp_db))
        raw_conn.row_factory = sqlite3.Row
        raw_conn.execute("PRAGMA foreign_keys = ON")
        raw_conn.execute("""
            INSERT OR IGNORE INTO shared_clips
                (clip_id, source, title, url, expires_at)
            VALUES
                ('expired-clip-001', 'twitch', 'Old Clip', 'https://clips.twitch.tv/old',
                 datetime('now', '-1 day'))
        """)
        raw_conn.commit()
        raw_conn.close()

        stats_before = get_pool_stats(db_path=tmp_db)
        deleted = expire_old_clips(db_path=tmp_db)
        stats_after = get_pool_stats(db_path=tmp_db)
        print(f"   Clips before expire: {stats_before['total_clips']}")
        print(f"   Clips deleted:       {deleted}  (expected 1)")
        print(f"   Clips after expire:  {stats_after['total_clips']}  (expected 3)")
        assert deleted == 1, f"Expected 1 deleted, got {deleted}"
        assert stats_after["total_clips"] == 3

        # 7. check_pool_freshness — False initially (no completed run)
        print("\n7. Testing check_pool_freshness...")
        fresh_before = check_pool_freshness(hours=6, db_path=tmp_db)
        print(f"   Fresh before any run: {fresh_before}  (expected False)")
        assert not fresh_before

        # Log a completed run
        run_id = log_pool_run_start("twitch", db_path=tmp_db)
        complete_pool_run(run_id, clips_added=3, clips_expired=1,
                          status="completed", db_path=tmp_db)

        fresh_after = check_pool_freshness(hours=6, db_path=tmp_db)
        print(f"   Fresh after completed run: {fresh_after}  (expected True)")
        assert fresh_after

        # Verify last_runs shows the run
        stats_final = get_pool_stats(db_path=tmp_db)
        print(f"   last_runs count: {len(stats_final['last_runs'])}  (expected >= 1)")
        assert len(stats_final["last_runs"]) >= 1
        latest_run = stats_final["last_runs"][0]
        print(f"   Latest run: status={latest_run['status']} added={latest_run['clips_added']}")

        print("\n" + "=" * 60)
        print("All shared_pool.py tests PASSED.")
        print("=" * 60)

    except AssertionError as ae:
        print(f"\nASSERTION FAILED: {ae}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\nUNEXPECTED ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Clean up temp database
        if tmp_db.exists():
            tmp_db.unlink()
            print(f"\nTemp DB cleaned up: {tmp_db}")
