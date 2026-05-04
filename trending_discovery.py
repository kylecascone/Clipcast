"""
trending_discovery.py
=====================
Dynamic creator discovery — keeps trending_creators table fresh.

Runs on a 6-hour cadence alongside pool refresh.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import database

logger = logging.getLogger(__name__)


def refresh_twitch_trending(user_config: Dict, top_n: int = 50) -> int:
    """
    Fetch top N live Twitch streamers by viewer count and upsert into trending_creators.
    Returns number of creators updated.
    """
    # fetch_trending_streamers takes (client_id, access_token, count)
    try:
        twitch_cfg = user_config.get("twitch", {})
        client_id = twitch_cfg.get("client_id", "")
        client_secret = twitch_cfg.get("client_secret", "")
        if not client_id or not client_secret:
            logger.warning("refresh_twitch_trending: Twitch credentials not configured")
            return 0

        from fetcher_twitch import fetch_trending_streamers, _get_access_token
        access_token = _get_access_token(client_id, client_secret)
        if not access_token:
            logger.warning("refresh_twitch_trending: could not get Twitch token")
            return 0
        streamers = fetch_trending_streamers(client_id, access_token, count=top_n)
    except Exception as exc:
        logger.warning("refresh_twitch_trending: fetch failed: %s", exc)
        return 0

    if not streamers:
        return 0

    conn = database.get_connection()
    updated = 0
    try:
        for rank, s in enumerate(streamers[:top_n], start=1):
            name = (s.get("user_name") or s.get("user_login") or s.get("login") or "").strip()
            if not name:
                continue
            viewer_count = int(s.get("viewer_count") or 0)
            category = s.get("game_name") or s.get("game") or ""
            conn.execute(
                """INSERT INTO trending_creators (creator_name, platform, viewer_count, rank, category, last_updated)
                   VALUES (?, 'twitch', ?, ?, ?, datetime('now'))
                   ON CONFLICT(creator_name) DO UPDATE SET
                     viewer_count = excluded.viewer_count,
                     rank = excluded.rank,
                     category = excluded.category,
                     last_updated = datetime('now')""",
                (name, viewer_count, rank, category),
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()

    logger.info("refresh_twitch_trending: updated %d creators", updated)
    return updated


def boost_creator_from_reddit(creator_name: str, upvotes: int, platform: str = "twitch") -> None:
    """
    Boost a creator's viral_signal_boost score when they appear in a Reddit post.
    Higher upvotes = larger boost. Boost decays over time (not implemented here —
    just adds to existing score, capped at 100).
    """
    if not creator_name or upvotes < 1000:
        return
    boost = min(upvotes / 10000.0 * 10, 10.0)  # 0–10 pts per mention, capped
    try:
        conn = database.get_connection()
        conn.execute(
            """INSERT INTO trending_creators (creator_name, platform, viral_signal_boost, last_updated)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(creator_name) DO UPDATE SET
                 viral_signal_boost = MIN(100.0, viral_signal_boost + ?),
                 last_updated = datetime('now')""",
            (creator_name, platform, boost, boost),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("boost_creator_from_reddit: %s", exc)


def get_top_trending_creators(limit: int = 25, max_age_hours: int = 24) -> List[Dict]:
    """
    Return top creators sorted by (viewer_count + viral_signal_boost * 1000) DESC.
    Only includes creators updated within the last max_age_hours.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
    try:
        conn = database.get_connection()
        rows = conn.execute(
            """SELECT creator_name, platform, viewer_count, viral_signal_boost, rank, category
               FROM trending_creators
               WHERE last_updated >= ?
               ORDER BY (viewer_count + viral_signal_boost * 1000) DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("get_top_trending_creators: %s", exc)
        return []


def extract_creator_from_reddit_title(title: str) -> Optional[str]:
    """
    Try to extract a creator/streamer name from a Reddit post title.
    Looks for patterns like 'xQc', '@username', or 'streamer reacts to'.
    Returns the name if found, else None.
    """
    import re
    # Match @mentions
    m = re.search(r'@([A-Za-z0-9_]{3,25})', title)
    if m:
        return m.group(1)
    # Match "Streamer reacts" / "Creator goes viral" patterns
    m = re.search(r'^([A-Za-z0-9_]{3,20})\s+(reacts?|goes|gets|streams?|plays?|wins?|loses?|says|calls?)', title, re.I)
    if m:
        return m.group(1)
    return None


def refresh_all_trending(user_config: Dict) -> Dict:
    """
    Main entry point — refresh Twitch trending list.
    Called every 6 hours from pool_fetcher.refresh_all_pools().
    Returns result summary dict.
    """
    result = {"twitch_updated": 0}
    try:
        result["twitch_updated"] = refresh_twitch_trending(user_config, top_n=50)
    except Exception as exc:
        logger.warning("refresh_all_trending failed: %s", exc)
    return result


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print("trending_discovery.py — self-test\n")

    # Test extract_creator_from_reddit_title
    tests = [
        ("xQc reacts to this insane clip",           "xQc"),
        ("@kai_cenat goes viral again today",         "kai_cenat"),
        ("Unrelated title about nothing",             None),
    ]
    print("extract_creator_from_reddit_title:")
    for title, expected in tests:
        result = extract_creator_from_reddit_title(title)
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] '{title[:50]}' → {result!r} (expected {expected!r})")

    print()
    print("get_top_trending_creators (DB query):")
    creators = get_top_trending_creators(limit=5)
    if creators:
        for c in creators:
            print(f"  {c['creator_name']} — {c['viewer_count']:,} viewers  rank={c['rank']}")
    else:
        print("  (no trending data yet — run refresh_all_trending() first)")
