"""
fetcher_twitch.py
=================
Fetches top viral clips from the Twitch Helix API.

Uses OAuth2 client-credentials flow (no user login needed — just your
client_id and client_secret from config.yaml).

SaaS Note:
    All functions accept an optional `user_config` / `user_prefs` parameter.
    In single-user mode, these are loaded from the YAML files automatically.
    In multi-user SaaS mode, pass in the user's stored credentials and
    preferences so multiple users can run independent fetch cycles.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── Twitch API base ────────────────────────────────────────────────────────────
_TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"
_TWITCH_API_BASE = "https://api.twitch.tv/helix"

# Clips fetched per API page (Twitch max is 100)
_PAGE_SIZE = 100

# Cached access token (process-level cache — resets each run)
_token_cache: Dict[str, Any] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Authentication
# ══════════════════════════════════════════════════════════════════════════════

def _get_access_token(client_id: str, client_secret: str) -> str:
    """
    Obtain a Twitch app access token using the client-credentials OAuth2 flow.
    Caches the token in memory until it expires.

    Args:
        client_id: Twitch application client ID.
        client_secret: Twitch application client secret.

    Returns:
        A valid Bearer token string.

    Raises:
        RuntimeError: If the token request fails.
    """
    now = datetime.now(timezone.utc)

    # Return cached token if still valid (with a 60-second buffer)
    if _token_cache.get("expires_at") and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    logger.debug("Requesting new Twitch access token...")
    response = requests.post(
        _TWITCH_AUTH_URL,
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=10,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Twitch token request failed ({response.status_code}): {response.text}"
        )

    data = response.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + timedelta(seconds=data["expires_in"] - 60)
    logger.debug("Twitch access token obtained (expires in ~%d s).", data["expires_in"])
    return _token_cache["access_token"]


def _headers(client_id: str, access_token: str) -> Dict[str, str]:
    """Build the standard Twitch API request headers."""
    return {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Lookup helpers
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_user_ids(
    usernames: List[str],
    client_id: str,
    access_token: str,
) -> Dict[str, str]:
    """
    Convert a list of Twitch usernames to {username: user_id} dict.

    Args:
        usernames: List of Twitch login names (lowercase).
        client_id: Twitch client ID.
        access_token: Valid Twitch Bearer token.

    Returns:
        Dict mapping username (str) → user_id (str).
    """
    if not usernames:
        return {}

    # Twitch allows up to 100 logins per request
    mapping: Dict[str, str] = {}
    for chunk_start in range(0, len(usernames), 100):
        chunk = usernames[chunk_start:chunk_start + 100]
        params = [("login", name) for name in chunk]
        resp = requests.get(
            f"{_TWITCH_API_BASE}/users",
            headers=_headers(client_id, access_token),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        for user in resp.json().get("data", []):
            mapping[user["login"].lower()] = user["id"]

    logger.debug("Resolved %d/%d Twitch usernames to IDs.", len(mapping), len(usernames))
    return mapping


def _resolve_game_id(
    game_name: str,
    client_id: str,
    access_token: str,
) -> Optional[str]:
    """
    Resolve a game name to its Twitch game ID.

    Returns:
        Game ID string, or None if not found.
    """
    resp = requests.get(
        f"{_TWITCH_API_BASE}/games",
        headers=_headers(client_id, access_token),
        params={"name": game_name},
        timeout=10,
    )
    resp.raise_for_status()
    games = resp.json().get("data", [])
    if not games:
        logger.warning("Twitch game not found: '%s'", game_name)
        return None
    return games[0]["id"]


# ══════════════════════════════════════════════════════════════════════════════
# Global trending streamer pool
# ══════════════════════════════════════════════════════════════════════════════

def fetch_trending_streamers(
    client_id: str,
    access_token: str,
    count: int = 150,
) -> List[Dict[str, Any]]:
    """
    Return the top `count` live streams sorted by viewer count right now.

    Uses GET /streams, which Twitch returns ordered by viewer count descending
    by default. Paginates using the cursor until `count` entries are collected
    or no further pages exist.

    Args:
        client_id:    Twitch application client ID.
        access_token: Valid Twitch Bearer token.
        count:        Target number of streams to collect (max 150).

    Returns:
        List of raw Twitch stream objects, each containing:
            user_id, user_login, user_name, game_id, game_name,
            viewer_count, title, thumbnail_url, etc.
    """
    streams: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    while len(streams) < count:
        params: Dict[str, Any] = {
            "first": min(_PAGE_SIZE, count - len(streams)),
            "language": "en",               # English-only streams
        }
        if cursor:
            params["after"] = cursor

        resp = requests.get(
            f"{_TWITCH_API_BASE}/streams",
            headers=_headers(client_id, access_token),
            params=params,
            timeout=15,
        )

        if resp.status_code == 429:
            logger.warning("Twitch rate limit hit while fetching trending streamers.")
            break

        resp.raise_for_status()
        data = resp.json()
        page = data.get("data", [])

        if not page:
            break

        streams.extend(page)
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor:
            break

    logger.debug("fetch_trending_streamers: collected %d live streams.", len(streams))
    return streams[:count]


# ══════════════════════════════════════════════════════════════════════════════
# Core fetch
# ══════════════════════════════════════════════════════════════════════════════

def fetch_clips(
    user_config: Optional[Dict] = None,
    user_prefs: Optional[Dict] = None,
    limit_per_streamer: int = 20,
    mode: str = "user",
) -> List[Dict[str, Any]]:
    """
    Fetch top viral Twitch clips from the last 24 hours.

    Reads target_streamers and target_games from preferences and pulls
    clips from each streamer. Optionally filters by game if target_games
    is set.

    Args:
        user_config: Dict with Twitch credentials. If None, loads from
                     config.yaml automatically.
        user_prefs:  Dict with user preferences. If None, loads from
                     preferences.yaml automatically.
        limit_per_streamer: Max clips to fetch per streamer (default 20).
        mode: 'user' (default) — fetch from configured target_streamers.
              'pool' — fetch only from global trending streamers; skips
                       target_streamers. Used by pool_fetcher.py.

    Returns:
        List of clip dicts, each containing:
            clip_id       (str)  — Twitch internal clip ID
            source        (str)  — always "twitch"
            title         (str)  — clip title
            url           (str)  — clip URL
            embed_url     (str)  — embeddable URL
            creator_name  (str)  — broadcaster login name
            duration      (float)— clip duration in seconds
            view_count    (int)  — total views
            created_at    (str)  — ISO 8601 creation timestamp
            game_id       (str)  — Twitch game ID
            thumbnail_url (str)  — thumbnail image URL
    """
    # ── Load config / prefs if not provided ───────────────────────────────────
    if user_config is None or user_prefs is None:
        from preferences import load_config, load_preferences
    if user_config is None:
        user_config = load_config()
    if user_prefs is None:
        user_prefs = load_preferences()

    client_id = user_config["twitch"]["client_id"]
    client_secret = user_config["twitch"]["client_secret"]

    if client_id == "YOUR_TWITCH_CLIENT_ID_HERE":
        raise RuntimeError(
            "Twitch credentials not configured. "
            "Edit config.yaml and fill in your client_id and client_secret."
        )

    target_streamers: List[str] = [
        s.lower() for s in user_prefs.get("target_streamers", [])
    ]
    target_games: List[str] = user_prefs.get("target_games", [])

    # In pool mode, skip the target_streamers section entirely.
    # pool_fetcher.py calls this with mode='pool' to get only trending content.
    if mode == "pool":
        target_streamers = []
    elif not target_streamers:
        logger.warning("No target_streamers configured in preferences. Skipping Twitch fetch.")
        return []

    # ── Auth ──────────────────────────────────────────────────────────────────
    access_token = _get_access_token(client_id, client_secret)

    # ── Resolve streamer usernames → user IDs ─────────────────────────────────
    user_id_map = _resolve_user_ids(target_streamers, client_id, access_token)
    # In pool mode target_streamers is empty by design — don't early-return here.
    if not user_id_map and mode != "pool":
        logger.warning("Could not resolve any Twitch streamer usernames.")
        return []

    # ── Optionally resolve game names → game IDs ──────────────────────────────
    game_ids: List[str] = []
    for game_name in target_games:
        gid = _resolve_game_id(game_name, client_id, access_token)
        if gid:
            game_ids.append(gid)

    # 24 hours ago in RFC3339 format (Twitch requires UTC)
    started_at = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    all_clips: List[Dict[str, Any]] = []
    seen_clip_ids: set = set()

    for username, broadcaster_id in user_id_map.items():
        logger.info("Fetching clips for Twitch streamer: %s", username)
        clips = _fetch_clips_for_broadcaster(
            broadcaster_id=broadcaster_id,
            client_id=client_id,
            access_token=access_token,
            started_at=started_at,
            game_ids=game_ids if game_ids else None,
            limit=limit_per_streamer,
        )

        for clip in clips:
            if clip["id"] not in seen_clip_ids:
                seen_clip_ids.add(clip["id"])
                all_clips.append(_normalize_clip(clip, username))

        logger.debug("  → %d clip(s) from %s", len(clips), username)

    # ── Global trending streamer pool — DISABLED ──────────────────────────────
    # The global trending pool is permanently disabled to prevent foreign-language
    # and unknown creator content from entering the pipeline.
    # pool_fetcher.py uses the TIER1_TWITCH whitelist exclusively via the
    # private helpers (_resolve_user_ids, _fetch_clips_for_broadcaster).
    # The allow_clips_from_non_target_streamers preference is intentionally
    # ignored here — no global discovery runs under any circumstances.

    # Sort by view count descending (highest viewed first)
    all_clips.sort(key=lambda c: c["view_count"], reverse=True)

    # ── Blocked creator filter ─────────────────────────────────────────────────
    try:
        from blocked_creators import is_blocked
        import audit
        passing = []
        for clip in all_clips:
            creator = clip.get("creator_name", "")
            if is_blocked(creator, "twitch"):
                logger.debug("Skipping opted-out creator: %s", creator)
                audit.log_blocked_creator(creator, "twitch")
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
        "Twitch fetch complete. %d unique clip(s) across %d streamer(s).",
        len(all_clips), len(user_id_map),
    )
    return all_clips


def _fetch_clips_for_broadcaster(
    broadcaster_id: str,
    client_id: str,
    access_token: str,
    started_at: str,
    game_ids: Optional[List[str]],
    limit: int,
) -> List[Dict]:
    """
    Fetch clips for a single broadcaster. Handles Twitch pagination.

    Args:
        broadcaster_id: Twitch numeric broadcaster ID.
        client_id: Twitch client ID.
        access_token: Valid Bearer token.
        started_at: RFC3339 datetime string — only clips created after this.
        game_ids: If provided, only return clips for these game IDs.
        limit: Maximum clips to return for this broadcaster.

    Returns:
        List of raw Twitch clip objects from the API.
    """
    clips: List[Dict] = []
    cursor: Optional[str] = None

    while len(clips) < limit:
        params: Dict[str, Any] = {
            "broadcaster_id": broadcaster_id,
            "started_at": started_at,
            "first": min(_PAGE_SIZE, limit - len(clips)),
        }
        if cursor:
            params["after"] = cursor

        resp = requests.get(
            f"{_TWITCH_API_BASE}/clips",
            headers=_headers(client_id, access_token),
            params=params,
            timeout=15,
        )

        if resp.status_code == 429:
            try:
                from rate_limiter import handle_twitch_response
                handle_twitch_response(resp, attempt=0)
            except ImportError:
                logger.warning("Twitch rate limit hit (429). Stopping this broadcaster fetch.")
            break

        resp.raise_for_status()
        data = resp.json()
        page_clips = data.get("data", [])

        if not page_clips:
            break  # No more clips

        # Filter by game_id if requested
        if game_ids:
            page_clips = [c for c in page_clips if c.get("game_id") in game_ids]

        clips.extend(page_clips)
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor:
            break  # Reached last page

    return clips[:limit]


def _normalize_clip(raw: Dict, creator_name_fallback: str) -> Dict[str, Any]:
    """Convert a raw Twitch API clip object to the ClipCast standard format."""
    return {
        "clip_id":      raw.get("id", ""),
        "source":       "twitch",
        "title":        raw.get("title", "Untitled Clip"),
        "url":          raw.get("url", ""),
        "embed_url":    raw.get("embed_url", ""),
        "creator_name": raw.get("broadcaster_name") or creator_name_fallback,
        "duration":     float(raw.get("duration", 0)),
        "view_count":   int(raw.get("view_count", 0)),
        "created_at":   raw.get("created_at", ""),
        "game_id":      raw.get("game_id", ""),
        "game":         raw.get("game_name", ""),
        "thumbnail_url": raw.get("thumbnail_url", ""),
    }


# ── Popular game IDs for global + category clip fetching ──────────────────────
TOP_GAME_IDS: Dict[str, str] = {
    "Just Chatting":        "509658",
    "Fortnite":             "33214",
    "League of Legends":    "21779",
    "Minecraft":            "27471",
    "Grand Theft Auto V":   "32982",
    "Valorant":             "516575",
    "Apex Legends":         "511224",
    "World of Warcraft":    "18122",
    "Counter-Strike 2":     "1229858182",
    "PUBG: Battlegrounds":  "493057",
    "Rocket League":        "30921",
    "Overwatch 2":          "515025",
    "Call of Duty: MW3":    "1678052513",
    "Dota 2":               "29595",
    "Elden Ring":           "65535",
}


def fetch_global_top_clips(
    client_id: str,
    token: str,
    max_clips: int = 100,
    days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Fetch the most viral clips across ALL of Twitch for the last `days` days.

    Since the Twitch API requires game_id or broadcaster_id, this first fetches
    the top live games via /games/top, then collects clips per game.  Returns
    clips from any creator, so they complement the TIER1_TWITCH whitelist.

    Args:
        client_id:  Twitch application client ID.
        token:      Valid Twitch Bearer token.
        max_clips:  Target clip count (default 100).
        days:       Lookback window in days (default 7).

    Returns:
        List of normalised clip dicts sorted by view_count DESC.
    """
    started_at = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fetch top 20 live games dynamically
    try:
        games_resp = requests.get(
            f"{_TWITCH_API_BASE}/games/top",
            headers=_headers(client_id, token),
            params={"first": 20},
            timeout=10,
        )
        if games_resp.status_code == 200:
            game_ids = [g["id"] for g in games_resp.json().get("data", [])]
        else:
            logger.warning(
                "fetch_global_top_clips: /games/top returned %d, using TOP_GAME_IDS",
                games_resp.status_code,
            )
            game_ids = list(TOP_GAME_IDS.values())
    except Exception as exc:
        logger.warning("fetch_global_top_clips: /games/top error: %s — using TOP_GAME_IDS", exc)
        game_ids = list(TOP_GAME_IDS.values())

    clips_per_game = max(2, max_clips // max(len(game_ids), 1))
    all_clips: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for gid in game_ids:
        if len(all_clips) >= max_clips:
            break
        try:
            resp = requests.get(
                f"{_TWITCH_API_BASE}/clips",
                headers=_headers(client_id, token),
                params={
                    "game_id":    gid,
                    "started_at": started_at,
                    "first":      min(clips_per_game, _PAGE_SIZE),
                },
                timeout=15,
            )
            if resp.status_code == 429:
                logger.warning("fetch_global_top_clips: rate limited on game_id=%s", gid)
                break
            if resp.status_code != 200:
                continue
            for raw in resp.json().get("data", []):
                cid = raw.get("id", "")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    clip = _normalize_clip(raw, raw.get("broadcaster_name", ""))
                    clip["discovery_source"] = "twitch_api"
                    all_clips.append(clip)
        except Exception as exc:
            logger.debug("fetch_global_top_clips: game_id=%s error: %s", gid, exc)

    all_clips.sort(key=lambda c: c.get("view_count", 0), reverse=True)
    logger.info(
        "fetch_global_top_clips: %d clips from %d game categories",
        len(all_clips), len(game_ids),
    )
    return all_clips[:max_clips]


def fetch_top_clips_by_category(
    client_id: str,
    token: str,
    clips_per_game: int = 10,
    days: int = 7,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch top clips per game from TOP_GAME_IDS for the last `days` days.

    Returns a dict keyed by game name, each containing a list of clip dicts.
    Useful for targeted content discovery by category.

    Args:
        client_id:      Twitch application client ID.
        token:          Valid Twitch Bearer token.
        clips_per_game: Max clips to fetch per game (default 10).
        days:           Lookback window in days (default 7).

    Returns:
        Dict mapping game_name → list of normalised clip dicts.
    """
    started_at = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    results: Dict[str, List[Dict[str, Any]]] = {}

    for game_name, game_id in TOP_GAME_IDS.items():
        try:
            resp = requests.get(
                f"{_TWITCH_API_BASE}/clips",
                headers=_headers(client_id, token),
                params={
                    "game_id":    game_id,
                    "started_at": started_at,
                    "first":      min(clips_per_game, _PAGE_SIZE),
                },
                timeout=15,
            )
            if resp.status_code == 429:
                logger.warning(
                    "fetch_top_clips_by_category: rate limit hit at game=%s", game_name
                )
                break
            if resp.status_code != 200:
                logger.debug(
                    "fetch_top_clips_by_category: %s returned %d", game_name, resp.status_code
                )
                continue

            raw_clips = resp.json().get("data", [])
            normalized = []
            for raw in raw_clips:
                clip = _normalize_clip(raw, raw.get("broadcaster_name", ""))
                clip["game"] = game_name
                clip["discovery_source"] = "twitch_api"
                normalized.append(clip)

            results[game_name] = normalized
            logger.debug(
                "fetch_top_clips_by_category: %s → %d clips", game_name, len(normalized)
            )
        except Exception as exc:
            logger.debug(
                "fetch_top_clips_by_category: error for %s: %s", game_name, exc
            )

    total = sum(len(v) for v in results.values())
    logger.info(
        "fetch_top_clips_by_category: %d clips across %d games", total, len(results)
    )
    return results


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing fetcher_twitch.py...")
    print("Loading config and preferences...")

    try:
        from preferences import load_config, load_preferences
        config = load_config()
        prefs = load_preferences()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if config["twitch"]["client_id"] == "YOUR_TWITCH_CLIENT_ID_HERE":
        print("Twitch credentials not set. Edit config.yaml first.")
        sys.exit(1)

    print(f"Target streamers: {prefs.get('target_streamers')}")
    print("Fetching clips (last 24 hours)...\n")

    clips = fetch_clips(user_config=config, user_prefs=prefs, limit_per_streamer=5)

    if not clips:
        print("No clips returned. This could mean:\n"
              "  • No clips in the last 24 hours for your target streamers\n"
              "  • Your game filter doesn't match any clips\n"
              "  • API credentials are incorrect")
    else:
        print(f"Fetched {len(clips)} clip(s):\n")
        for clip in clips[:10]:
            print(f"  [{clip['creator_name']}] {clip['title'][:60]}")
            print(f"    Duration: {clip['duration']}s | Views: {clip['view_count']:,}")
            print(f"    URL: {clip['url']}\n")

    print("Twitch fetcher test complete.")
