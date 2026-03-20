"""
viral_discovery.py
==================
Finds clips already going viral on Reddit and YouTube Shorts,
traces them back to their original Twitch / YouTube / Kick source, and returns
standardised clip dicts ready for shared_pool.add_clip_to_pool().

Platform status
---------------
Reddit (r/LivestreamFail etc.)  — WORKING  (JSON API, no credentials)
YouTube Shorts search           — WORKING  (yt-dlp search)
TikTok                          — UNAVAILABLE (yt-dlp app-info broken + IP blocked)
Instagram                       — UNAVAILABLE (yt-dlp does not support profile scraping)

Discovery sources
-----------------
    Reddit post links  → discovery_source='reddit_trending'
    YouTube Shorts     → discovery_source='youtube_shorts_trending'

Score boosts
------------
    Reddit:          +35 base, +15 if >1 000 upvotes, +25 if >5 000 upvotes
    YouTube Shorts:  +45 base, +10 if >500 K views, +15 if >2 M views

Usage
-----
    import viral_discovery
    clips = viral_discovery.discover_viral_clips()
    # clips is a list of standardised dicts ready for add_clip_to_pool()
"""

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── Timeouts ───────────────────────────────────────────────────────────────────
_YTDLP_TIMEOUT  = 60   # seconds per yt-dlp call
_HTTP_TIMEOUT   = 15   # seconds per requests call

# ── View / upvote minimums ─────────────────────────────────────────────────────
_MIN_VIEWS_YT_SHORTS  = 20_000
_MIN_UPVOTES_REDDIT   = 50    # lowered from 200 — quality filter handles relevance

# ── Subreddit → creator fallback map ──────────────────────────────────────────
# Used when post title doesn't mention the creator (common in creator-specific subs)
_SUBREDDIT_TO_CREATOR: Dict[str, tuple] = {
    "xqcow":          ("twitch",  "xqc"),
    "kaicenat":       ("twitch",  "kaicenat"),
    "ishowspeed":     ("youtube", "IShowSpeed"),
    "adinross":       ("kick",    "adinross"),
    "jynxzi":         ("twitch",  "jynxzi"),
    "nickmercs":      ("twitch",  "nickmercs"),
    "shroud":         ("twitch",  "shroud"),
    "mizkif":         ("twitch",  "mizkif"),
    "caseoh":         ("twitch",  "caseoh"),
    "nmplol":         ("twitch",  "nmplol"),
    "forsen":         ("twitch",  "forsen"),
    "sodapoppin":     ("twitch",  "sodapoppin"),
    "summit1g":       ("twitch",  "summit1g"),
    "pokimane":       ("twitch",  "pokimane"),
    "hasanabi":       ("twitch",  "hasanabi"),
    "emiru":          ("twitch",  "emiru"),
    "trainwreck":     ("twitch",  "trainwreck"),
}

# ── Reddit user-agent rotation pool ───────────────────────────────────────────
_REDDIT_UA_POOL: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "ClipCastBot/2.0 (+personal automation; not-for-profit)",
    "python-requests/2.31.0",
]

# ── Reddit subreddits ──────────────────────────────────────────────────────────
REDDIT_SUBREDDITS: List[str] = [
    # Streaming — highest priority
    "LivestreamFail",
    "xqcow",
    "KaiCenat",
    "IShowSpeed",
    "AdinRoss",
    "twitchclips",
    "LivestreamFails",
    # General viral video
    "PublicFreakout",
    "unexpected",
    "nextfuckinglevel",
    "maybemaybemaybe",
    "therewasanattempt",
    "instant_regret",
    "nonononoyes",
    "WTF",
    "interestingasfuck",
    "Damnthatsinteresting",
    # Sports
    "sports",
    "nba",
    "soccer",
    "MMA",
    "Boxing",
]
_REDDIT_HEADERS = {"User-Agent": "ClipCastBot/1.0 (personal automation tool)"}

# ── YouTube Shorts search queries (42 total) ───────────────────────────────────
YT_SHORTS_QUERIES: List[str] = [
    # Original 12
    "xqc funny moments twitch clip",
    "kai cenat viral clip reaction",
    "ishowspeed funny moment shorts",
    "twitch streamer insane clip",
    "adinross irl clip viral",
    "jynxzi clip funny moment",
    "twitch highlights funny",
    "streamer reaction insane moment",
    "gaming rage quit funny clip",
    "twitch clip goes viral",
    "kick streamer clip viral",
    "irl streamer crazy moment",
    # 30 more (per user request)
    "shroud clips shorts",
    "nickmercs shorts",
    "timthetatman funny shorts",
    "pokimane reaction shorts",
    "hasanabi shorts",
    "moistcritikal shorts",
    "ludwig shorts",
    "valkyrae shorts",
    "dream smp shorts",
    "tommyinnit shorts",
    "caseoh shorts",
    "forsen shorts",
    "summit1g shorts",
    "sodapoppin shorts",
    "mizkif shorts",
    "emiru shorts",
    "adinross irl shorts",
    "kai cenat irl shorts",
    "ishowspeed irl shorts",
    "neon kick shorts",
    "jidion shorts",
    "sneako shorts",
    "clavicular kick shorts",
    "trainwreck shorts",
    "viral twitch moment shorts",
    "twitch ban clip shorts",
    "streamer freaks out shorts",
    "twitch fails shorts",
    "streamer goes viral shorts",
    "funny twitch clip shorts",
]

# ── YouTube Shorts channel scrapers ───────────────────────────────────────────
YT_SHORTS_CHANNELS: List[str] = [
    "UCkZFsVkDzVUMkWGnmF4mQCQ",   # Best of Twitch
    "UCwmFOfFuvRPI112vR5DNnrA",   # Twitch Moments
    # Handles (resolved via yt-dlp URL)
]
YT_SHORTS_CHANNEL_URLS: List[str] = [
    "https://www.youtube.com/@shroudclips/shorts",
    "https://www.youtube.com/@nickmercsclips/shorts",
    "https://www.youtube.com/@kaicenathighlights/shorts",
    "https://www.youtube.com/@adinrossclips/shorts",
    "https://www.youtube.com/@ishowspeedmoments/shorts",
    "https://www.youtube.com/@twitchfails/shorts",
    "https://www.youtube.com/@streamerhighlights2/shorts",
    "https://www.youtube.com/@viralstreamclips/shorts",
]

# ── IRL vs Gaming theme classification ─────────────────────────────────────────
_IRL_THEMES    = {"PRANK", "CONFRONTATION", "TRAVEL", "SOCIAL", "DATE", "VIRAL_MOMENT"}
_GAMING_THEMES = {"FUNNY", "RAGE", "SHOCKED", "CLUTCH", "WHOLESOME", "DRAMA",
                  "FAIL", "REACTION"}

# ── Theme keyword maps ─────────────────────────────────────────────────────────
_THEME_KEYWORDS: Dict[str, List[str]] = {
    "RAGE":          ["rage", "angry", "mad", "furious", "slams", "breakdown",
                      "tilted", "rage quit", "loses it", "goes off"],
    "CLUTCH":        ["clutch", "insane play", "no way", "1v5", "1v4", "1v3",
                      "impossible", "crazy play", "godlike", "highlight"],
    "FAIL":          ["fail", "falls", "dies", "trolled", "scammed", "baited",
                      "embarrassing", "miss", "fumble", "throws"],
    "SHOCKED":       ["shocked", "can't believe", "no way", "wtf", "omg",
                      "react", "reaction", "pog", "poggers"],
    "FUNNY":         ["funny", "lol", "lmao", "hilarious", "comedy", "jokes",
                      "roasted", "clowned", "trolled", "clown"],
    "WHOLESOME":     ["wholesome", "sweet", "cute", "heartwarming", "kind",
                      "charitable", "gives", "donates", "crying happy"],
    "DRAMA":         ["drama", "beef", "beef with", "clout", "fight",
                      "exposed", "cancel", "cancelled", "banned"],
    "REACTION":      ["reacts", "reaction", "watching", "responds", "first time",
                      "never seen"],
    "PRANK":         ["prank", "pranks", "pranked", "pulls prank", "gotcha",
                      "trick", "tricks"],
    "CONFRONTATION": ["confrontation", "argument", "fight", "argues", "confronts",
                      "conflict", "altercation", "kicked out"],
    "TRAVEL":        ["travel", "abroad", "country", "visiting", "city tour",
                      "exploring", "vacation", "trip", "flies to"],
    "SOCIAL":        ["social experiment", "people react", "public", "strangers",
                      "crowd", "fans", "approaches", "meets fans"],
    "DATE":          ["date", "dating", "romantic", "relationship", "girlfriend",
                      "boyfriend", "love"],
    "VIRAL_MOMENT":  ["viral", "blew up", "trending", "million views",
                      "everyone talking", "went viral", "internet"],
}


# ══════════════════════════════════════════════════════════════════════════════
# Theme detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_theme(title: str, category: str = "", transcript: str = "") -> str:
    """
    Classify a clip into one of 14 content themes.

    Returns:
        One of: FUNNY, RAGE, SHOCKED, CLUTCH, WHOLESOME, DRAMA, FAIL,
                REACTION, PRANK, CONFRONTATION, TRAVEL, SOCIAL, DATE, VIRAL_MOMENT
        Default: "FUNNY"
    """
    combined = " ".join([title or "", category or "", transcript or ""]).lower()
    scores: Dict[str, int] = {t: 0 for t in _THEME_KEYWORDS}
    for theme, keywords in _THEME_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                scores[theme] += 2 if kw in (title or "").lower() else 1
    best = max(scores, key=lambda t: scores[t])
    return best if scores[best] > 0 else "FUNNY"


# ══════════════════════════════════════════════════════════════════════════════
# Source tracing
# ══════════════════════════════════════════════════════════════════════════════

def find_original_source(
    title: str,
    description: str = "",
    uploader: str = "",
) -> Optional[Dict[str, str]]:
    """
    Parse a post title / description to identify the original Tier-1 streamer.

    Checks against TIER1_TWITCH, TIER1_KICK, TIER1_YOUTUBE (Twitch has priority
    when a creator appears on multiple platforms).

    Returns:
        {source: 'twitch'|'kick'|'youtube', creator_name: str}
        or None if no known creator can be identified.
    """
    try:
        from viral_creators import TIER1_TWITCH, TIER1_KICK, TIER1_YOUTUBE
    except ImportError:
        return None

    # Build lookup with Twitch last so it wins on collision
    lookup: Dict[str, tuple] = {}
    for name in TIER1_KICK:
        lookup[_norm_name(name)] = ("kick", name)
    for display in TIER1_YOUTUBE.keys():
        lookup[_norm_name(display)] = ("youtube", display)
    for name in TIER1_TWITCH:
        lookup[_norm_name(name)] = ("twitch", name)

    haystack = " ".join([title or "", description or "", uploader or ""]).lower()

    for norm, (source, original) in lookup.items():
        if norm and norm in _norm_name(haystack):
            return {"source": source, "creator_name": original}

    # @mention extraction
    for mention in re.findall(r"@([a-zA-Z0-9_]+)", haystack):
        key = _norm_name(mention)
        if key in lookup:
            source, original = lookup[key]
            return {"source": source, "creator_name": original}

    return None


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ══════════════════════════════════════════════════════════════════════════════
# Score boost logic
# ══════════════════════════════════════════════════════════════════════════════

def _compute_viral_boost(discovery_source: str, metric: int) -> float:
    """
    Compute score boost.

    For Reddit:         metric = upvotes
    For YouTube Shorts: metric = view_count
    """
    if discovery_source == "reddit_trending":
        boost = 35.0
        if metric >= 5_000:
            boost += 25.0
        elif metric >= 1_000:
            boost += 15.0
    elif discovery_source == "youtube_shorts_trending":
        boost = 45.0
        if metric >= 2_000_000:
            boost += 15.0
        elif metric >= 500_000:
            boost += 10.0
    elif discovery_source == "youtube_gaming_trending":
        boost = 30.0
        if metric >= 1_000_000:
            boost += 15.0
        elif metric >= 100_000:
            boost += 8.0
    else:
        boost = 0.0
    return boost


# ══════════════════════════════════════════════════════════════════════════════
# Reddit scraper
# ══════════════════════════════════════════════════════════════════════════════

def scrape_reddit(subreddit: str, limit: int = 50) -> List[Dict]:
    """
    Fetch top posts from a subreddit using a 4-method cascade.

    Method A: Rotating browser-like user agents on reddit.com/r/{sub}.json
    Method B: old.reddit.com subdomain fallback with different user agent
    Method C: OAuth app-only auth (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET env vars)
    Method D: api.reddit.com with Accept header (last resort)

    Prints which method worked on success.
    Returns raw post data dicts (v.redd.it videos only).
    """
    import os
    url_base = f"https://www.reddit.com/r/{subreddit}/top.json?t=week&limit={limit}"
    old_url  = f"https://old.reddit.com/r/{subreddit}/top.json?t=week&limit={limit}"
    api_url  = f"https://api.reddit.com/r/{subreddit}/top.json?t=week&limit={limit}"

    def _extract_video_posts(r) -> List[Dict]:
        posts = r.json().get("data", {}).get("children", [])
        return [
            p["data"] for p in posts
            if p.get("data", {}).get("is_video", False)
            and "v.redd.it" in p.get("data", {}).get("url", "")
        ]

    # Method A: Rotating user agents on reddit.com
    for ua in _REDDIT_UA_POOL:
        try:
            r = requests.get(
                url_base, headers={"User-Agent": ua},
                timeout=_HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                posts = _extract_video_posts(r)
                logger.debug(
                    "scrape_reddit r/%s Method A (UA=%s…): %d video posts",
                    subreddit, ua[:30], len(posts),
                )
                if posts or r.json().get("data", {}).get("children"):
                    return posts
        except Exception as exc:
            logger.debug("scrape_reddit r/%s Method A UA error: %s", subreddit, exc)

    # Method B: old.reddit.com
    try:
        r = requests.get(
            old_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            posts = _extract_video_posts(r)
            if posts or r.json().get("data", {}).get("children"):
                logger.debug(
                    "scrape_reddit r/%s Method B (old.reddit.com): %d video posts",
                    subreddit, len(posts),
                )
                return posts
    except Exception as exc:
        logger.debug("scrape_reddit r/%s Method B error: %s", subreddit, exc)

    # Method C: OAuth app-only (optional — needs env vars)
    client_id     = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if client_id and client_secret:
        try:
            token_resp = requests.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": "ClipCastBot/2.0"},
                timeout=10,
            )
            if token_resp.status_code == 200:
                token = token_resp.json().get("access_token", "")
                if token:
                    r = requests.get(
                        f"https://oauth.reddit.com/r/{subreddit}/top.json?t=week&limit={limit}",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "User-Agent":    "ClipCastBot/2.0",
                        },
                        timeout=_HTTP_TIMEOUT,
                    )
                    if r.status_code == 200:
                        posts = _extract_video_posts(r)
                        logger.debug(
                            "scrape_reddit r/%s Method C (OAuth): %d video posts",
                            subreddit, len(posts),
                        )
                        return posts
        except Exception as exc:
            logger.debug("scrape_reddit r/%s Method C error: %s", subreddit, exc)

    # Method D: api.reddit.com with Accept header
    try:
        r = requests.get(
            api_url,
            headers={
                "User-Agent": "ClipCastBot/2.0",
                "Accept":     "application/json",
            },
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            posts = _extract_video_posts(r)
            logger.debug(
                "scrape_reddit r/%s Method D (api.reddit.com): %d video posts",
                subreddit, len(posts),
            )
            return posts
    except Exception as exc:
        logger.debug("scrape_reddit r/%s Method D error: %s", subreddit, exc)

    logger.warning("scrape_reddit r/%s: all 4 methods failed — returning []", subreddit)
    return []


def _normalise_reddit_post(post: Dict, min_upvotes: int) -> Optional[Dict]:
    """Convert a raw Reddit post dict to a ClipCast clip dict."""
    ups = post.get("ups", 0)
    if ups < min_upvotes:
        return None

    title = post.get("title") or ""
    url   = post.get("url") or post.get("url_overridden_by_dest") or ""
    post_id = post.get("id") or url

    if not url or "v.redd.it" not in url:
        return None

    # Duration is available directly from Reddit's API — no yt-dlp call needed
    media = post.get("media") or post.get("secure_media") or {}
    reddit_video = media.get("reddit_video") or {}
    duration = float(reddit_video.get("duration") or 0)

    if duration <= 0 or duration > 180:
        return None

    has_audio = bool(reddit_video.get("has_audio", True))

    # Trace to original Tier-1 creator
    origin = find_original_source(title, post.get("selftext", ""), post.get("author", ""))

    # Fallback 1: infer creator from subreddit name (e.g. r/KaiCenat posts rarely
    # mention "Kai Cenat" in the title)
    if origin is None:
        subreddit_lower = (post.get("subreddit") or "").lower()
        if subreddit_lower in _SUBREDDIT_TO_CREATOR:
            src, name = _SUBREDDIT_TO_CREATOR[subreddit_lower]
            origin = {"source": src, "creator_name": name}

    # Fallback 2: for general viral subreddits (r/PublicFreakout, r/WTF, etc.)
    # accept the clip without tier1 attribution — quality filter decides
    if origin is None:
        origin = {"source": "twitch", "creator_name": post.get("author") or "viral_clip"}

    boost = _compute_viral_boost("reddit_trending", ups)
    # Use upvotes × 50 as a rough view-count proxy for pool filtering
    view_count = ups * 50

    return {
        "clip_id":          f"viral_reddit_{post_id}",
        "source":           origin["source"],
        "creator_name":     origin["creator_name"],
        "title":            title[:200],
        "url":              url,
        "duration":         duration,
        "duration_sec":     duration,
        "view_count":       view_count,
        "upvotes":          ups,
        "score":            min(100.0, boost),
        "has_music":        not has_audio,
        "language":         "en",
        "game":             "",
        "category":         "",
        "mode":             "auto",
        "discovery_source": "reddit_trending",
        "viral_title":      title[:200],
        "theme":            detect_theme(title),
    }


# ══════════════════════════════════════════════════════════════════════════════
# YouTube Shorts scraper
# ══════════════════════════════════════════════════════════════════════════════

def _run_ytdlp_search(query: str, max_items: int = 20) -> List[Dict]:
    """Run yt-dlp ytsearch and return flat entry list."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--quiet",
        f"ytsearch{max_items}:{query}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_YTDLP_TIMEOUT)
        if result.returncode != 0:
            logger.debug("yt-dlp search non-zero for '%s': %s", query, result.stderr[:150])
            return []
        data = json.loads(result.stdout)
        if "entries" in data:
            return [e for e in (data.get("entries") or []) if e]
        return [data]
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp search timed out for '%s'", query)
        return []
    except (json.JSONDecodeError, Exception) as exc:
        logger.debug("yt-dlp search error for '%s': %s", query, exc)
        return []


def _run_ytdlp_flat(url: str, max_items: int = 15) -> List[Dict]:
    """Run yt-dlp in flat-playlist mode on a URL."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--quiet",
        "--playlist-end", str(max_items),
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_YTDLP_TIMEOUT)
        if result.returncode != 0:
            logger.debug("yt-dlp non-zero for %s: %s", url, result.stderr[:150])
            return []
        data = json.loads(result.stdout)
        if "entries" in data:
            return [e for e in (data.get("entries") or []) if e]
        return [data]
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out for %s", url)
        return []
    except (json.JSONDecodeError, Exception) as exc:
        logger.debug("yt-dlp error for %s: %s", url, exc)
        return []


def scrape_youtube_shorts(max_results: int = 60) -> List[Dict]:
    """
    Scrape YouTube Shorts via yt-dlp search queries and channel scrapers.

    Filters: duration ≤ 180 s, view_count ≥ _MIN_VIEWS_YT_SHORTS.
    No date filter — viral clips may be weeks old but still spreading.
    """
    results: List[Dict] = []
    seen_ids: set = set()

    def _accept(e: Dict) -> bool:
        if not e:
            return False
        vid_id = e.get("id") or e.get("url", "")
        if vid_id in seen_ids:
            return False
        duration = e.get("duration") or 0
        if duration <= 0 or duration > 180:
            return False
        view_count = e.get("view_count") or 0
        if view_count < _MIN_VIEWS_YT_SHORTS:
            return False
        return True

    # Search queries
    for query in YT_SHORTS_QUERIES:
        if len(results) >= max_results:
            break
        for e in _run_ytdlp_search(query, max_items=15):
            if _accept(e):
                vid_id = e.get("id") or e.get("url", "")
                seen_ids.add(vid_id)
                results.append(e)
        time.sleep(0.3)

    # Channel scrapers
    for ch_id in YT_SHORTS_CHANNELS:
        if len(results) >= max_results:
            break
        url = f"https://www.youtube.com/channel/{ch_id}/shorts"
        for e in _run_ytdlp_flat(url, max_items=20):
            if _accept(e):
                vid_id = e.get("id") or e.get("url", "")
                seen_ids.add(vid_id)
                results.append(e)

    for ch_url in YT_SHORTS_CHANNEL_URLS:
        if len(results) >= max_results:
            break
        for e in _run_ytdlp_flat(ch_url, max_items=20):
            if _accept(e):
                vid_id = e.get("id") or e.get("url", "")
                seen_ids.add(vid_id)
                results.append(e)
        time.sleep(0.2)

    logger.debug("scrape_youtube_shorts: %d clips after filters", len(results))
    return results


def _normalise_yt_shorts_entry(entry: Dict) -> Optional[Dict]:
    """Convert a raw yt-dlp Shorts entry to a ClipCast clip dict."""
    if not entry:
        return None

    raw_url    = entry.get("url") or entry.get("webpage_url") or ""
    title      = entry.get("title") or ""
    uploader   = entry.get("uploader") or entry.get("channel") or ""
    view_count = int(entry.get("view_count") or 0)
    duration   = float(entry.get("duration") or 0)
    vid_id     = entry.get("id") or raw_url

    if not raw_url or not title or duration <= 0:
        return None

    origin = find_original_source(title, entry.get("description") or "", uploader)
    if origin is None:
        return None

    boost = _compute_viral_boost("youtube_shorts_trending", view_count)

    return {
        "clip_id":          f"viral_yt_shorts_{vid_id[:64]}",
        "source":           origin["source"],
        "creator_name":     origin["creator_name"],
        "title":            title[:200],
        "url":              raw_url,
        "duration":         duration,
        "duration_sec":     duration,
        "view_count":       view_count,
        "score":            min(100.0, boost),
        "has_music":        False,
        "language":         "en",
        "game":             "",
        "category":         "",
        "mode":             "auto",
        "discovery_source": "youtube_shorts_trending",
        "viral_title":      title[:200],
        "theme":            detect_theme(title),
    }


# ══════════════════════════════════════════════════════════════════════════════
# YouTube gaming trending (replaces Medal.tv)
# ══════════════════════════════════════════════════════════════════════════════

_YT_GAMING_TRENDING_QUERIES: List[str] = [
    "best gaming moments viral",
    "twitch moments insane reaction",
    "funny gaming moment shorts",
    "gaming fail viral video",
    "streamer reaction insane clip",
    "gaming highlight viral 2024",
    "best gaming shorts compilation",
]


def fetch_youtube_gaming_trending(max_clips: int = 50) -> List[Dict]:
    """
    Discover gaming clips trending on YouTube using yt-dlp search queries.

    This replaces Medal.tv in the discovery pipeline (Medal uses a private API
    that is no longer accessible).  No attribution requirement — any creator
    with a gaming clip in-range is included and scored by the quality filter.

    Duration filter: 20–180 s.
    Returns clips with discovery_source='youtube_gaming_trending'.
    """
    clips: List[Dict] = []
    seen_ids: set = set()

    for query in _YT_GAMING_TRENDING_QUERIES:
        if len(clips) >= max_clips:
            break
        for e in _run_ytdlp_search(query, max_items=10):
            if not e:
                continue
            vid_id   = e.get("id") or e.get("url", "")
            duration = float(e.get("duration") or 0)
            if not vid_id or vid_id in seen_ids:
                continue
            if duration <= 0 or duration > 180:
                continue
            if duration < 20:
                continue

            seen_ids.add(vid_id)
            title    = (e.get("title") or "")[:200]
            uploader = e.get("uploader") or e.get("channel") or ""

            # Try to identify original creator; if not found, keep clip anyway
            origin = find_original_source(title, e.get("description") or "", uploader)
            source       = origin["source"]       if origin else "youtube"
            creator_name = origin["creator_name"] if origin else uploader

            boost = _compute_viral_boost("youtube_gaming_trending", int(e.get("view_count") or 0))

            clips.append({
                "clip_id":          f"viral_yt_gaming_{vid_id[:64]}",
                "source":           source,
                "creator_name":     creator_name,
                "title":            title,
                "url":              (
                    e.get("url") or e.get("webpage_url")
                    or f"https://www.youtube.com/watch?v={vid_id}"
                ),
                "duration":         duration,
                "duration_sec":     duration,
                "view_count":       int(e.get("view_count") or 0),
                "score":            min(100.0, 40.0 + boost),
                "has_music":        False,
                "language":         "en",
                "game":             "",
                "category":         "",
                "mode":             "auto",
                "discovery_source": "youtube_gaming_trending",
                "viral_title":      title,
                "theme":            detect_theme(title),
            })
        time.sleep(0.2)

    # Register gaming_trending as a valid boost source
    logger.info("fetch_youtube_gaming_trending: %d clips found", len(clips))
    return clips


# ══════════════════════════════════════════════════════════════════════════════
# Streamable scraper
# ══════════════════════════════════════════════════════════════════════════════

STREAMABLE_QUERIES: List[str] = [
    "xqc twitch", "kai cenat", "ishowspeed", "jynxzi",
    "adin ross", "shroud", "nickmercs", "twitch clip funny",
    "twitch moment insane", "streamer reacts", "twitch fail",
    "viral stream moment", "twitch banned", "streamer freaks out",
]


def fetch_streamable_clips(max_per_query: int = 5) -> List[Dict]:
    """
    Search Google for Streamable clips matching streamer keywords, then
    extract metadata via yt-dlp on each URL found.

    Slow but effective — each slug gets a yt-dlp call.
    Duration filter: 20–180 s.

    Returns:
        List of ClipCast clip dicts with discovery_source='streamable_trending'.
    """
    import yt_dlp as _ytdlp

    clips: List[Dict] = []
    seen_slugs: set = set()
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    for query in STREAMABLE_QUERIES:
        try:
            search_url = (
                f"https://www.google.com/search"
                f"?q=site:streamable.com+{query.replace(' ', '+')}&tbs=qdr:w"
            )
            resp = requests.get(search_url, headers=headers, timeout=10)
            slugs = list(
                set(re.findall(r"streamable\.com/([a-zA-Z0-9]{4,8})", resp.text))
            )[:max_per_query]

            for slug in slugs:
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                url = f"https://streamable.com/{slug}"
                try:
                    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
                    with _ytdlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    if not info:
                        continue
                    duration = int(info.get("duration") or 0)
                    if duration < 20 or duration > 180:
                        continue
                    clips.append({
                        "clip_id":          f"viral_streamable_{slug}",
                        "url":              url,
                        "title":            (info.get("title") or query)[:200],
                        "creator_name":     info.get("uploader") or "",
                        "view_count":       int(info.get("view_count") or 0),
                        "duration":         duration,
                        "duration_sec":     duration,
                        "upvotes":          0,
                        "source":           "twitch",
                        "discovery_source": "streamable_trending",
                        "language":         "en",
                        "has_music":        False,
                        "game":             "",
                        "category":         "",
                        "mode":             "auto",
                    })
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("fetch_streamable_clips error for '%s': %s", query, exc)
            continue

    logger.info("fetch_streamable_clips: %d Streamable clips found", len(clips))
    return clips


# ══════════════════════════════════════════════════════════════════════════════
# Twitter / X scraper (via Nitter instances)
# ══════════════════════════════════════════════════════════════════════════════

TWITTER_QUERIES: List[str] = [
    "twitch clip viral", "xqc clip", "kaicenat clip",
    "ishowspeed clip", "adinross clip", "twitch moment",
    "streamer clip funny", "jynxzi clip", "shroud clip",
    "gaming moment viral", "streamer freaks out clip",
]

NITTER_INSTANCES: List[str] = [
    "nitter.privacydev.net",
    "nitter.poast.org",
    "nitter.1d4.us",
]


def fetch_twitter_clips() -> List[Dict]:
    """
    Search Nitter (open-source Twitter frontend) for video tweets referencing
    streamers, then extract metadata via yt-dlp on each tweet URL found.

    Tries each Nitter instance until one responds.
    Duration filter: 20–180 s.

    Returns:
        List of ClipCast clip dicts with discovery_source='twitter_trending'.
    """
    import yt_dlp as _ytdlp

    clips: List[Dict] = []
    seen_ids: set = set()
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    for query in TWITTER_QUERIES:
        for nitter in NITTER_INSTANCES:
            try:
                search_url = (
                    f"https://{nitter}/search"
                    f"?q={query.replace(' ', '+')}&f=videos"
                )
                resp = requests.get(search_url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    continue

                tweet_ids = list(
                    set(re.findall(r"/status/(\d+)", resp.text))
                )[:5]

                for tweet_id in tweet_ids:
                    if tweet_id in seen_ids:
                        continue
                    seen_ids.add(tweet_id)
                    url = f"https://twitter.com/i/status/{tweet_id}"
                    try:
                        ydl_opts = {
                            "quiet": True,
                            "no_warnings": True,
                            "skip_download": True,
                        }
                        with _ytdlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                        if not info:
                            continue
                        duration = int(info.get("duration") or 0)
                        if duration < 20 or duration > 180:
                            continue
                        title = (
                            info.get("title")
                            or info.get("description", "")[:100]
                            or query
                        )
                        clips.append({
                            "clip_id":          f"viral_twitter_{tweet_id}",
                            "url":              url,
                            "title":            str(title)[:200],
                            "creator_name":     info.get("uploader") or "",
                            "view_count":       int(info.get("view_count") or 0),
                            "duration":         duration,
                            "duration_sec":     duration,
                            "upvotes":          0,
                            "source":           "twitch",
                            "discovery_source": "twitter_trending",
                            "language":         "en",
                            "has_music":        False,
                            "game":             "",
                            "category":         "",
                            "mode":             "auto",
                        })
                    except Exception:
                        continue
                break  # Move to next query once a working Nitter instance found
            except Exception as exc:
                logger.debug(
                    "fetch_twitter_clips: Nitter %s error for '%s': %s",
                    nitter, query, exc,
                )
                continue

    logger.info("fetch_twitter_clips: %d Twitter clips found", len(clips))
    return clips


# ══════════════════════════════════════════════════════════════════════════════
# TikTok / Instagram — unavailable stubs
# ══════════════════════════════════════════════════════════════════════════════

def scrape_tiktok_hashtag(hashtag: str, max_results: int = 10) -> List[Dict]:
    """
    TikTok scraping is currently unavailable.

    yt-dlp's TikTok extractor requires app signing credentials that TikTok
    has revoked.  Additionally, direct video URLs return "IP address blocked."

    Returns [] immediately.
    """
    logger.debug(
        "scrape_tiktok_hashtag: TikTok yt-dlp extractor unavailable "
        "(app info broken + IP blocked). Returning []."
    )
    return []


def scrape_instagram_account(account: str, max_results: int = 8) -> List[Dict]:
    """
    Instagram scraping is currently unavailable.

    yt-dlp does not support Instagram profile / Reels page scraping — only
    individual post URLs. Returning [] immediately.
    """
    logger.debug(
        "scrape_instagram_account: yt-dlp does not support Instagram profile "
        "pages. Returning []."
    )
    return []


# ══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ══════════════════════════════════════════════════════════════════════════════

def discover_viral_clips(
    include_youtube_shorts: bool = True,
    include_reddit: bool = True,
    include_tiktok: bool = False,    # Currently unavailable
    include_instagram: bool = False, # Currently unavailable
    max_total: int = 100,
) -> List[Dict]:
    """
    Orchestrate viral discovery across all active platforms.

    Scrapes Reddit (r/LivestreamFail etc.) and YouTube Shorts; traces each
    post back to a known Tier-1 creator; normalises into ClipCast clip dicts
    ready for add_clip_to_pool().

    Args:
        include_youtube_shorts:  Scrape YouTube Shorts (default True).
        include_reddit:          Scrape Reddit subreddits (default True).
        include_tiktok:          Currently unavailable — always returns 0.
        include_instagram:       Currently unavailable — always returns 0.
        max_total:               Cap on total clips returned (default 100).

    Returns:
        List of normalised clip dicts, deduplicated by clip_id.
    """
    clips: List[Dict] = []
    seen_ids: set = set()

    def _add(clip: Optional[Dict]) -> None:
        if not clip:
            return
        cid = clip.get("clip_id", "")
        if cid in seen_ids:
            return
        seen_ids.add(cid)
        clips.append(clip)

    # ── Reddit ────────────────────────────────────────────────────────────────
    if include_reddit:
        logger.info(
            "viral_discovery: scraping %d Reddit subreddits…",
            len(REDDIT_SUBREDDITS),
        )
        reddit_raw = 0
        reddit_added = 0
        for sub in REDDIT_SUBREDDITS:
            if len(clips) >= max_total:
                break
            try:
                posts = scrape_reddit(sub, limit=50)
                reddit_raw += len(posts)
                for post in posts:
                    clip = _normalise_reddit_post(post, _MIN_UPVOTES_REDDIT)
                    if clip:
                        _add(clip)
                        reddit_added += 1
            except Exception as exc:
                logger.debug("Reddit r/%s error: %s", sub, exc)
            time.sleep(0.3)
        logger.info(
            "viral_discovery: Reddit — %d raw posts → %d attributed clips",
            reddit_raw, reddit_added,
        )

    # ── YouTube Shorts ────────────────────────────────────────────────────────
    if include_youtube_shorts:
        logger.info("viral_discovery: scraping YouTube Shorts (%d queries)…", len(YT_SHORTS_QUERIES))
        yt_raw = 0
        yt_added = 0
        try:
            entries = scrape_youtube_shorts(max_results=max_total - len(clips))
            yt_raw = len(entries)
            for e in entries:
                if len(clips) >= max_total:
                    break
                clip = _normalise_yt_shorts_entry(e)
                if clip:
                    _add(clip)
                    yt_added += 1
        except Exception as exc:
            logger.debug("YouTube Shorts scrape error: %s", exc)
        logger.info(
            "viral_discovery: YouTube Shorts — %d raw entries → %d attributed clips",
            yt_raw, yt_added,
        )

    # ── Platform warnings ─────────────────────────────────────────────────────
    if include_tiktok:
        logger.warning(
            "viral_discovery: TikTok scraping skipped — yt-dlp app info "
            "broken and IP blocked. 0 clips from TikTok."
        )
    if include_instagram:
        logger.warning(
            "viral_discovery: Instagram scraping skipped — yt-dlp does not "
            "support profile page scraping. 0 clips from Instagram."
        )

    reddit_count  = sum(1 for c in clips if c.get("discovery_source") == "reddit_trending")
    yt_count      = sum(1 for c in clips if c.get("discovery_source") == "youtube_shorts_trending")

    logger.info(
        "viral_discovery: complete — %d total attributed clips "
        "(reddit=%d, yt_shorts=%d).",
        len(clips), reddit_count, yt_count,
    )

    if len(clips) < 50:
        logger.warning(
            "viral_discovery: only %d clips found (target: 50+). "
            "Reddit=%d YT_Shorts=%d. TikTok and Instagram are currently unavailable.",
            len(clips), reddit_count, yt_count,
        )

    return clips


# ══════════════════════════════════════════════════════════════════════════════
# Self-test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    print("=" * 65)
    print("viral_discovery.py  —  standalone test")
    print("=" * 65)

    # ── 1. detect_theme ────────────────────────────────────────────────────────
    print("\n[1] detect_theme() smoke tests:")
    cases = [
        ("xQc goes INSANE rage quit", "",        "RAGE"),
        ("INSANE clutch 1v5 no way",  "",        "CLUTCH"),
        ("Funniest stream moments",   "",        "FUNNY"),
        ("IRL prank in public",       "",        "PRANK"),
        ("Travel to Japan exploring", "Travel",  "TRAVEL"),
    ]
    for title, cat, expected in cases:
        got = detect_theme(title, cat)
        ok  = got == expected
        print(f"  {'OK' if ok else 'FAIL'} | '{title[:40]}' → {got} (expected {expected})")

    # ── 2. find_original_source ───────────────────────────────────────────────
    print("\n[2] find_original_source() smoke tests:")
    for title, desc, exp_src in [
        ("xQc REACTS to drama",    "",      "twitch"),
        ("Kai Cenat funny moment", "",      "twitch"),
        ("@shroud insane play",    "",      "twitch"),
        ("random unknown person",  "",      None),
    ]:
        got = find_original_source(title, desc)
        ok  = (got is None) == (exp_src is None)
        if got and exp_src:
            ok = ok and got["source"] == exp_src
        print(f"  {'OK' if ok else 'FAIL'} | '{title[:40]}' → {got}")

    # ── 3. Score boost ────────────────────────────────────────────────────────
    print("\n[3] Score boost sanity:")
    for dsrc, metric, expected_min in [
        ("reddit_trending",          5_001, 60.0),
        ("reddit_trending",          1_001, 50.0),
        ("reddit_trending",            300, 35.0),
        ("youtube_shorts_trending",  2_000_001, 60.0),
        ("youtube_shorts_trending",    100_000, 45.0),
    ]:
        boost = _compute_viral_boost(dsrc, metric)
        ok    = boost >= expected_min
        print(f"  {'OK' if ok else 'FAIL'} | {dsrc} metric={metric:,} → boost={boost}")

    # ── 4. Reddit live scrape ─────────────────────────────────────────────────
    print("\n[4] Reddit live scrape (r/LivestreamFail top week, limit=10):")
    posts = scrape_reddit("LivestreamFail", limit=10)
    print(f"  Raw video posts: {len(posts)}")
    attributed = 0
    for p in posts:
        clip = _normalise_reddit_post(p, _MIN_UPVOTES_REDDIT)
        if clip:
            attributed += 1
            print(
                f"  + [{clip['creator_name']}] ups={p['ups']:,}  "
                f"dur={clip['duration']:.0f}s  "
                f"'{clip['title'][:50]}'  theme={clip['theme']}"
            )
    if attributed == 0:
        print("  (0 attributed — titles may not match Tier-1 creators)")

    # ── 5. YouTube Shorts live (2 queries only) ───────────────────────────────
    print("\n[5] YouTube Shorts live (2 queries, max 5 results):")
    yt_total = 0
    for q in ["xqc funny clip short", "kai cenat irl shorts"]:
        entries = _run_ytdlp_search(q, max_items=10)
        short_entries = [
            e for e in entries
            if e and 0 < (e.get("duration") or 0) <= 180
            and (e.get("view_count") or 0) >= _MIN_VIEWS_YT_SHORTS
        ]
        print(f"  '{q}': {len(entries)} total, {len(short_entries)} pass filters")
        for e in short_entries[:2]:
            clip = _normalise_yt_shorts_entry(e)
            status = f"attributed to {clip['creator_name']}" if clip else "no attribution"
            print(f"    - {e.get('title','')[:50]}  views={e.get('view_count',0):,}  → {status}")
        yt_total += len(short_entries)

    # ── 6. Full discover run ──────────────────────────────────────────────────
    print("\n[6] discover_viral_clips() full run:")
    clips = discover_viral_clips(max_total=100)
    reddit_n = sum(1 for c in clips if c["discovery_source"] == "reddit_trending")
    yt_n     = sum(1 for c in clips if c["discovery_source"] == "youtube_shorts_trending")
    print(f"  Total clips:    {len(clips)}")
    print(f"  Reddit:         {reddit_n}")
    print(f"  YouTube Shorts: {yt_n}")
    print(f"  TikTok:         0 (unavailable)")
    print(f"  Instagram:      0 (unavailable)")
    if clips:
        print("\n  Top 5 by score:")
        for c in sorted(clips, key=lambda x: x["score"], reverse=True)[:5]:
            print(
                f"    [{c['discovery_source']}] {c['creator_name']} | "
                f"score={c['score']:.0f} | theme={c['theme']} | "
                f"'{c['title'][:40]}'"
            )

    print("\n" + "=" * 65)
    print("viral_discovery.py self-test complete.")
    print("=" * 65)
