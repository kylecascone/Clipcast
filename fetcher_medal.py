"""
fetcher_medal.py
================
Fetches viral clips from Medal.tv — a clip-sharing platform popular with
Twitch/YouTube gaming streamers.

Attempts two strategies:
1. Medal trending endpoint  → top clips across all categories
2. Creator search           → top clips for each TIER1_CREATORS name

Both strategies use public API endpoints with no credentials required.
If an endpoint returns a non-200 or unexpected shape, the fetcher logs and
continues silently — no crash.

Source mapping
--------------
Medal clips come from streamers who stream on Twitch/YouTube/Kick.
The 'source' field is set to 'twitch' by default; pool_fetcher.py will
attempt to trace the creator to their primary platform via find_original_source().

Usage
-----
    from fetcher_medal import fetch_medal_clips
    clips = fetch_medal_clips(max_clips=50)
"""

import logging
from typing import List, Dict

import requests

logger = logging.getLogger(__name__)

MEDAL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
}

TIER1_CREATORS = [
    "xqc", "shroud", "nickmercs", "jynxzi", "kaicenat",
    "ishowspeed", "adinross", "caseoh", "moistcritikal", "pokimane",
]

_MEDAL_TIMEOUT = 15
_MIN_DURATION  = 20
_MAX_DURATION  = 180


def _normalise_medal_clip(clip: dict, creator_hint: str = "") -> dict:
    """Convert a raw Medal API clip dict to a ClipCast clip dict."""
    url      = clip.get("contentUrl") or clip.get("videoUrl") or clip.get("url", "")
    duration = int(clip.get("videoLengthSeconds") or clip.get("duration") or 0)
    title    = clip.get("contentTitle") or clip.get("title") or f"{creator_hint} clip"
    creator  = (
        clip.get("username")
        or (clip.get("user") or {}).get("username")
        or creator_hint
        or ""
    )
    views = int(
        clip.get("views") or clip.get("contentViews") or clip.get("viewCount") or 0
    )
    clip_id = (
        clip.get("contentId") or clip.get("id") or clip.get("clipId") or url
    )
    return {
        "clip_id":          f"viral_medal_{str(clip_id)[:64]}",
        "url":              url,
        "title":            str(title)[:200],
        "creator_name":     creator,
        "view_count":       views,
        "duration":         duration,
        "duration_sec":     duration,
        "source":           "twitch",          # placeholder; normalised by pool_fetcher
        "discovery_source": "medal_trending",
        "language":         "en",
        "has_music":        False,
        "game":             "",
        "category":         clip.get("category") or "",
        "mode":             "auto",
    }


def fetch_medal_clips(max_clips: int = 50) -> List[Dict]:
    """
    Fetch viral clips from Medal.tv.

    Tries the trending endpoint first, then searches for each TIER1_CREATORS
    name.  Returns a deduplicated list of ClipCast clip dicts.

    Args:
        max_clips: Maximum clips to return across all strategies.

    Returns:
        List of clip dicts with source='twitch' (placeholder until traced by
        pool_fetcher).  Empty list on complete failure.
    """
    clips: list = []
    seen_urls: set = set()

    def _add(raw: dict, creator_hint: str = "") -> None:
        norm = _normalise_medal_clip(raw, creator_hint)
        url = norm.get("url", "")
        dur = norm.get("duration", 0)
        if not url or url in seen_urls:
            return
        if dur < _MIN_DURATION or dur > _MAX_DURATION:
            return
        seen_urls.add(url)
        clips.append(norm)

    # ── 1. Trending endpoint ───────────────────────────────────────────────────
    try:
        resp = requests.get(
            "https://medal.tv/api/clips/trending",
            headers=MEDAL_HEADERS,
            timeout=_MEDAL_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            clip_list = (
                data if isinstance(data, list)
                else data.get("clips", data.get("data", []))
            )
            for raw in clip_list[:max_clips]:
                _add(raw)
            logger.debug(
                "fetch_medal_clips: trending returned %d clips, %d accepted so far",
                len(clip_list), len(clips),
            )
        else:
            logger.debug(
                "fetch_medal_clips: trending endpoint status %d", resp.status_code
            )
    except Exception as exc:
        logger.debug("fetch_medal_clips: trending error: %s", exc)

    # ── 2. Per-creator search ─────────────────────────────────────────────────
    for creator in TIER1_CREATORS:
        if len(clips) >= max_clips:
            break
        try:
            resp = requests.get(
                f"https://medal.tv/api/search?query={creator}&type=clips&limit=10",
                headers=MEDAL_HEADERS,
                timeout=_MEDAL_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                for raw in data.get("clips", []):
                    _add(raw, creator_hint=creator)
        except Exception as exc:
            logger.debug("fetch_medal_clips: search error for %s: %s", creator, exc)
            continue

    logger.info("fetch_medal_clips: %d Medal.tv clips found", len(clips))
    return clips[:max_clips]


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.DEBUG, format="%(levelname)s  %(message)s")
    clips = fetch_medal_clips(max_clips=20)
    print(f"\nMedal.tv clips found: {len(clips)}")
    for c in clips[:5]:
        print(
            f"  [{c['creator_name']}] {c['duration']}s "
            f"views={c['view_count']:,}  '{c['title'][:50]}'"
        )
