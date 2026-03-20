"""
fetcher_kick.py
===============
Fetches viral clips from Kick.com using the public Kick.com v2 API.

No authentication required — Kick's public API returns live channels and
clips without API keys. yt-dlp natively supports Kick.com URLs, so clips
discovered here go through the standard editor pipeline with no special
download handling needed.

Kick.com is a rapidly growing streaming platform where many high-traffic
English-speaking creators (xQc, Adin Ross, TrainwrecksTv, etc.) either
stream exclusively or simulcast, producing content that performs very well
on TikTok.

Fetch strategy (whitelist-only):
    1. Fetch clips from every channel in TIER1_KICK (viral_creators.py).
    2. Normalise to ClipCast standard format.
    No live channel discovery — that path pulled foreign-language content.

Note on rate limiting:
    Kick's API does not publish rate-limit documentation. We add a small
    delay between requests and cap batch sizes conservatively to avoid
    being blocked.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── API constants ──────────────────────────────────────────────────────────────
_KICK_API_BASE     = "https://kick.com/api/v2"
_REQUEST_TIMEOUT   = 15
_INTER_REQUEST_DELAY = 0.5   # seconds between API calls — be a good citizen
_MIN_VIEWER_COUNT  = 1_000   # Only fetch clips from channels with 1k+ viewers

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/121.0.0.0 Safari/537.36",
}


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def fetch_kick_pool(
    tier1_channels: Optional[List[str]] = None,
    max_clips_per_channel: int = 5,
    max_channels: int = 20,
) -> List[Dict[str, Any]]:
    """
    Fetch viral clips from Kick.com and return them in ClipCast standard format.

    WHITELIST-ONLY: only fetches clips from the TIER1_KICK creator list.
    No live channel discovery, no unknown channels, no foreign-language content.

    Args:
        tier1_channels:       List of Kick channel slugs to fetch.
                              Defaults to TIER1_KICK from viral_creators.py.
        max_clips_per_channel: Maximum clips to fetch per channel (default 5).
        max_channels:         Unused — kept for API compatibility.

    Returns:
        List of clip dicts in ClipCast standard format, sorted by view_count DESC.
    """
    try:
        from viral_creators import TIER1_KICK
        if tier1_channels is None:
            tier1_channels = list(TIER1_KICK)
    except ImportError:
        if tier1_channels is None:
            tier1_channels = []

    all_clips: List[Dict[str, Any]] = []
    seen_clip_ids: set = set()

    # Whitelist-only: fetch only from the explicit TIER1_KICK creator list.
    # No live channel discovery — that pulls unknown/foreign-language channels.
    logger.info(
        "fetch_kick_pool: fetching clips from %d TIER1_KICK channel(s) (whitelist-only)…",
        len(tier1_channels),
    )
    for slug in tier1_channels:
        try:
            clips = _fetch_clips_for_channel(slug, limit=max_clips_per_channel)
            for clip in clips:
                if clip["clip_id"] not in seen_clip_ids:
                    seen_clip_ids.add(clip["clip_id"])
                    all_clips.append(clip)
            time.sleep(_INTER_REQUEST_DELAY)
        except Exception as exc:
            logger.debug("fetch_kick_pool: error fetching TIER1 channel '%s': %s", slug, exc)

    # Sort by view_count descending
    all_clips.sort(key=lambda c: c.get("view_count", 0), reverse=True)

    logger.info(
        "fetch_kick_pool: complete — %d unique clip(s) from %d TIER1 channel(s).",
        len(all_clips), len(tier1_channels),
    )
    return all_clips


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_live_channels(
    limit: int = 20,
    min_viewer_count: int = _MIN_VIEWER_COUNT,
) -> List[Dict[str, Any]]:
    """
    Return currently live Kick channels with at least min_viewer_count viewers.

    Uses the /channels/live endpoint if available, otherwise falls back to
    fetching the featured channels listing.

    Returns:
        List of channel dicts with at minimum: slug, viewer_count, user_username.
    """
    try:
        resp = requests.get(
            f"{_KICK_API_BASE}/channels",
            params={
                "limit": min(limit * 3, 100),  # Over-fetch to compensate for filtering
                "sort": "viewer_count",
            },
            headers=_HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        channels = resp.json()
        if not isinstance(channels, list):
            channels = channels.get("data", [])

        # Filter to live channels with sufficient viewers
        live = [
            ch for ch in channels
            if ch.get("is_live", False)
            and int(ch.get("viewer_count") or ch.get("viewers_count") or 0)
               >= min_viewer_count
        ]
        return live[:limit]

    except Exception as exc:
        logger.debug("_fetch_live_channels error: %s", exc)
        return []


def _fetch_clips_for_channel(
    channel_slug: str,
    limit: int = 5,
    max_age_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Fetch the most-viewed recent clips for a single Kick channel.

    Args:
        channel_slug: Kick channel slug (lowercase URL-safe name).
        limit:        Maximum number of clips to return.
        max_age_days: Only include clips newer than this many days.

    Returns:
        List of normalised clip dicts in ClipCast standard format.
    """
    try:
        resp = requests.get(
            f"{_KICK_API_BASE}/channels/{channel_slug}/clips",
            params={
                "sort": "view_count",
                "time": "week",
                "limit": min(limit * 2, 20),
            },
            headers=_HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            logger.debug("_fetch_clips_for_channel: channel '%s' not found (404).", channel_slug)
            return []
        resp.raise_for_status()

        data = resp.json()
        raw_clips = data if isinstance(data, list) else data.get("clips", data.get("data", []))

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        clips = []
        for raw in raw_clips[:limit * 2]:
            clip = _normalize_clip(raw, channel_slug)
            if clip is None:
                continue
            # Age filter
            created = clip.get("created_at", "")
            if created:
                try:
                    dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            clips.append(clip)
            if len(clips) >= limit:
                break

        return clips

    except Exception as exc:
        logger.debug("_fetch_clips_for_channel error for '%s': %s", channel_slug, exc)
        return []


def _normalize_clip(raw: Dict[str, Any], channel_slug: str) -> Optional[Dict[str, Any]]:
    """
    Convert a raw Kick API clip object to the ClipCast standard format.

    Returns None if the clip is missing essential fields (id, url).
    """
    # Kick clip IDs may be under different keys depending on API version
    clip_id = (
        raw.get("id") or
        raw.get("clip_id") or
        raw.get("uuid") or
        ""
    )
    if not clip_id:
        return None

    # Clip URL — Kick clips have a clip URL and a download URL
    url = (
        raw.get("clip_url") or
        raw.get("url") or
        raw.get("playback_url") or
        ""
    )
    if not url:
        # Construct URL from channel slug and clip_id if not directly available
        url = f"https://kick.com/{channel_slug}?clip={clip_id}"

    # Creator name
    creator = (
        raw.get("channel", {}).get("user", {}).get("username") or
        raw.get("channel_slug") or
        channel_slug
    )

    # Duration
    duration = float(raw.get("duration", 0) or 0)
    if duration == 0:
        # Some responses embed duration in clip metadata
        duration = float(raw.get("clip_length", 0) or 0)

    # View count
    view_count = int(raw.get("view_count", 0) or raw.get("views", 0) or 0)

    # Title
    title = raw.get("title") or raw.get("clip_title") or "Kick Clip"

    return {
        "clip_id":      f"kick_{clip_id}",
        "source":       "kick",
        "title":        title,
        "url":          url,
        "creator_name": creator,
        "duration":     duration,
        "view_count":   view_count,
        "created_at":   raw.get("created_at", ""),
        "thumbnail_url": raw.get("thumbnail_url", ""),
        "language":     "en",   # Kick is primarily English; filter applied at channel level
    }


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing fetcher_kick.py…\n")
    print("Fetching from Tier 1 Kick channels (max 3 clips per channel)…\n")

    try:
        clips = fetch_kick_pool(max_clips_per_channel=3, max_channels=5)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not clips:
        print(
            "No clips returned. This may mean:\n"
            "  • Kick API is unavailable or changed\n"
            "  • The target channels have no recent clips\n"
            "  • Network connectivity issue"
        )
    else:
        print(f"Fetched {len(clips)} clip(s):\n")
        for clip in clips[:10]:
            print(f"  [{clip['source']}] {clip['creator_name']} — {clip['title'][:55]}")
            print(f"    Duration: {clip['duration']:.0f}s | Views: {clip['view_count']:,}")
            print(f"    URL: {clip['url'][:80]}\n")

    print("Kick fetcher test complete.")
