"""
tiktok_shop/database.py
=======================
SQLite database layer for the TikTok Shop module.

Tables:
    ts_products     — Products imported from Kalodata CSV exports.
    ts_videos       — Generated videos ready for Google Drive export.
    ts_export_log   — Record of every Drive export attempt.

Follows the same patterns as clipcast/database.py — uses CREATE TABLE IF NOT EXISTS,
stores everything in the existing clipcast.db file, and supports user_id for future
multi-tenant SaaS use.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).parent.parent
DATABASE_PATH = BASE_DIR / "clipcast.db"

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Connection
# ══════════════════════════════════════════════════════════════════════════════

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DATABASE_PATH
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# Schema init — safe to call on every startup
# ══════════════════════════════════════════════════════════════════════════════

def init_tables(db_path: Optional[Path] = None) -> None:
    """Create TikTok Shop tables if they don't already exist."""
    with get_connection(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ts_products (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL DEFAULT 1,

                -- Kalodata fields
                product_name        TEXT NOT NULL,
                product_url         TEXT,
                category            TEXT,
                commission_rate     REAL,
                price_usd           REAL,
                monthly_revenue     REAL,
                ad_spend_score      REAL,   -- how much brands are spending on ads
                creator_count       INTEGER, -- number of TikTok creators promoting it
                avg_views           REAL,

                -- Our scoring
                opportunity_score   REAL,   -- composite 0-100 score from Claude
                score_breakdown     TEXT,   -- JSON: {commission, price, saturation, demand}
                score_reasoning     TEXT,   -- Claude's plain-English reasoning
                status              TEXT NOT NULL DEFAULT 'scored',
                                            -- scored | approved | rejected | video_queued | done

                imported_at         TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS ts_videos (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL DEFAULT 1,
                product_id          INTEGER NOT NULL REFERENCES ts_products(id),

                -- Generated content
                script              TEXT,   -- Claude-generated voiceover script
                voice_path          TEXT,   -- local path to ElevenLabs MP3
                video_path          TEXT,   -- local path to assembled MP4
                caption             TEXT,   -- TikTok caption with hashtags

                -- Export
                status              TEXT NOT NULL DEFAULT 'pending',
                                            -- pending | voice_ready | video_ready | exported | failed
                drive_file_id       TEXT,   -- Google Drive file ID after export
                drive_link          TEXT,   -- Shareable Drive link
                error_message       TEXT,

                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS ts_export_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id            INTEGER NOT NULL REFERENCES ts_videos(id),
                attempted_at        TEXT NOT NULL DEFAULT (datetime('now')),
                success             INTEGER NOT NULL DEFAULT 0,
                error_message       TEXT
            );
        """)
    logger.info("TikTok Shop tables initialised.")


# ══════════════════════════════════════════════════════════════════════════════
# Products
# ══════════════════════════════════════════════════════════════════════════════

def insert_product(product: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """Insert a scored product. Returns the new row id."""
    sql = """
        INSERT INTO ts_products (
            user_id, product_name, product_url, category,
            commission_rate, price_usd, monthly_revenue,
            ad_spend_score, creator_count, avg_views,
            opportunity_score, score_breakdown, score_reasoning, status
        ) VALUES (
            :user_id, :product_name, :product_url, :category,
            :commission_rate, :price_usd, :monthly_revenue,
            :ad_spend_score, :creator_count, :avg_views,
            :opportunity_score, :score_breakdown, :score_reasoning, :status
        )
    """
    with get_connection(db_path) as conn:
        cur = conn.execute(sql, product)
        return cur.lastrowid


def get_products(
    status: Optional[str] = None,
    user_id: int = 1,
    limit: int = 100,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    with get_connection(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM ts_products WHERE user_id=? AND status=? ORDER BY opportunity_score DESC LIMIT ?",
                (user_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ts_products WHERE user_id=? ORDER BY opportunity_score DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_product(product_id: int, db_path: Optional[Path] = None) -> Optional[Dict]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM ts_products WHERE id=?", (product_id,)
        ).fetchone()
        return dict(row) if row else None


def update_product_status(product_id: int, status: str, db_path: Optional[Path] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE ts_products SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, product_id),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Videos
# ══════════════════════════════════════════════════════════════════════════════

def insert_video(video: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """Insert a video job. Returns the new row id."""
    sql = """
        INSERT INTO ts_videos (
            user_id, product_id, script, caption, status
        ) VALUES (
            :user_id, :product_id, :script, :caption, :status
        )
    """
    with get_connection(db_path) as conn:
        cur = conn.execute(sql, video)
        return cur.lastrowid


def update_video(video_id: int, fields: Dict[str, Any], db_path: Optional[Path] = None) -> None:
    """Update arbitrary fields on a video row."""
    fields["updated_at"] = datetime.now().isoformat()
    fields["id"] = video_id
    set_clause = ", ".join(f"{k}=:{k}" for k in fields if k != "id")
    with get_connection(db_path) as conn:
        conn.execute(f"UPDATE ts_videos SET {set_clause} WHERE id=:id", fields)


def get_videos(
    status: Optional[str] = None,
    user_id: int = 1,
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> List[Dict]:
    with get_connection(db_path) as conn:
        if status:
            rows = conn.execute(
                """SELECT v.*, p.product_name, p.opportunity_score, p.commission_rate
                   FROM ts_videos v JOIN ts_products p ON v.product_id=p.id
                   WHERE v.user_id=? AND v.status=? ORDER BY v.created_at DESC LIMIT ?""",
                (user_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT v.*, p.product_name, p.opportunity_score, p.commission_rate
                   FROM ts_videos v JOIN ts_products p ON v.product_id=p.id
                   WHERE v.user_id=? ORDER BY v.created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_video(video_id: int, db_path: Optional[Path] = None) -> Optional[Dict]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """SELECT v.*, p.product_name, p.product_url, p.category,
                      p.opportunity_score, p.commission_rate, p.price_usd,
                      p.score_reasoning
               FROM ts_videos v JOIN ts_products p ON v.product_id=p.id
               WHERE v.id=?""",
            (video_id,),
        ).fetchone()
        return dict(row) if row else None


def log_export(video_id: int, success: bool, error: Optional[str] = None, db_path: Optional[Path] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO ts_export_log (video_id, success, error_message) VALUES (?,?,?)",
            (video_id, int(success), error),
        )
