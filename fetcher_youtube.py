"""
fetcher_youtube.py
==================
Fetches top recent clips and highlight videos from YouTube channels
using the YouTube Data API v3.

SaaS Note:
    All functions accept optional user_config / user_prefs parameters.
    In single-user mode these are loaded from YAML files automatically.
    In multi-user mode, pass in the user's stored credentials and preferences.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

# ── YouTube API base ───────────────────────────────────────────────────────────
_YT_API_BASE = "https://www.googleapis.com/youtube/v3"

# Clip length limits for YouTube search
# YouTube "short" videos: under 4 minutes. We focus on clips 15–180 s.
_MIN_DURATION_SEC = 15
_MAX_DURATION_SEC = 180

# Keywords to look for in titles / descriptions when filtering for gaming clips
_HIGHLIGHT_KEYWORDS = [
    "clip", "highlight", "moment", "play", "reaction",
    "insane", "crazy", "epic", "best", "top",
]


# ══════════════════════════════════════════════════════════════════════════════
# Duration parsing
# ══════════════════════════════════════════════════════════════════════════════

def _parse_iso8601_duration(duration_str: str) -> float:
    """
    Convert ISO 8601 duration string (e.g. "PT1M30S") to seconds (float).

    YouTube returns durations in ISO 8601 format in the contentDetails field.
    """
    if not duration_str or not duration_str.startswith("PT"):
        return 0.0

    import re
    pattern = re.compile(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?"
    )
    match = pattern.match(duration_str)
    if not match:
        return 0.0

    hours   = float(match.group(1) or 0)
    minutes = float(match.group(2) or 0)
    seconds = float(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


# ══════════════════════════════════════════════════════════════════════════════
# Channel ID resolution
# ══════════════════════════════════════════════════════════════════════════════

# Process-level cache: handle/name → channel_id.
# Avoids burning quota re-resolving the same channel every pipeline run.
_channel_id_cache: Dict[str, str] = {}


def _resolve_channel_id(
    channel_handle_or_id: str,
    api_key: str,
) -> Optional[str]:
    """
    Resolve a YouTube channel handle, legacy username, or bare name to a
    channel ID using a three-strategy cascade.

    Strategy 1 — channels?forHandle  : official handle lookup (works when the
                                        channel has a verified @ handle set).
    Strategy 2 — search?type=channel  : searches YouTube by the handle string;
                                        reliable for all modern @ handles.
    Strategy 3 — channels?forUsername : legacy YouTube username lookup (only
                                        works for old-style accounts).

    Results are cached in-process so the same channel is only resolved once
    per run, saving API quota.

    Args:
        channel_handle_or_id: e.g. "@MoistCr1TiKaL", "MoistCr1TiKaL",
                               or an explicit channel ID like "UCxxxxxx...".
        api_key: YouTube Data API v3 key.

    Returns:
        Channel ID string (e.g. "UC7_YgWpupZT9xMYMZaVd7YA"), or None if
        the channel could not be resolved via any strategy.
    """
    original = channel_handle_or_id.strip()

    # ── Fast path: already a channel ID ───────────────────────────────────────
    # YouTube channel IDs start with "UC" and are exactly 24 characters.
    if original.startswith("UC") and len(original) == 24:
        return original

    # ── Cache hit ─────────────────────────────────────────────────────────────
    cache_key = original.lower()
    if cache_key in _channel_id_cache:
        logger.debug("Channel ID cache hit for '%s'", original)
        return _channel_id_cache[cache_key]

    # Normalise: strip leading @ for use in requests that don't want it
    handle_bare = original.lstrip("@")
    # Always send the @ prefix to the forHandle endpoint
    handle_at   = f"@{handle_bare}"

    channel_id: Optional[str] = None

    # ── Strategy 1: channels?forHandle ────────────────────────────────────────
    # This is the documented endpoint for @ handles but silently returns []
    # for some channels (particularly those that haven't set a custom handle).
    try:
        resp = requests.get(
            f"{_YT_API_BASE}/channels",
            params={"part": "id", "forHandle": handle_at, "key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            channel_id = items[0]["id"]
            logger.debug(
                "Resolved '%s' via forHandle → %s", original, channel_id
            )
    except requests.RequestException as e:
        logger.debug("Strategy 1 (forHandle) request error for '%s': %s", original, e)

    # ── Strategy 2: search?type=channel&q=@handle ─────────────────────────────
    # Uses YouTube's search index, which reliably finds channels by their
    # displayed @ handle. We verify the result by checking channelId.
    if not channel_id:
        try:
            resp = requests.get(
                f"{_YT_API_BASE}/search",
                params={
                    "part": "snippet",
                    "type": "channel",
                    "q": handle_at,        # search for "@ChannelName"
                    "maxResults": 5,
                    "key": api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])

            for item in items:
                snippet      = item.get("snippet", {})
                result_title = snippet.get("title", "").lower()
                result_custom = snippet.get("customUrl", "").lower().lstrip("@")
                search_bare   = handle_bare.lower()

                # Accept if the channel title or customUrl closely matches
                # the handle we searched for (avoid false positives).
                if (
                    result_custom == search_bare or
                    result_title  == search_bare or
                    search_bare   in result_custom
                ):
                    channel_id = item["snippet"]["channelId"]
                    logger.debug(
                        "Resolved '%s' via search (title='%s', customUrl='%s') → %s",
                        original, snippet.get("title"), snippet.get("customUrl"),
                        channel_id,
                    )
                    break

            # If nothing matched by name, fall back to the top search result
            # only when there was exactly one result (high confidence).
            if not channel_id and len(items) == 1:
                channel_id = items[0]["snippet"]["channelId"]
                logger.debug(
                    "Resolved '%s' via search (single result fallback) → %s",
                    original, channel_id,
                )

        except requests.RequestException as e:
            logger.debug("Strategy 2 (search) request error for '%s': %s", original, e)

    # ── Strategy 3: channels?forUsername ──────────────────────────────────────
    # Only works for old-style YouTube accounts that have a legacy username.
    # Kept as a last resort for maximum compatibility.
    if not channel_id:
        try:
            resp = requests.get(
                f"{_YT_API_BASE}/channels",
                params={"part": "id", "forUsername": handle_bare, "key": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if items:
                channel_id = items[0]["id"]
                logger.debug(
                    "Resolved '%s' via forUsername → %s", original, channel_id
                )
        except requests.RequestException as e:
            logger.debug(
                "Strategy 3 (forUsername) request error for '%s': %s", original, e
            )

    # ── Result ─────────────────────────────────────────────────────────────────
    if channel_id:
        _channel_id_cache[cache_key] = channel_id
        return channel_id

    logger.error(
        "Could not resolve YouTube channel '%s'.\n"
        "  Tried: forHandle, search, and forUsername — all returned no results.\n"
        "  Check that:\n"
        "    • The channel handle is spelled correctly (e.g. @MoistCr1TiKaL)\n"
        "    • The channel exists and is not private\n"
        "    • Your YouTube API key has the YouTube Data API v3 enabled\n"
        "    • You have not exhausted today's API quota (10,000 units/day default)",
        original,
    )
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Core fetch
# ══════════════════════════════════════════════════════════════════════════════

def fetch_clips(
    user_config: Optional[Dict] = None,
    user_prefs: Optional[Dict] = None,
    limit_per_channel: int = 20,
    mode: str = "user",
) -> List[Dict[str, Any]]:
    """
    Fetch top recent clips and highlight videos from configured YouTube channels.

    Searches each target channel for videos uploaded in the last 24 hours,
    then filters by duration, game keywords, and relevance score.

    Args:
        user_config: Dict with YouTube API credentials. If None, loads from
                     config.yaml automatically.
        user_prefs:  Dict with user preferences. If None, loads from
                     preferences.yaml automatically.
        limit_per_channel: Max videos to return per channel (default 20).
        mode: 'user' (default) — fetch from configured target_youtube_channels.
              'pool' — skip target channels and run only the global pool
                       discovery methods. Used by pool_fetcher.py.

    Returns:
        List of clip dicts, each containing:
            clip_id       (str)   — YouTube video ID
            source        (str)   — always "youtube"
            title         (str)   — video title
            url           (str)   — full YouTube watch URL
            creator_name  (str)   — channel title
            duration      (float) — duration in seconds
            view_count    (int)   — view count
            like_count    (int)   — like count
            created_at    (str)   — ISO 8601 publish date
            thumbnail_url (str)   — thumbnail URL
            channel_id    (str)   — YouTube channel ID
    """
    # ── Load config / prefs if not provided ───────────────────────────────────
    if user_config is None or user_prefs is None:
        from preferences import load_config, load_preferences
    if user_config is None:
        user_config = load_config()
    if user_prefs is None:
        user_prefs = load_preferences()

    api_key: str = user_config["youtube"]["api_key"]

    if api_key == "YOUR_YOUTUBE_DATA_API_KEY_HERE":
        raise RuntimeError(
            "YouTube API key not configured. "
            "Edit config.yaml and fill in your youtube.api_key."
        )

    target_channels: List[str] = user_prefs.get("target_youtube_channels", [])
    target_games: List[str] = [g.lower() for g in user_prefs.get("target_games", [])]

    # In pool mode, skip per-channel fetching entirely.
    # pool_fetcher.py calls this with mode='pool' to run only the global pool methods.
    if mode == "pool":
        target_channels = []
    elif not target_channels:
        logger.warning("No target_youtube_channels configured in preferences.")
        return []

    # Build the lookback window from preferences.
    # youtube_lookback_days defaults to 14 — YouTube content stays relevant
    # much longer than Twitch clips, so a wider window gives a larger pool.
    # Clamped to 1–30 to avoid absurdly large or zero windows.
    lookback_days = int(user_prefs.get("youtube_lookback_days", 14))
    lookback_days = max(1, min(30, lookback_days))
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info("YouTube lookback window: %d day(s) (published after %s)", lookback_days, published_after)

    all_clips: List[Dict[str, Any]] = []
    seen_video_ids: set = set()

    for channel_handle in target_channels:
        logger.info("Fetching clips for YouTube channel: %s", channel_handle)

        channel_id = _resolve_channel_id(channel_handle, api_key)
        if not channel_id:
            continue

        videos = _search_channel(
            channel_id=channel_id,
            api_key=api_key,
            published_after=published_after,
            target_games=target_games,
            limit=limit_per_channel,
        )

        for video in videos:
            vid_id = video.get("clip_id", "")
            if vid_id and vid_id not in seen_video_ids:
                seen_video_ids.add(vid_id)
                all_clips.append(video)

        logger.debug("  → %d video(s) from %s", len(videos), channel_handle)

    # ── Trending Discovery — DISABLED ─────────────────────────────────────────
    # YouTube trending and global pool discovery are permanently disabled to
    # prevent foreign-language content from entering the pipeline.
    # pool_fetcher.py fetches exclusively from TIER1_YOUTUBE channel IDs via
    # _search_channel(). No trending chart, no search queries, no related
    # channel discovery runs under any circumstances.
    # (allow_youtube_trending and youtube_global_pool_enabled are ignored.)

    # Sort by view_count descending
    all_clips.sort(key=lambda c: c.get("view_count", 0), reverse=True)

    # ── Blocked channel/creator filter ────────────────────────────────────────
    try:
        from blocked_creators import is_blocked
        import audit
        passing = []
        for clip in all_clips:
            creator = clip.get("creator_name", "")
            channel = clip.get("channel_id", "")
            # Check both the channel display name and channel ID
            if is_blocked(creator, "youtube") or is_blocked(channel, "youtube"):
                logger.debug("Skipping opted-out YouTube creator: %s", creator or channel)
                audit.log_blocked_creator(creator or channel, "youtube")
            else:
                passing.append(clip)
        all_clips = passing
    except ImportError:
        pass

    # ── Minimum views filter ───────────────────────────────────────────────────
    min_views = user_prefs.get("minimum_views", 0)
    if min_views > 0:
        before = len(all_clips)
        all_clips = [c for c in all_clips if c.get("view_count", 0) >= min_views]
        if len(all_clips) < before:
            logger.debug(
                "minimum_views filter (%d): removed %d low-view clips.",
                min_views, before - len(all_clips),
            )

    # ── Audit logging ──────────────────────────────────────────────────────────
    try:
        import audit
        audit.log_fetch_batch(all_clips)
    except ImportError:
        pass

    logger.info(
        "YouTube fetch complete. %d unique video(s) total (%d channel(s) + trending).",
        len(all_clips), len(target_channels),
    )
    return all_clips


def _search_channel(
    channel_id: str,
    api_key: str,
    published_after: str,
    target_games: List[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Search a single YouTube channel for recent short-form clips.

    Step 1: Search API to find video IDs published in last 24h.
    Step 2: Videos.list API to get duration and statistics.
    Step 3: Filter by duration and game keywords.

    Returns:
        List of normalized clip dicts.
    """
    # ── Step 1: Search for recent videos ──────────────────────────────────────
    search_params: Dict[str, Any] = {
        "part": "id,snippet",
        "channelId": channel_id,
        "type": "video",
        "order": "viewCount",
        "publishedAfter": published_after,
        "videoDuration": "short",   # YouTube "short" = under 4 minutes
        "maxResults": min(50, limit * 2),  # Fetch extra to allow for filtering
        "relevanceLanguage": "en",  # Prefer English content
        "regionCode": "US",         # US region for maximum viral relevance
        "key": api_key,
    }

    # Add game keyword to search query if target_games is set
    if target_games:
        search_params["q"] = " OR ".join(target_games[:5])  # API supports OR

    # ── Quota pre-check (search.list costs 100 units) ─────────────────────────
    try:
        from rate_limiter import check_youtube_quota, record_youtube_quota
        if not check_youtube_quota("search.list"):
            logger.warning("YouTube quota exhausted — skipping channel search.")
            return []
    except ImportError:
        check_youtube_quota = record_youtube_quota = None

    resp = requests.get(
        f"{_YT_API_BASE}/search",
        params=search_params,
        timeout=15,
    )
    resp.raise_for_status()
    try:
        if record_youtube_quota:
            record_youtube_quota("search.list")
    except Exception:
        pass

    search_items = resp.json().get("items", [])
    if not search_items:
        return []

    video_ids = [item["id"]["videoId"] for item in search_items if "videoId" in item.get("id", {})]
    if not video_ids:
        return []

    # ── Step 2: Fetch full video details (duration, stats) ────────────────────
    details_resp = requests.get(
        f"{_YT_API_BASE}/videos",
        params={
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(video_ids),
            "key": api_key,
        },
        timeout=15,
    )
    details_resp.raise_for_status()
    try:
        if record_youtube_quota:
            record_youtube_quota("videos.list")
    except Exception:
        pass
    detail_items = details_resp.json().get("items", [])

    # ── Step 3: Filter by duration, game keyword, and enforce limit ───────────
    clips = []
    for item in detail_items:
        duration_iso = item.get("contentDetails", {}).get("duration", "")
        duration_sec = _parse_iso8601_duration(duration_iso)

        if not (_MIN_DURATION_SEC <= duration_sec <= _MAX_DURATION_SEC):
            continue

        snippet = item.get("snippet", {})
        title = snippet.get("title", "")
        description = snippet.get("description", "")

        # Filter by game name in title or description when target_games is set
        if target_games:
            combined = (title + " " + description).lower()
            if not any(game in combined for game in target_games):
                continue

        clips.append(_normalize_video(item, duration_sec))

        if len(clips) >= limit:
            break

    return clips


def _fetch_video_details(
    video_ids: List[str],
    api_key: str,
) -> List[Dict[str, Any]]:
    """
    Fetch full video details for a list of video IDs and return normalized
    clip dicts that fall within the target duration range.

    Calls videos?part=snippet,contentDetails,statistics and filters by
    _MIN_DURATION_SEC / _MAX_DURATION_SEC. Does NOT record quota — callers
    are responsible for recording videos.list (1 unit) when appropriate.

    Args:
        video_ids: List of YouTube video ID strings.
        api_key:   YouTube Data API v3 key.

    Returns:
        List of clip dicts in ClipCast standard format.
    """
    if not video_ids:
        return []

    try:
        resp = requests.get(
            f"{_YT_API_BASE}/videos",
            params={
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(video_ids),
                "key": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("_fetch_video_details request failed: %s", e)
        return []

    clips = []
    for item in resp.json().get("items", []):
        duration_iso = item.get("contentDetails", {}).get("duration", "")
        duration_sec = _parse_iso8601_duration(duration_iso)
        if _MIN_DURATION_SEC <= duration_sec <= _MAX_DURATION_SEC:
            clips.append(_normalize_video(item, duration_sec))

    return clips


def fetch_global_youtube_pool(
    api_key: str,
    target_channels: List[str],
    published_after: str,
    target_games: List[str],
    search_queries: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Discover viral content beyond the user's configured channel list.

    Runs three discovery methods in sequence and returns a deduplicated pool
    of clips in ClipCast standard format:

    Method 1 — Trending chart    : YouTube mostPopular chart, up to 50 videos.
    Method 2 — Viral search      : Up to 5 configurable search queries that
                                   target the content most likely to go viral
                                   on TikTok. Each query returns up to 10
                                   results sorted by viewCount from the
                                   lookback window.
    Method 3 — Related channels  : For each channel in target_channels, finds
                                   3 similar channels by topic via the search
                                   endpoint, then pulls the top short video
                                   from each within the lookback window.

    All three methods share a deduplication set — a video ID found by one
    method is never returned again by another.

    Quota usage per run (approximate):
        Method 1 : 1 unit   (videos.list chart call, no search)
        Method 2 : ~505 units  (100 per search.list × 5 queries + 1×5 videos)
        Method 3 : varies — 1 unit per channel title lookup (channels.list),
                   100 per similar-channel search, 100 per top-video search,
                   1 per details fetch. Guarded by check_youtube_quota().

    Args:
        api_key:         YouTube Data API v3 key.
        target_channels: Channel handles or IDs from preferences.yaml.
                         Used by Method 3 for related-channel discovery.
        published_after: RFC3339 datetime string — only videos newer than this.
        target_games:    Lower-cased game names. Empty list = no content filter.
        search_queries:  Viral search queries for Method 2. Defaults to 5
                         broad viral-content queries when None.

    Returns:
        List of clip dicts in ClipCast standard format, deduped by video ID.
    """
    _DEFAULT_QUERIES = [
        "reaction video compilation",
        "gaming highlights best moments",
        "funny moments compilation",
        "shocking moments caught on camera",
        "sports highlights best plays",
    ]
    queries = search_queries if search_queries else _DEFAULT_QUERIES

    pool: List[Dict[str, Any]] = []
    seen_ids: set = set()

    def _add(clips: List[Dict[str, Any]]) -> int:
        """Deduplicate-add clips to pool. Returns count added."""
        added = 0
        for clip in clips:
            vid_id = clip.get("clip_id", "")
            if vid_id and vid_id not in seen_ids:
                seen_ids.add(vid_id)
                pool.append(clip)
                added += 1
        return added

    # Lazy-import quota helpers so this module stays functional without rate_limiter
    try:
        from rate_limiter import check_youtube_quota, record_youtube_quota
        _qcheck = check_youtube_quota
        _qrec = record_youtube_quota
    except ImportError:
        _qcheck = lambda *_: True   # type: ignore[assignment]
        _qrec   = lambda *_: None   # type: ignore[assignment]

    # ── Method 1: Trending chart ───────────────────────────────────────────────
    logger.info("Global YouTube pool — Method 1: trending chart (limit=50)…")
    try:
        trending = fetch_trending_clips(api_key=api_key, target_games=[], limit=50)
        n = _add(trending)
        logger.info("  Method 1: added %d clip(s) from trending chart.", n)
    except Exception as exc:
        logger.warning("Global pool Method 1 (trending) failed: %s", exc)

    # ── Method 2: Viral search queries ────────────────────────────────────────
    logger.info(
        "Global YouTube pool — Method 2: viral search queries (%d queries)…",
        len(queries),
    )
    for query in queries:
        if not _qcheck("search.list"):
            logger.warning("YouTube quota exhausted — stopping Method 2 search queries.")
            break

        try:
            search_resp = requests.get(
                f"{_YT_API_BASE}/search",
                params={
                    "part": "id",
                    "type": "video",
                    "q": query,
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "videoDuration": "short",
                    "maxResults": 10,
                    "relevanceLanguage": "en",
                    "regionCode": "US",
                    "key": api_key,
                },
                timeout=15,
            )
            search_resp.raise_for_status()
            _qrec("search.list")

            video_ids = [
                item["id"]["videoId"]
                for item in search_resp.json().get("items", [])
                if "videoId" in item.get("id", {})
            ]

            if video_ids:
                details = _fetch_video_details(video_ids, api_key)
                n = _add(details)
                _qrec("videos.list")
                logger.debug("  Method 2 query '%s': added %d clip(s).", query, n)

        except Exception as exc:
            logger.warning("Global pool Method 2 query '%s' failed: %s", query, exc)

    # ── Method 3: Related channel discovery ───────────────────────────────────
    logger.info("Global YouTube pool — Method 3: related channel discovery…")
    for channel_handle in target_channels:
        if not _qcheck("search.list"):
            logger.warning("YouTube quota exhausted — stopping Method 3 discovery.")
            break

        # Resolve handle/ID → channel ID (uses in-process cache, cheap on repeat)
        channel_id = _resolve_channel_id(channel_handle, api_key)
        if not channel_id:
            continue

        # Fetch channel title to use as similarity search query (channels.list = 1 unit)
        if not _qcheck("channels.list"):
            break
        try:
            ch_resp = requests.get(
                f"{_YT_API_BASE}/channels",
                params={"part": "snippet", "id": channel_id, "key": api_key},
                timeout=10,
            )
            ch_resp.raise_for_status()
            _qrec("channels.list")
            ch_items = ch_resp.json().get("items", [])
            if not ch_items:
                continue
            channel_title = ch_items[0].get("snippet", {}).get("title", "")
            if not channel_title:
                continue
        except Exception as exc:
            logger.debug("Could not get title for channel %s: %s", channel_handle, exc)
            continue

        # Find 3 similar channels by topic (search.list = 100 units)
        try:
            sim_resp = requests.get(
                f"{_YT_API_BASE}/search",
                params={
                    "part": "snippet",
                    "type": "channel",
                    "q": channel_title,
                    "maxResults": 5,
                    "relevanceLanguage": "en",
                    "regionCode": "US",
                    "key": api_key,
                },
                timeout=10,
            )
            sim_resp.raise_for_status()
            _qrec("search.list")

            similar_ids = [
                item["snippet"]["channelId"]
                for item in sim_resp.json().get("items", [])
                if item["snippet"].get("channelId") != channel_id
            ][:3]

        except Exception as exc:
            logger.debug(
                "Could not find similar channels for %s: %s", channel_handle, exc
            )
            continue

        # Pull top short video from each similar channel within the lookback window
        for sim_id in similar_ids:
            if not _qcheck("search.list"):
                break
            try:
                vid_resp = requests.get(
                    f"{_YT_API_BASE}/search",
                    params={
                        "part": "id",
                        "channelId": sim_id,
                        "type": "video",
                        "order": "viewCount",
                        "publishedAfter": published_after,
                        "videoDuration": "short",
                        "maxResults": 1,
                        "relevanceLanguage": "en",
                        "regionCode": "US",
                        "key": api_key,
                    },
                    timeout=10,
                )
                vid_resp.raise_for_status()
                _qrec("search.list")

                vid_items = vid_resp.json().get("items", [])
                if not vid_items:
                    continue
                video_id = vid_items[0].get("id", {}).get("videoId")
                if not video_id:
                    continue

                details = _fetch_video_details([video_id], api_key)
                if details:
                    _qrec("videos.list")
                    n = _add(details)
                    logger.debug(
                        "  Method 3 related channel %s: added %d clip(s).", sim_id, n
                    )

            except Exception as exc:
                logger.debug(
                    "Error fetching video from related channel %s: %s", sim_id, exc
                )

    logger.info(
        "Global YouTube pool complete: %d unique clip(s) from all discovery methods.",
        len(pool),
    )
    return pool


def fetch_trending_clips(
    api_key: str,
    target_games: List[str],
    limit: int = 50,
    region_code: str = "US",
) -> List[Dict[str, Any]]:
    """
    Fetch trending videos from YouTube using the mostPopular chart across all
    categories.

    Called when allow_youtube_trending is enabled in preferences. Results are
    merged into the main clip pool and compete on score alongside channel clips,
    giving the compiler more high-quality content to fill packages.

    The trending endpoint does not support keyword search, so game filtering is
    applied post-fetch by checking titles and descriptions.

    Args:
        api_key:      YouTube Data API v3 key.
        target_games: Lower-cased game names to filter by. Empty = no filter.
        limit:        Max number of qualifying clips to return.
        region_code:  ISO 3166-1 alpha-2 country code (default "US").

    Returns:
        List of clip dicts in the ClipCast standard format.
    """
    logger.info("Fetching YouTube trending videos (region=%s)…", region_code)

    try:
        resp = requests.get(
            f"{_YT_API_BASE}/videos",
            params={
                "part": "snippet,contentDetails,statistics",
                "chart": "mostPopular",
                "regionCode": region_code,
                "maxResults": 50,          # API hard maximum
                "key": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except requests.RequestException as e:
        logger.error("Failed to fetch YouTube trending gaming videos: %s", e)
        return []

    if not items:
        logger.info("YouTube trending: no items returned for region=%s.", region_code)
        return []

    clips: List[Dict[str, Any]] = []
    for item in items:
        duration_iso = item.get("contentDetails", {}).get("duration", "")
        duration_sec = _parse_iso8601_duration(duration_iso)

        # Skip videos outside our target duration window
        if not (_MIN_DURATION_SEC <= duration_sec <= _MAX_DURATION_SEC):
            continue

        # Apply game filter post-fetch (trending API does not support q param)
        if target_games:
            snippet = item.get("snippet", {})
            combined = (
                snippet.get("title", "") + " " + snippet.get("description", "")
            ).lower()
            if not any(game in combined for game in target_games):
                continue

        clips.append(_normalize_video(item, duration_sec))

        if len(clips) >= limit:
            break

    logger.info(
        "YouTube trending: %d clip(s) in duration range.", len(clips)
    )
    return clips


def fetch_youtube_via_ytdlp(max_clips: int = 50) -> List[Dict[str, Any]]:
    """
    Fallback YouTube discovery using yt-dlp search queries.

    Called by refresh_youtube_pool() when YouTube API quota is CRITICAL, or
    by refresh_viral_discovery_pool() as an additional gaming content source.
    Uses yt-dlp ytsearch against the public YouTube frontend — no API quota.

    Args:
        max_clips: Target number of clips to return (default 50).

    Returns:
        List of clip dicts in ClipCast standard format.
        discovery_source is set to 'youtube_gaming_trending'.
    """
    import subprocess
    import json as _json

    _YTDLP_QUERIES = [
        "best gaming moments viral",
        "funniest twitch clips this week",
        "gaming fails compilation shorts",
        "streamer freaks out viral clip",
        "insane gaming moments shorts",
        "twitch highlights funny moments",
        "gaming rage quit funny",
        "best gaming clips short",
        "viral gaming moments 2024",
        "funny gaming moments compilation",
        "twitch ban moment viral",
        "streamer goes viral gaming",
    ]

    clips: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for query in _YTDLP_QUERIES:
        if len(clips) >= max_clips:
            break
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            "--quiet",
            f"ytsearch10:{query}",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                continue
            data = _json.loads(result.stdout)
            entries = data.get("entries") or ([data] if "id" in data else [])
            for e in entries:
                if not e:
                    continue
                vid_id = e.get("id") or ""
                if not vid_id or vid_id in seen_ids:
                    continue
                duration = float(e.get("duration") or 0)
                if not (_MIN_DURATION_SEC <= duration <= _MAX_DURATION_SEC):
                    continue
                seen_ids.add(vid_id)
                clips.append({
                    "clip_id":          vid_id,
                    "source":           "youtube",
                    "title":            (e.get("title") or "")[:200],
                    "url":              (
                        e.get("url")
                        or e.get("webpage_url")
                        or f"https://www.youtube.com/watch?v={vid_id}"
                    ),
                    "creator_name":     e.get("uploader") or e.get("channel") or "",
                    "duration":         duration,
                    "view_count":       int(e.get("view_count") or 0),
                    "like_count":       0,
                    "created_at":       "",
                    "thumbnail_url":    e.get("thumbnail") or "",
                    "channel_id":       e.get("channel_id") or "",
                    "game":             "",
                    "discovery_source": "youtube_gaming_trending",
                })
        except (subprocess.TimeoutExpired, Exception) as exc:
            logger.debug("fetch_youtube_via_ytdlp query '%s' error: %s", query, exc)

    logger.info("fetch_youtube_via_ytdlp: %d clips via yt-dlp fallback", len(clips))
    return clips[:max_clips]


def _normalize_video(item: Dict, duration_sec: float) -> Dict[str, Any]:
    """Convert a raw YouTube API video item to the ClipCast standard format."""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    video_id = item.get("id", "")

    return {
        "clip_id":      video_id,
        "source":       "youtube",
        "title":        snippet.get("title", "Untitled"),
        "url":          f"https://www.youtube.com/watch?v={video_id}",
        "creator_name": snippet.get("channelTitle", "Unknown"),
        "duration":     duration_sec,
        "view_count":   int(stats.get("viewCount", 0)),
        "like_count":   int(stats.get("likeCount", 0)),
        "created_at":   snippet.get("publishedAt", ""),
        "thumbnail_url": (
            snippet.get("thumbnails", {}).get("high", {}).get("url", "") or
            snippet.get("thumbnails", {}).get("default", {}).get("url", "")
        ),
        "channel_id":   snippet.get("channelId", ""),
        # category_id "20" = Gaming; anything else routes to IRL/talking layout
        "game":         snippet.get("categoryId", ""),
    }


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing fetcher_youtube.py...")

    try:
        from preferences import load_config, load_preferences
        config = load_config()
        prefs = load_preferences()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if config["youtube"]["api_key"] == "YOUR_YOUTUBE_DATA_API_KEY_HERE":
        print("YouTube API key not set. Edit config.yaml first.")
        sys.exit(1)

    print(f"Target channels: {prefs.get('target_youtube_channels')}")
    print("Fetching YouTube clips (last 24 hours)...\n")

    clips = fetch_clips(user_config=config, user_prefs=prefs, limit_per_channel=5)

    if not clips:
        print("No clips returned. This could mean:\n"
              "  • No videos uploaded in the last 24 hours for your target channels\n"
              "  • Your game filter doesn't match any titles/descriptions\n"
              "  • API key is incorrect or quota exceeded")
    else:
        print(f"Fetched {len(clips)} clip(s):\n")
        for clip in clips[:10]:
            print(f"  [{clip['creator_name']}] {clip['title'][:60]}")
            print(f"    Duration: {clip['duration']}s | Views: {clip['view_count']:,}")
            print(f"    URL: {clip['url']}\n")

    print("YouTube fetcher test complete.")
