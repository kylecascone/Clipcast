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
_MIN_VIEWS_YT_SHORTS  = 50_000   # only clips with proven view traction
_MIN_UPVOTES_REDDIT   = 1_000    # frontpage-level validation required

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

# ── Subreddit → content_category mapping ──────────────────────────────────────
_SUBREDDIT_CATEGORIES: Dict[str, str] = {
    # Fails / viral moments
    "PublicFreakout":       "fails",
    "nextfuckinglevel":     "fails",
    "WTF":                  "fails",
    "unexpected":           "fails",
    "maybemaybemaybe":      "fails",
    "therewasanattempt":    "fails",
    "instant_regret":       "fails",
    "nonononoyes":          "fails",
    # Sports
    "sports":               "sports",
    "nba":                  "sports",
    "soccer":               "sports",
    "MMA":                  "sports",
    "Boxing":               "sports",
    "golf":                 "sports",
    # Gaming / streaming
    "LivestreamFail":       "gaming",
    "LivestreamFails":      "gaming",
    "gaming":               "gaming",
    "xboxone":              "gaming",
    "pcgaming":             "gaming",
    "twitchclips":          "gaming",
    "xqcow":                "gaming",
    "KaiCenat":             "gaming",
    "IShowSpeed":           "gaming",
    "AdinRoss":             "gaming",
    # News
    "news":                 "news",
    "worldnews":            "news",
    "politics":             "news",
    # Podcast
    "JoeRogan":             "podcast",
    "podcast":              "podcast",
}

# ── Reddit subreddits (prioritised — 1 000+ upvote frontpage content only) ────
REDDIT_SUBREDDITS: List[str] = [
    # Fails / viral moments
    "PublicFreakout",
    "nextfuckinglevel",
    "WTF",
    "unexpected",
    "maybemaybemaybe",
    "therewasanattempt",
    "instant_regret",
    "nonononoyes",
    # Sports
    "sports",
    "nba",
    "soccer",
    "MMA",
    "Boxing",
    "golf",
    # Gaming / streaming
    "LivestreamFail",
    "LivestreamFails",
    "gaming",
    "xboxone",
    "pcgaming",
    "twitchclips",
    # Podcast / news
    "JoeRogan",
    "news",
    "worldnews",
]
_REDDIT_HEADERS = {"User-Agent": "ClipCastBot/1.0 (personal automation tool)"}

# ── YouTube Shorts search queries — categorised for broad appeal ───────────────
YT_SHORTS_QUERIES: List[str] = [
    # Gaming / streaming (content_category: gaming)
    "twitch streamer insane moment viral",
    "gaming rage quit funny clip",
    "streamer reaction insane clip shorts",
    "twitch ban clip viral shorts",
    "streamer freaks out viral moment",
    "twitch highlights funny moments",
    "kick streamer clip viral",
    # Sports (content_category: sports)
    "nba insane dunk viral shorts",
    "soccer crazy goal viral",
    "mma knockout viral clip",
    "nfl crazy play viral shorts",
    "boxing knockout viral clip",
    "sports fail funny viral",
    "athlete insane moment viral",
    # Fails / reactions (content_category: fails)
    "public freakout viral shorts",
    "crazy fail compilation viral",
    "unexpected moment viral clip",
    "people fail funny viral",
    "insane reaction viral shorts",
    "wild moment caught on camera",
    "viral video nobody expected",
    # News / events (content_category: news)
    "news moment viral shorts",
    "politician insane moment viral",
    "breaking news viral clip",
    # Podcasts (content_category: podcast)
    "joe rogan clip viral moment",
    "podcast viral moment shorts",
    "interview insane reaction viral",
    # General trending
    "viral video millions views shorts",
    "insane moment viral 2024",
    "funniest viral clip shorts",
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
    url_base = f"https://www.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"
    old_url  = f"https://old.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"
    api_url  = f"https://api.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"

    _VIDEO_URL_PATTERNS = (
        "v.redd.it",
        "youtube.com/watch", "youtu.be",
        "clips.twitch.tv", "twitch.tv/clip",
        "streamable.com",
        "twitter.com/i/status", "x.com/i/status",
    )

    def _extract_video_posts(r) -> List[Dict]:
        posts = r.json().get("data", {}).get("children", [])
        results = []
        for p in posts:
            d = p.get("data", {})
            url = d.get("url") or d.get("url_overridden_by_dest") or ""
            # Accept native Reddit video OR external video URLs
            if d.get("is_video") or any(pat in url for pat in _VIDEO_URL_PATTERNS):
                results.append(d)
        return results

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

    title   = post.get("title") or ""
    url     = post.get("url") or post.get("url_overridden_by_dest") or ""
    post_id = post.get("id") or url

    if not url:
        return None

    subreddit       = post.get("subreddit") or ""
    subreddit_lower = subreddit.lower()

    # Determine content_category from subreddit
    content_category = _SUBREDDIT_CATEGORIES.get(subreddit_lower, "") or \
                       _SUBREDDIT_CATEGORIES.get(subreddit, "")

    # Duration: available for native Reddit videos; 0 for external links (editor probes later)
    duration  = 0.0
    has_audio = True
    is_native_reddit = "v.redd.it" in url

    if is_native_reddit:
        media        = post.get("media") or post.get("secure_media") or {}
        reddit_video = media.get("reddit_video") or {}
        duration     = float(reddit_video.get("duration") or 0)
        has_audio    = bool(reddit_video.get("has_audio", True))
        if duration > 180:
            return None
    else:
        # External video link — duration unknown until yt-dlp probes it
        # Accept it; editor will probe & reject if out of range
        duration = 60.0  # placeholder so scorer doesn't reject as "0s"

    # Trace to original creator
    origin = find_original_source(title, post.get("selftext", ""), post.get("author", ""))

    if origin is None:
        if subreddit_lower in _SUBREDDIT_TO_CREATOR:
            src, name = _SUBREDDIT_TO_CREATOR[subreddit_lower]
            origin = {"source": src, "creator_name": name}

    # For broad viral subreddits accept without strict creator attribution
    if origin is None:
        # Infer source from URL
        if "youtube.com" in url or "youtu.be" in url:
            src = "youtube"
        elif "twitch.tv" in url:
            src = "twitch"
        else:
            src = "youtube"
        origin = {"source": src, "creator_name": post.get("author") or "viral_clip"}

    boost = _compute_viral_boost("reddit_trending", ups)
    view_count = ups * 50  # rough proxy

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
        "category":         content_category,
        "content_category": content_category,
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


_YT_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "sports":  ["nba", "nfl", "soccer", "mma", "boxing", "golf", "athlete", "dunk", "goal", "knockout"],
    "fails":   ["freakout", "fail", "unexpected", "wtf", "crazy", "public", "reaction", "insane moment"],
    "gaming":  ["twitch", "streamer", "gaming", "kick", "gamer", "stream", "rage", "ban"],
    "news":    ["news", "politician", "breaking", "president", "government"],
    "podcast": ["rogan", "podcast", "interview", "episode"],
}


def _infer_yt_category(title: str, uploader: str) -> str:
    """Infer content_category from title and uploader keywords."""
    combined = (title + " " + uploader).lower()
    for cat, keywords in _YT_CATEGORY_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return cat
    return "trending"


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
        # Accept clips even without Tier-1 attribution — quality gate handles relevance
        origin = {"source": "youtube", "creator_name": uploader or "viral_clip"}

    boost            = _compute_viral_boost("youtube_shorts_trending", view_count)
    content_category = _infer_yt_category(title, uploader)

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
        "category":         content_category,
        "content_category": content_category,
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

_TIKTOK_HASHTAGS: List[tuple] = [
    # (hashtag, content_category)
    ("gaming",  "gaming"),
    ("sports",  "sports"),
    ("fails",   "fails"),
    ("viral",   "trending"),
    ("fyp",     "trending"),
]

_MIN_VIEWS_TIKTOK = 500_000


def scrape_tiktok_hashtag(hashtag: str, max_results: int = 20) -> List[Dict]:
    """
    Attempt to scrape TikTok trending videos for a hashtag via yt-dlp.

    yt-dlp's TikTok extractor is fragile and IP-blocked on datacenter IPs.
    This function tries but returns [] silently on any failure — never
    crashes the pipeline.
    """
    url = f"https://www.tiktok.com/tag/{hashtag}"
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--quiet",
        "--playlist-end", str(max_results),
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.debug("scrape_tiktok_hashtag #%s: yt-dlp failed (expected on Railway)", hashtag)
            return []
        data = json.loads(result.stdout)
        entries = data.get("entries") or []
        clips: List[Dict] = []
        for e in entries:
            if not e:
                continue
            view_count = int(e.get("view_count") or 0)
            if view_count < _MIN_VIEWS_TIKTOK:
                continue
            duration = float(e.get("duration") or 0)
            if duration <= 0 or duration > 180:
                continue
            vid_id = e.get("id") or e.get("url", "")
            title  = (e.get("title") or e.get("description") or hashtag)[:200]
            clips.append({
                "clip_id":          f"viral_tiktok_{vid_id[:64]}",
                "source":           "youtube",
                "creator_name":     e.get("uploader") or e.get("channel") or "tiktok_viral",
                "title":            title,
                "url":              e.get("url") or e.get("webpage_url") or "",
                "duration":         duration,
                "duration_sec":     duration,
                "view_count":       view_count,
                "upvotes":          0,
                "score":            65.0,
                "has_music":        True,
                "language":         "en",
                "game":             "",
                "category":         "trending",
                "content_category": "trending",
                "mode":             "auto",
                "discovery_source": "tiktok_trending",
                "viral_title":      title,
                "theme":            detect_theme(title),
            })
        logger.info("scrape_tiktok_hashtag #%s: %d clips >= %dK views", hashtag, len(clips), _MIN_VIEWS_TIKTOK // 1000)
        return clips
    except Exception as exc:
        logger.debug("scrape_tiktok_hashtag #%s: %s", hashtag, exc)
        return []


def fetch_tiktok_trending(max_clips: int = 30) -> List[Dict]:
    """Scrape TikTok trending hashtags. Silently returns [] on IP-blocked environments."""
    clips: List[Dict] = []
    seen_ids: set = set()
    for hashtag, category in _TIKTOK_HASHTAGS:
        if len(clips) >= max_clips:
            break
        for c in scrape_tiktok_hashtag(hashtag, max_results=10):
            cid = c.get("clip_id", "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                c["content_category"] = category
                clips.append(c)
        time.sleep(0.5)
    if clips:
        logger.info("fetch_tiktok_trending: %d clips found across %d hashtags", len(clips), len(_TIKTOK_HASHTAGS))
    return clips


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
    include_tiktok: bool = True,
    include_instagram: bool = False,  # yt-dlp does not support profile pages
    max_total: int = 100,
) -> List[Dict]:
    """
    Orchestrate viral discovery across all active platforms.

    Only pulls content with proven engagement (1 000+ Reddit upvotes,
    50 000+ YouTube views, 500 000+ TikTok views).

    Args:
        include_youtube_shorts:  Scrape YouTube Shorts (default True).
        include_reddit:          Scrape Reddit — 24-hour top posts (default True).
        include_tiktok:          Attempt TikTok hashtag scraping (default True,
                                 silently skipped when IP-blocked).
        include_instagram:       Currently unavailable — always returns 0.
        max_total:               Cap on total clips returned (default 100).

    Returns:
        List of normalised clip dicts with content_category set, deduplicated by clip_id.
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

    # ── Reddit (24-hour top posts, 1000+ upvotes required) ───────────────────
    if include_reddit:
        logger.info(
            "viral_discovery: scraping %d Reddit subreddits (top/day, min %d upvotes)…",
            len(REDDIT_SUBREDDITS), _MIN_UPVOTES_REDDIT,
        )
        reddit_raw = reddit_added = 0
        for sub in REDDIT_SUBREDDITS:
            if len(clips) >= max_total:
                break
            try:
                posts = scrape_reddit(sub, limit=25)
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
            "viral_discovery: Reddit — %d raw posts → %d clips (>=%d upvotes)",
            reddit_raw, reddit_added, _MIN_UPVOTES_REDDIT,
        )

    # ── YouTube Shorts ────────────────────────────────────────────────────────
    if include_youtube_shorts:
        logger.info("viral_discovery: scraping YouTube Shorts (%d queries, min %dK views)…",
                    len(YT_SHORTS_QUERIES), _MIN_VIEWS_YT_SHORTS // 1000)
        yt_raw = yt_added = 0
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
        logger.info("viral_discovery: YouTube Shorts — %d raw → %d clips", yt_raw, yt_added)

    # ── TikTok (best-effort — silently skipped when IP-blocked) ──────────────
    if include_tiktok:
        try:
            tiktok_clips = fetch_tiktok_trending(max_clips=30)
            for c in tiktok_clips:
                _add(c)
            if tiktok_clips:
                logger.info("viral_discovery: TikTok — %d clips", len(tiktok_clips))
        except Exception as exc:
            logger.debug("viral_discovery: TikTok fetch error (non-fatal): %s", exc)

    if include_instagram:
        logger.debug("viral_discovery: Instagram profile scraping unavailable (yt-dlp limitation).")

    reddit_count  = sum(1 for c in clips if c.get("discovery_source") == "reddit_trending")
    yt_count      = sum(1 for c in clips if c.get("discovery_source") == "youtube_shorts_trending")
    tiktok_count  = sum(1 for c in clips if c.get("discovery_source") == "tiktok_trending")

    logger.info(
        "viral_discovery: complete — %d total clips (reddit=%d, yt_shorts=%d, tiktok=%d).",
        len(clips), reddit_count, yt_count, tiktok_count,
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
