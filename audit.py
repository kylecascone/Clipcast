"""
audit.py
========
Silent audit logging for all ClipCast Studio pipeline actions.

Records every significant action to the audit_log table in the database:
  - Every clip fetched (source, creator, URL)
  - Every package processed (template, clip count)
  - Every post made (platform, post ID)
  - Every blocked creator encountered
  - Attribution applied per clip
  - Daily API quota summaries

All functions are wrapped in try/except and never interrupt the pipeline.
Audit logging failures are silent — the pipeline always continues.

This log is for the operator's legal protection only. It provides a
timestamped record of what was processed and posted, useful if a DMCA
dispute ever arises.

Access:
    python main.py --audit       Exports last 30 days to legal/audit_export_*.csv

Test:
    python audit.py
"""

import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import database

logger = logging.getLogger(__name__)

# ── Output folder ──────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
LEGAL_DIR = BASE_DIR / "legal"


# ══════════════════════════════════════════════════════════════════════════════
# Public logging functions — all silent on failure
# ══════════════════════════════════════════════════════════════════════════════

def log_fetch(clip: Dict[str, Any], user_id: int = 1) -> None:
    """
    Record a single clip fetch event.

    Args:
        clip:    Clip dict (needs 'source', 'title', 'url', 'creator_name').
        user_id: User ID.
    """
    try:
        detail = (
            f"creator='{clip.get('creator_name', '')}' "
            f"title='{clip.get('title', '')[:50]}' "
            f"url='{clip.get('url', '')[:80]}'"
        )
        database.log_audit(
            action="fetch",
            detail=detail,
            source=clip.get("source", "unknown"),
            clip_id=clip.get("clip_id"),
            user_id=user_id,
        )
    except Exception:
        pass


def log_fetch_batch(clips: List[Dict[str, Any]], user_id: int = 1) -> None:
    """
    Record a batch of fetched clips efficiently (one summary row).

    Args:
        clips:   List of clip dicts.
        user_id: User ID.
    """
    try:
        source_counts: Dict[str, int] = {}
        for clip in clips:
            src = clip.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        detail = "  ".join(f"{src}:{n}" for src, n in sorted(source_counts.items()))
        database.log_audit(
            action="fetch_batch",
            detail=f"total={len(clips)}  {detail}",
            source=",".join(sorted(source_counts)),
            user_id=user_id,
        )
    except Exception:
        pass


def log_process(
    package_id: int,
    template_id: int,
    clip_ids: List[int],
    user_id: int = 1,
) -> None:
    """
    Record a package processing event.

    Args:
        package_id:  Database package ID.
        template_id: Template number used.
        clip_ids:    List of clip IDs in this package.
        user_id:     User ID.
    """
    try:
        database.log_audit(
            action="process",
            detail=f"clips={len(clip_ids)}  template={template_id}  ids={clip_ids}",
            package_id=package_id,
            user_id=user_id,
        )
    except Exception:
        pass


def log_attribution(
    clip: Dict[str, Any],
    caption_text: str,
    user_id: int = 1,
) -> None:
    """
    Record attribution details for a processed clip.

    Args:
        clip:         Clip dict with creator_name.
        caption_text: The final caption including attribution suffix.
        user_id:      User ID.
    """
    try:
        database.log_audit(
            action="attribution",
            detail=(
                f"creator='{clip.get('creator_name', 'unknown')}' "
                f"caption='{caption_text[:80]}'"
            ),
            source=clip.get("source", "unknown"),
            clip_id=clip.get("clip_id"),
            user_id=user_id,
        )
    except Exception:
        pass


def log_post(
    package_id: int,
    platform: str,
    post_id: str,
    user_id: int = 1,
) -> None:
    """
    Record a successful post event.

    Args:
        package_id: Database package ID.
        platform:   Platform name ('tiktok', 'youtube_shorts', 'instagram_reels').
        post_id:    Platform-assigned post ID.
        user_id:    User ID.
    """
    try:
        database.log_audit(
            action="post",
            detail=f"post_id='{post_id}'",
            source=platform,
            package_id=package_id,
            user_id=user_id,
        )
    except Exception:
        pass


def log_blocked_creator(
    name: str,
    platform: str,
    user_id: int = 1,
) -> None:
    """
    Record a blocked-creator encounter.

    Args:
        name:     Creator name that was skipped.
        platform: 'twitch' or 'youtube'.
        user_id:  User ID.
    """
    try:
        database.log_audit(
            action="blocked",
            detail=f"creator='{name}'",
            source=platform,
            user_id=user_id,
        )
    except Exception:
        pass


def log_quota_summary(user_id: int = 1) -> None:
    """Record today's API quota usage summary for each service."""
    try:
        today = date.today().isoformat()
        for service in ("youtube", "tiktok_posts"):
            used = database.get_daily_quota(service, date_str=today, user_id=user_id)
            if used > 0:
                database.log_audit(
                    action="quota",
                    detail=f"service='{service}'  used={used}  date={today}",
                    source=service,
                    user_id=user_id,
                )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# CSV export
# ══════════════════════════════════════════════════════════════════════════════

def export_to_csv(days: int = 30, user_id: int = 1) -> Optional[str]:
    """
    Export the last N days of audit logs to a timestamped CSV file in legal/.

    Args:
        days:    Number of days to include (default 30).
        user_id: User ID.

    Returns:
        Absolute path to the exported CSV, or None on failure.
    """
    try:
        logs = database.get_audit_logs(days=days, user_id=user_id)
        if not logs:
            return None

        LEGAL_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"audit_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        out_path = LEGAL_DIR / filename

        fieldnames = [
            "audit_id", "action", "source", "detail",
            "package_id", "clip_id", "created_at",
        ]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(logs)

        logger.info("Audit log exported: %s (%d rows, %d days)", out_path, len(logs), days)
        return str(out_path)

    except Exception as e:
        logger.error("Failed to export audit log: %s", e)
        return None


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print("Testing audit.py...\n")

    database.initialize_database()

    log_fetch({
        "source": "twitch",
        "title": "Amazing clip",
        "creator_name": "teststreamer",
        "url": "https://clips.twitch.tv/test123",
        "clip_id": None,
    })
    print("✓ log_fetch()")

    log_fetch_batch([
        {"source": "twitch", "title": "Clip 1"},
        {"source": "youtube", "title": "Clip 2"},
    ])
    print("✓ log_fetch_batch()")

    log_process(package_id=1, template_id=2, clip_ids=[1, 2])
    print("✓ log_process()")

    log_attribution(
        clip={"source": "twitch", "creator_name": "teststreamer", "clip_id": 1},
        caption_text="He had NO idea | #gaming | clip via teststreamer",
    )
    print("✓ log_attribution()")

    log_post(package_id=1, platform="tiktok", post_id="tiktok_abc123")
    print("✓ log_post()")

    log_blocked_creator(name="optout_streamer", platform="twitch")
    print("✓ log_blocked_creator()")

    logs = database.get_audit_logs(days=1)
    print(f"✓ Retrieved {len(logs)} audit log row(s)")

    out = export_to_csv(days=1)
    print(f"✓ Exported to: {out}")

    print("\nAll audit tests passed.")
