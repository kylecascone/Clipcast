"""
analytics.py
============
Pulls performance data for previously posted videos from the TikTok API and
stores it in the database performance table. Prints a rich summary table.

Accessed via:  python main.py --analytics

TikTok API used:
  GET https://open.tiktokapis.com/v2/video/list/
  Requires scope: video.list
  Returns: view_count, like_count, comment_count, share_count per video.

The function matches TikTok post IDs stored in the database to fetch
fresh performance data and update the performance table.

SaaS Note:
    All functions accept user_config and user_id for multi-user support.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from rich.console import Console
from rich.table import Table

import database

logger = logging.getLogger(__name__)
console = Console()

_TIKTOK_VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"
_VIDEO_FIELDS = "id,title,video_description,duration,view_count,like_count,comment_count,share_count"


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def fetch_and_store_analytics(
    user_config: Optional[Dict] = None,
    user_id: int = 1,
    test_mode: bool = False,
) -> List[Dict[str, Any]]:
    """
    Fetch TikTok performance data for all posted packages and store results.

    Steps:
      1. Pull all posted packages with a tiktok_post_id from the database.
      2. Request video stats from TikTok's video/list endpoint.
      3. Match results by post ID and upsert into the performance table.

    Args:
        user_config: Config dict with TikTok credentials. Loaded from
                     config.yaml if None.
        user_id:     User ID for database writes.
        test_mode:   If True, skip the API call and return sample data.

    Returns:
        List of performance dicts with view/like/comment/share counts.
    """
    if user_config is None:
        from preferences import load_config
        user_config = load_config()

    # Gather all packages with a tiktok_post_id
    posted_pkgs = _get_posted_packages(user_id)
    if not posted_pkgs:
        console.print("[yellow]No posted packages with TikTok post IDs found.[/yellow]")
        return []

    post_id_to_pkg = {
        pkg["tiktok_post_id"]: pkg
        for pkg in posted_pkgs
        if pkg.get("tiktok_post_id")
    }

    if not post_id_to_pkg:
        console.print("[yellow]No TikTok post IDs found in posted packages.[/yellow]")
        return []

    if test_mode:
        console.print("[yellow][TEST MODE] Skipping TikTok API call — returning sample data.[/yellow]")
        return _sample_analytics(post_id_to_pkg)

    # Fetch performance from TikTok API
    try:
        from uploader import get_valid_access_token
        access_token = get_valid_access_token(user_config)
    except RuntimeError as e:
        console.print(f"[red]TikTok auth error:[/red] {e}")
        return []

    tiktok_stats = _fetch_tiktok_video_stats(access_token)
    if not tiktok_stats:
        console.print("[yellow]No video stats returned from TikTok.[/yellow]")
        return []

    # Match by post ID and store
    results = []
    for stat in tiktok_stats:
        video_id = stat.get("id", "")
        pkg = post_id_to_pkg.get(video_id)
        if not pkg:
            continue

        database.upsert_performance(
            package_id=pkg["package_id"],
            platform="tiktok",
            post_id=video_id,
            view_count=stat.get("view_count", 0),
            like_count=stat.get("like_count", 0),
            comment_count=stat.get("comment_count", 0),
            share_count=stat.get("share_count", 0),
            user_id=user_id,
        )
        results.append({
            "package_id":    pkg["package_id"],
            "platform":      "tiktok",
            "post_id":       video_id,
            "caption":       pkg.get("caption_text", "")[:60],
            "view_count":    stat.get("view_count", 0),
            "like_count":    stat.get("like_count", 0),
            "comment_count": stat.get("comment_count", 0),
            "share_count":   stat.get("share_count", 0),
        })

    logger.info("Analytics: updated %d package(s).", len(results))
    return results


def display_analytics_table(user_id: int = 1) -> None:
    """
    Print a formatted table of performance data from the database, sorted by
    views descending.
    """
    rows = database.get_performance_summary(user_id=user_id)

    if not rows:
        console.print("[yellow]No analytics data yet. Run [bold]python main.py --analytics[/bold] to fetch.[/yellow]")
        return

    table = Table(
        title="ClipCast Analytics — Post Performance",
        show_lines=True,
    )
    table.add_column("Pkg", style="dim", width=5)
    table.add_column("Platform", style="cyan", width=16)
    table.add_column("Caption", width=40)
    table.add_column("Views", justify="right", style="green")
    table.add_column("Likes", justify="right", style="yellow")
    table.add_column("Comments", justify="right")
    table.add_column("Shares", justify="right")
    table.add_column("Fetched", style="dim", width=18)

    for row in rows:
        table.add_row(
            str(row["package_id"]),
            row.get("platform", ""),
            (row.get("caption_text") or "")[:40],
            f"{row['view_count']:,}",
            f"{row['like_count']:,}",
            f"{row['comment_count']:,}",
            f"{row['share_count']:,}",
            (row.get("fetched_at") or "")[:16],
        )

    console.print(table)


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_posted_packages(user_id: int) -> List[Dict]:
    """Return all packages with status='posted' for a user."""
    import sqlite3, json
    conn = database.get_connection()
    try:
        rows = conn.execute("""
            SELECT package_id, caption_text, tiktok_post_id, yt_shorts_post_id,
                   instagram_post_id, compiled_path
            FROM packages
            WHERE user_id = ? AND status = 'posted'
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _fetch_tiktok_video_stats(access_token: str) -> List[Dict]:
    """
    Fetch the authenticated user's video list with performance stats from
    TikTok's video/list endpoint.

    Returns:
        List of video stat dicts from TikTok. Empty list on failure.
    """
    try:
        resp = requests.get(
            _TIKTOK_VIDEO_LIST_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            params={"fields": _VIDEO_FIELDS},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(
                "TikTok video/list failed (%d): %s",
                resp.status_code, resp.text[:300],
            )
            return []
        return resp.json().get("data", {}).get("videos", [])

    except requests.RequestException as e:
        logger.error("TikTok analytics request failed: %s", e)
        return []


def _sample_analytics(post_id_to_pkg: Dict[str, Dict]) -> List[Dict]:
    """Return synthetic analytics rows for test mode."""
    import random
    results = []
    for post_id, pkg in list(post_id_to_pkg.items())[:5]:
        results.append({
            "package_id":    pkg["package_id"],
            "platform":      "tiktok",
            "post_id":       post_id,
            "caption":       (pkg.get("caption_text") or "")[:60],
            "view_count":    random.randint(100, 50000),
            "like_count":    random.randint(10, 5000),
            "comment_count": random.randint(0, 500),
            "share_count":   random.randint(0, 200),
        })
    return results


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing analytics.py...\n")
    database.initialize_database()

    # Show existing data first
    display_analytics_table()

    print("\nTo fetch fresh data from TikTok:")
    print("  python main.py --analytics")
