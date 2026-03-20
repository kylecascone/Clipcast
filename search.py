"""
search.py
=========
On-demand search for streamers and channels, plus per-streamer/channel clip
fetching. Designed to be called by the web dashboard when a user searches.

Functions:
  search_twitch_streamers      — find Twitch streamers matching a query
  search_youtube_channels      — find YouTube channels matching a query
  fetch_clips_for_streamer     — top clips from a single Twitch username (7 days)
  fetch_clips_for_youtube_channel — recent videos from a single YT channel (7 days)

All functions return normalized dicts compatible with the rest of the pipeline.

SaaS Note:
    All functions accept explicit credentials. In multi-user mode, pass each
    user's stored API keys instead of loading from config.yaml.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_TWITCH_API_BASE = "https://api.twitch.tv/helix"
_YT_API_BASE     = "https://www.googleapis.com/youtube/v3"


# ══════════════════════════════════════════════════════════════════════════════
# Twitch streamer search
# ══════════════════════════════════════════════════════════════════════════════

def search_twitch_streamers(
    query: str,
    limit: int = 20,
    user_config: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """
    Search Twitch for channels matching a query string.

    Args:
        query:       Search term (e.g. "speed", "xqc").
        limit:       Max results to return (default 20, API max 100).
        user_config: Config dict with Twitch credentials. If None, loads
                     from config.yaml.

    Returns:
        List of streamer dicts, each with:
            login         (str)  — Twitch username
            display_name  (str)  — Display name
            broadcaster_id (str) — Twitch user ID
            is_live       (bool) — Whether currently live
            viewer_count  (int)  — Current viewers (0 if not live)
            thumbnail_url (str)  — Profile image URL
            started_at    (str)  — Stream start time (empty if not live)
    """
    if user_config is None:
        from preferences import load_config
        user_config = load_config()

    client_id = user_config["twitch"]["client_id"]
    client_secret = user_config["twitch"]["client_secret"]

    if client_id == "YOUR_TWITCH_CLIENT_ID_HERE":
        raise RuntimeError("Twitch credentials not configured in config.yaml.")

    token = _get_twitch_token(client_id, client_secret)
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{_TWITCH_API_BASE}/search/channels",
        headers=headers,
        params={"query": query, "first": min(limit, 100)},
        timeout=10,
    )
    resp.raise_for_status()

    results = []
    for item in resp.json().get("data", []):
        results.append({
            "login":          item.get("broadcaster_login", ""),
            "display_name":   item.get("display_name", ""),
            "broadcaster_id": item.get("id", ""),
            "is_live":        item.get("is_live", False),
            "viewer_count":   item.get("viewer_count", 0),
            "thumbnail_url":  item.get("thumbnail_url", ""),
            "started_at":     item.get("started_at", ""),
        })

    logger.info("Twitch search '%s': %d results.", query, len(results))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# YouTube channel search
# ══════════════════════════════════════════════════════════════════════════════

def search_youtube_channels(
    query: str,
    limit: int = 20,
    user_config: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """
    Search YouTube for channels matching a query string.

    Args:
        query:       Search term (e.g. "speed", "moist critikal").
        limit:       Max results to return (default 20, API max 50).
        user_config: Config dict with YouTube API key. If None, loads
                     from config.yaml.

    Returns:
        List of channel dicts, each with:
            channel_id        (str) — YouTube channel ID
            title             (str) — Channel name
            description       (str) — Short description
            thumbnail_url     (str) — Channel thumbnail URL
            subscriber_count  (int) — Subscriber count (0 if hidden)
            custom_url        (str) — @handle if set
    """
    if user_config is None:
        from preferences import load_config
        user_config = load_config()

    api_key = user_config["youtube"]["api_key"]

    if api_key == "YOUR_YOUTUBE_DATA_API_KEY_HERE":
        raise RuntimeError("YouTube API key not configured in config.yaml.")

    # Step 1: Search for channels
    search_resp = requests.get(
        f"{_YT_API_BASE}/search",
        params={
            "part": "snippet",
            "type": "channel",
            "q": query,
            "maxResults": min(limit, 50),
            "key": api_key,
        },
        timeout=10,
    )
    search_resp.raise_for_status()
    items = search_resp.json().get("items", [])

    if not items:
        return []

    # Step 2: Batch-fetch subscriber counts
    channel_ids = [i["snippet"]["channelId"] for i in items]
    stats_resp = requests.get(
        f"{_YT_API_BASE}/channels",
        params={
            "part": "statistics,snippet",
            "id": ",".join(channel_ids),
            "key": api_key,
        },
        timeout=10,
    )
    stats_resp.raise_for_status()
    stats_map: Dict[str, Dict] = {
        c["id"]: c for c in stats_resp.json().get("items", [])
    }

    results = []
    for item in items:
        cid = item["snippet"]["channelId"]
        stats = stats_map.get(cid, {})
        snippet = stats.get("snippet", item.get("snippet", {}))
        statistics = stats.get("statistics", {})

        results.append({
            "channel_id":       cid,
            "title":            snippet.get("title", ""),
            "description":      snippet.get("description", "")[:200],
            "thumbnail_url":    (
                snippet.get("thumbnails", {})
                .get("default", {}).get("url", "")
            ),
            "subscriber_count": int(statistics.get("subscriberCount", 0)),
            "custom_url":       snippet.get("customUrl", ""),
        })

    logger.info("YouTube channel search '%s': %d results.", query, len(results))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Per-streamer clip fetch (Twitch, last 7 days)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_clips_for_streamer(
    username: str,
    limit: int = 20,
    user_config: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch top Twitch clips for a single streamer from the last 7 days.

    Bypasses the target_streamers preference — useful for on-demand lookups
    when the user discovers a new streamer via search.

    Args:
        username:    Twitch login name (case-insensitive).
        limit:       Max clips to return (default 20).
        user_config: Config dict with Twitch credentials.

    Returns:
        List of clip dicts in ClipCast standard format (same as fetcher_twitch).
    """
    if user_config is None:
        from preferences import load_config
        user_config = load_config()

    client_id = user_config["twitch"]["client_id"]
    client_secret = user_config["twitch"]["client_secret"]

    token = _get_twitch_token(client_id, client_secret)
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}

    # Resolve username → broadcaster_id
    resp = requests.get(
        f"{_TWITCH_API_BASE}/users",
        headers=headers,
        params={"login": username.lower()},
        timeout=10,
    )
    resp.raise_for_status()
    users = resp.json().get("data", [])
    if not users:
        logger.warning("search: Twitch user '%s' not found.", username)
        return []

    broadcaster_id = users[0]["id"]
    display_name   = users[0].get("display_name", username)

    started_at = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    clips_resp = requests.get(
        f"{_TWITCH_API_BASE}/clips",
        headers=headers,
        params={
            "broadcaster_id": broadcaster_id,
            "started_at":     started_at,
            "first":          min(limit, 100),
        },
        timeout=15,
    )
    clips_resp.raise_for_status()

    clips = []
    for raw in clips_resp.json().get("data", [])[:limit]:
        clips.append({
            "clip_id":      raw.get("id", ""),
            "source":       "twitch",
            "title":        raw.get("title", "Untitled Clip"),
            "url":          raw.get("url", ""),
            "embed_url":    raw.get("embed_url", ""),
            "creator_name": raw.get("broadcaster_name") or display_name,
            "duration":     float(raw.get("duration", 0)),
            "view_count":   int(raw.get("view_count", 0)),
            "created_at":   raw.get("created_at", ""),
            "game_id":      raw.get("game_id", ""),
            "thumbnail_url": raw.get("thumbnail_url", ""),
        })

    logger.info(
        "search: %d Twitch clip(s) for '%s' (last 7 days).",
        len(clips), username,
    )
    return clips


# ══════════════════════════════════════════════════════════════════════════════
# Per-channel clip fetch (YouTube, last 7 days)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_clips_for_youtube_channel(
    channel_id: str,
    limit: int = 20,
    user_config: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch recent short-form videos from a single YouTube channel (last 7 days).

    Bypasses the target_youtube_channels preference — useful for on-demand
    lookups after a search discovery.

    Args:
        channel_id:  YouTube channel ID (UCxxxxxxx...) or @handle.
        limit:       Max videos to return.
        user_config: Config dict with YouTube API key.

    Returns:
        List of clip dicts in ClipCast standard format (same as fetcher_youtube).
    """
    if user_config is None:
        from preferences import load_config
        user_config = load_config()

    api_key = user_config["youtube"]["api_key"]

    # Resolve handle to channel ID if needed
    if not (channel_id.startswith("UC") and len(channel_id) == 24):
        from fetcher_youtube import _resolve_channel_id
        resolved = _resolve_channel_id(channel_id, api_key)
        if not resolved:
            logger.warning("search: could not resolve YouTube channel '%s'.", channel_id)
            return []
        channel_id = resolved

    published_after = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    from fetcher_youtube import _search_channel
    clips = _search_channel(
        channel_id=channel_id,
        api_key=api_key,
        published_after=published_after,
        target_games=[],
        limit=limit,
    )

    logger.info(
        "search: %d YouTube video(s) for channel '%s' (last 7 days).",
        len(clips), channel_id,
    )
    return clips


# ══════════════════════════════════════════════════════════════════════════════
# Auth helper (Twitch client-credentials)
# ══════════════════════════════════════════════════════════════════════════════

_twitch_token_cache: Dict[str, Any] = {}


def _get_twitch_token(client_id: str, client_secret: str) -> str:
    """Obtain/cache a Twitch app access token."""
    now = datetime.now(timezone.utc)
    if (
        _twitch_token_cache.get("access_token")
        and _twitch_token_cache.get("expires_at")
        and now < _twitch_token_cache["expires_at"]
    ):
        return _twitch_token_cache["access_token"]

    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _twitch_token_cache["access_token"] = data["access_token"]
    _twitch_token_cache["expires_at"] = (
        now + timedelta(seconds=data.get("expires_in", 3600) - 60)
    )
    return _twitch_token_cache["access_token"]


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing search.py — searching for 'speed' on both platforms...\n")

    try:
        from preferences import load_config
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # ── Twitch streamer search ─────────────────────────────────────────────────
    print("=" * 60)
    print("Twitch streamers matching 'speed':")
    print("=" * 60)
    try:
        streamers = search_twitch_streamers("speed", limit=5, user_config=config)
        for s in streamers:
            live = "[LIVE]" if s["is_live"] else "      "
            viewers = f"{s['viewer_count']:,}" if s["is_live"] else "—"
            print(f"  {live} {s['display_name']:<30} viewers={viewers}")
    except Exception as e:
        print(f"  Error: {e}")

    print()

    # ── YouTube channel search ─────────────────────────────────────────────────
    print("=" * 60)
    print("YouTube channels matching 'speed':")
    print("=" * 60)
    try:
        channels = search_youtube_channels("speed", limit=5, user_config=config)
        for c in channels:
            subs = f"{c['subscriber_count']:,}" if c["subscriber_count"] else "hidden"
            print(f"  {c['title']:<35} subs={subs}")
    except Exception as e:
        print(f"  Error: {e}")

    print("\nSearch test complete.")
