"""
pool_fetcher.py
===============
Global content fetcher for the ClipCast Studio shared pool.

WHITELIST-ONLY POLICY
---------------------
The pool ONLY fetches clips from creators explicitly listed in viral_creators.py.
No trending discovery, no global search queries, no unknown streamers.
This guarantees every clip in the pool comes from a known English-speaking
viral creator.

Fetch strategy
--------------
Twitch:
    Top 5 clips (last 7 days) from every creator in TIER1_TWITCH.
    Resolves usernames → broadcaster IDs, then fetches per-creator.

YouTube:
    Most recent videos (last 14 days) from every channel in TIER1_YOUTUBE.
    Loops through the channel ID dict directly — no search queries.
    Cost: 100 quota units per channel × 20 channels = ~2000 units/run.

Kick:
    Top clips from every channel in TIER1_KICK via public API.

SaaS Note:
    refresh_all_pools() is designed to be called once per schedule tick.
    Individual users call get_clips_for_user() from shared_pool.py.

Usage
-----
    import pool_fetcher
    result = pool_fetcher.refresh_all_pools(user_config, user_prefs=prefs)
    # result: {"twitch_added": N, "youtube_added": N, "kick_added": N, ...}
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import database
import shared_pool

logger = logging.getLogger(__name__)

# ── Placeholder sentinel values copied from config.yaml defaults ───────────────
_TWITCH_PLACEHOLDER  = "YOUR_TWITCH_CLIENT_ID_HERE"
_YOUTUBE_PLACEHOLDER = "YOUR_YOUTUBE_DATA_API_KEY_HERE"


# ══════════════════════════════════════════════════════════════════════════════
# Quota level helper
# (imported from rate_limiter if available; defined locally as fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _get_quota_level() -> str:
    """
    Return 'ok', 'warn', or 'critical' based on today's YouTube quota usage.

    Tries to import get_youtube_quota_level from rate_limiter first.
    Falls back to a local implementation if that symbol does not exist yet.

    Returns:
        'ok'       — quota usage < 80 %
        'warn'     — quota usage >= 80 % and < 95 %
        'critical' — quota usage >= 95 %
    """
    # Prefer the exported symbol from rate_limiter if it exists
    try:
        from rate_limiter import get_youtube_quota_level  # type: ignore[attr-defined]
        return get_youtube_quota_level()
    except (ImportError, AttributeError):
        pass

    # Local fallback implementation
    from datetime import date
    today = date.today().isoformat()
    try:
        current = database.get_daily_quota("youtube", date_str=today, user_id=1)
    except Exception:
        return "ok"
    pct = current / 10_000
    if pct >= 0.95:
        return "critical"
    if pct >= 0.80:
        return "warn"
    return "ok"


# ══════════════════════════════════════════════════════════════════════════════
# Twitch pool refresh
# ══════════════════════════════════════════════════════════════════════════════

def refresh_twitch_pool(
    user_config: Dict[str, Any],
    clips_per_creator: int = 5,
    db_path: Optional[Path] = None,
) -> Dict[str, int]:
    """
    Fetch clips from TIER1_TWITCH whitelist + global top clips + category clips.

    Steps:
        1. Verify Twitch credentials.
        2. Fetch TIER1_TWITCH whitelist clips (per-creator).
        3. Fetch global top clips across all Twitch (fetch_global_top_clips).
        4. Fetch top clips by game category (fetch_top_clips_by_category).
        5. Score and insert all clips into the shared pool.

    Args:
        user_config:       Config dict with twitch.client_id / client_secret.
        clips_per_creator: Max clips to fetch per TIER1 creator (default 5).
        db_path:           Override database path.

    Returns:
        Dict with twitch_added, global_clips_added, category_clips_added.
    """
    _zero = {"twitch_added": 0, "global_clips_added": 0, "category_clips_added": 0}

    twitch_cfg = user_config.get("twitch", {})
    client_id     = twitch_cfg.get("client_id", "")
    client_secret = twitch_cfg.get("client_secret", "")

    if not client_id or client_id == _TWITCH_PLACEHOLDER:
        logger.info("refresh_twitch_pool: Twitch credentials not configured — skipping.")
        return _zero
    if not client_secret or client_secret.startswith("YOUR_"):
        logger.info("refresh_twitch_pool: Twitch client_secret not configured — skipping.")
        return _zero

    run_id = shared_pool.log_pool_run_start("twitch", db_path=db_path)
    twitch_added   = 0
    global_added   = 0
    category_added = 0

    try:
        from viral_creators import TIER1_TWITCH
        from fetcher_twitch import (
            _get_access_token,
            _resolve_user_ids,
            _fetch_clips_for_broadcaster,
            _normalize_clip,
            fetch_global_top_clips,
            fetch_top_clips_by_category,
        )
        from scorer import score_clip

        access_token = _get_access_token(client_id, client_secret)
        started_at = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── TIER1 whitelist clips ─────────────────────────────────────────────
        logger.info(
            "refresh_twitch_pool: resolving %d TIER1_TWITCH creator(s)…",
            len(TIER1_TWITCH),
        )
        user_id_map = _resolve_user_ids(TIER1_TWITCH, client_id, access_token)
        logger.info(
            "refresh_twitch_pool: resolved %d/%d creators — fetching clips…",
            len(user_id_map), len(TIER1_TWITCH),
        )

        for username, broadcaster_id in user_id_map.items():
            try:
                raw_clips = _fetch_clips_for_broadcaster(
                    broadcaster_id=broadcaster_id,
                    client_id=client_id,
                    access_token=access_token,
                    started_at=started_at,
                    game_ids=None,
                    limit=clips_per_creator,
                )
            except Exception as exc:
                logger.debug(
                    "refresh_twitch_pool: error fetching clips for %s: %s", username, exc
                )
                continue

            for raw in raw_clips:
                try:
                    clip = _normalize_clip(raw, username)
                    clip["discovery_source"] = "twitch_api"
                    clip = score_clip(clip, user_prefs=None)
                    if shared_pool.add_clip_to_pool(clip, db_path=db_path):
                        twitch_added += 1
                except Exception as exc:
                    logger.debug("refresh_twitch_pool: error processing clip: %s", exc)

        print(f"  Twitch TIER1 whitelist: {twitch_added} new clips")

        # ── Global top clips ──────────────────────────────────────────────────
        try:
            global_clips = fetch_global_top_clips(client_id, access_token, max_clips=100)
            logger.info(
                "refresh_twitch_pool: global top clips — %d clips fetched",
                len(global_clips),
            )
            for clip in global_clips:
                try:
                    clip = score_clip(clip, user_prefs=None)
                    if shared_pool.add_clip_to_pool(clip, db_path=db_path):
                        global_added += 1
                except Exception as exc:
                    logger.debug("refresh_twitch_pool: global clip error: %s", exc)
            print(f"  Twitch global top clips: {global_added} new clips")
        except Exception as exc:
            logger.warning("refresh_twitch_pool: global fetch error (non-fatal): %s", exc)
            print(f"  Twitch global top clips: error — {exc}")

        # ── Category clips ────────────────────────────────────────────────────
        try:
            category_dict = fetch_top_clips_by_category(
                client_id, access_token, clips_per_game=10
            )
            cat_fetched = sum(len(v) for v in category_dict.values())
            logger.info(
                "refresh_twitch_pool: category clips — %d clips across %d games",
                cat_fetched, len(category_dict),
            )
            for _game, game_clips in category_dict.items():
                for clip in game_clips:
                    try:
                        clip = score_clip(clip, user_prefs=None)
                        if shared_pool.add_clip_to_pool(clip, db_path=db_path):
                            category_added += 1
                    except Exception as exc:
                        logger.debug("refresh_twitch_pool: category clip error: %s", exc)
            print(f"  Twitch category clips: {category_added} new clips")
        except Exception as exc:
            logger.warning("refresh_twitch_pool: category fetch error (non-fatal): %s", exc)
            print(f"  Twitch category clips: error — {exc}")

        total = twitch_added + global_added + category_added
        logger.info(
            "refresh_twitch_pool: complete — tier1=%d global=%d category=%d total=%d",
            twitch_added, global_added, category_added, total,
        )
        shared_pool.complete_pool_run(
            run_id, clips_added=total, clips_expired=0,
            status="completed", db_path=db_path,
        )
        return {
            "twitch_added":        twitch_added,
            "global_clips_added":  global_added,
            "category_clips_added": category_added,
        }

    except Exception as exc:
        logger.error("refresh_twitch_pool: unexpected error: %s", exc, exc_info=True)
        shared_pool.complete_pool_run(
            run_id, clips_added=twitch_added + global_added + category_added,
            clips_expired=0, status="error", db_path=db_path,
        )
        return _zero


# ══════════════════════════════════════════════════════════════════════════════
# YouTube pool refresh
# ══════════════════════════════════════════════════════════════════════════════

def refresh_youtube_pool(
    user_config: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> int:
    """
    Fetch videos from every channel in TIER1_YOUTUBE (whitelist-only).

    No trending chart. No search queries. No related channel discovery.
    Only the exact channels in viral_creators.TIER1_YOUTUBE.

    Loops through each channel ID and calls _search_channel() to get the
    most recent short-form videos (last 14 days). Cost: ~100 quota units
    per channel × 20 channels = ~2000 quota units per full run.

    If quota level is 'critical', returns 0 immediately.

    Args:
        user_config: Config dict with youtube.api_key.
        db_path:     Override database path.

    Returns:
        Number of new clips inserted into the shared pool.
    """
    # 1. Credential check
    youtube_cfg = user_config.get("youtube", {})
    api_key = youtube_cfg.get("api_key", "")

    if not api_key or api_key == _YOUTUBE_PLACEHOLDER:
        logger.info("refresh_youtube_pool: YouTube API key not configured — skipping.")
        return 0

    # 2. Quota check — fall back to yt-dlp when critical
    quota_level = _get_quota_level()
    if quota_level == "critical":
        logger.warning(
            "refresh_youtube_pool: YouTube quota is CRITICAL — "
            "falling back to yt-dlp discovery."
        )
        try:
            from fetcher_youtube import fetch_youtube_via_ytdlp
            from scorer import score_clip
            ytdlp_clips = fetch_youtube_via_ytdlp(max_clips=50)
            print(f"  YouTube yt-dlp fallback (quota critical): {len(ytdlp_clips)} clips found")
            run_id = shared_pool.log_pool_run_start("youtube", db_path=db_path)
            added = 0
            for clip in ytdlp_clips:
                try:
                    clip = score_clip(clip, user_prefs=None)
                    if shared_pool.add_clip_to_pool(clip, db_path=db_path):
                        added += 1
                except Exception:
                    pass
            shared_pool.complete_pool_run(
                run_id, clips_added=added, clips_expired=0,
                status="completed", db_path=db_path,
            )
            print(f"  YouTube yt-dlp fallback: {added} new clips added")
            return added
        except Exception as exc:
            logger.warning("refresh_youtube_pool: yt-dlp fallback failed: %s", exc)
            return 0

    run_id = shared_pool.log_pool_run_start("youtube", db_path=db_path)
    clips_added = 0

    try:
        from viral_creators import TIER1_YOUTUBE
        from fetcher_youtube import _search_channel
        from scorer import score_clip

        # Quota helpers
        try:
            from rate_limiter import check_youtube_quota, record_youtube_quota
            _qcheck = check_youtube_quota
            _qrec   = record_youtube_quota
        except ImportError:
            _qcheck = lambda *_: True   # type: ignore[assignment]
            _qrec   = lambda *_: None   # type: ignore[assignment]

        published_after = (
            datetime.now(timezone.utc) - timedelta(days=14)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(
            "refresh_youtube_pool: fetching from %d TIER1_YOUTUBE channel(s) — "
            "whitelist-only, no trending discovery…",
            len(TIER1_YOUTUBE),
        )

        for display_name, channel_id in TIER1_YOUTUBE.items():
            # Each _search_channel call costs ~100 quota units (search.list)
            if not _qcheck("search.list"):
                logger.warning(
                    "refresh_youtube_pool: quota exhausted — stopping early "
                    "after %d clip(s).", clips_added
                )
                break

            try:
                videos = _search_channel(
                    channel_id=channel_id,
                    api_key=api_key,
                    published_after=published_after,
                    target_games=[],
                    limit=5,
                )
                _qrec("search.list")

                for clip in videos:
                    try:
                        clip = score_clip(clip, user_prefs=None)
                        if shared_pool.add_clip_to_pool(clip, db_path=db_path):
                            clips_added += 1
                            logger.debug(
                                "  + [youtube/%s] %s",
                                display_name, clip.get("title", "")[:50],
                            )
                    except Exception as exc:
                        logger.debug(
                            "refresh_youtube_pool: error adding clip from %s: %s",
                            display_name, exc,
                        )

                logger.debug(
                    "refresh_youtube_pool: %s → %d video(s) found.",
                    display_name, len(videos),
                )

            except Exception as exc:
                logger.warning(
                    "refresh_youtube_pool: error fetching %s (%s): %s",
                    display_name, channel_id, exc,
                )

        logger.info(
            "refresh_youtube_pool: complete — %d new clip(s) from %d TIER1 channel(s).",
            clips_added, len(TIER1_YOUTUBE),
        )
        shared_pool.complete_pool_run(
            run_id, clips_added=clips_added, clips_expired=0,
            status="completed", db_path=db_path,
        )
        return clips_added

    except Exception as exc:
        logger.error("refresh_youtube_pool: unexpected error: %s", exc, exc_info=True)
        shared_pool.complete_pool_run(
            run_id, clips_added=clips_added, clips_expired=0,
            status="error", db_path=db_path,
        )
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Kick pool refresh
# ══════════════════════════════════════════════════════════════════════════════

def refresh_kick_pool(
    db_path: Optional[Path] = None,
) -> int:
    """
    Fetch viral clips from Kick.com and add them to the shared pool.

    No credentials required — uses Kick's public API.
    Respects kick_enabled preference (checked by caller in refresh_all_pools).

    Returns:
        Number of new clips inserted into the shared pool.
    """
    run_id = shared_pool.log_pool_run_start("kick", db_path=db_path)
    clips_added = 0

    try:
        from fetcher_kick import fetch_kick_pool
        from scorer import score_clip

        logger.info("refresh_kick_pool: fetching Kick.com clips…")
        kick_clips = fetch_kick_pool(
            max_clips_per_channel=5,
            max_channels=20,
        )
        logger.info("refresh_kick_pool: got %d clip(s), scoring and inserting…", len(kick_clips))

        for clip in kick_clips:
            try:
                clip = score_clip(clip, user_prefs=None)
                if shared_pool.add_clip_to_pool(clip, db_path=db_path):
                    clips_added += 1
            except Exception as exc:
                logger.debug("refresh_kick_pool: error processing clip: %s", exc)

        logger.info(
            "refresh_kick_pool: complete — %d new clip(s) added to shared pool.",
            clips_added,
        )
        shared_pool.complete_pool_run(
            run_id, clips_added=clips_added, clips_expired=0,
            status="completed", db_path=db_path,
        )
        return clips_added

    except Exception as exc:
        logger.error("refresh_kick_pool: unexpected error: %s", exc, exc_info=True)
        shared_pool.complete_pool_run(
            run_id, clips_added=clips_added, clips_expired=0,
            status="error", db_path=db_path,
        )
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Language filter
# ══════════════════════════════════════════════════════════════════════════════

def is_english_content(clip: dict) -> bool:
    """
    Return True if the clip is English (or language is unset/ambiguous).
    Rejects clips where:
      - The explicit language field is a known non-English locale, OR
      - The title+creator contain >30% non-ASCII alphabetic characters
        (catches Arabic, Chinese, Japanese, Korean, Cyrillic, Thai, etc.)
    """
    lang = (clip.get("language") or "").lower().strip()
    if lang and lang not in ("", "en", "en-us", "en-gb", "english"):
        return False

    title   = clip.get("title", "") or ""
    creator = clip.get("creator_name", "") or ""
    combined = title + creator

    total_alpha = sum(1 for c in combined if c.isalpha())
    if total_alpha > 0:
        non_ascii = sum(1 for c in combined if ord(c) > 127)
        if non_ascii / total_alpha > 0.3:
            return False

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Two-layer quality filter
# ══════════════════════════════════════════════════════════════════════════════

def filter_and_score_clips(clips: List[Dict]) -> List[Dict]:
    """
    Run the two-layer quality filter on a list of raw clips.

    Layer 1 — Rule-based (passes_quality_filter):
        Fast, free.  Auto-approves strong signals; rejects obvious junk.
    Layer 2 — AI analysis (batch_analyze_clips):
        Only runs on clips that didn't auto-approve in Layer 1.
        Requires ANTHROPIC_API_KEY.  Skipped gracefully if not set.

    After filtering, computes final_score for every approved clip.

    Args:
        clips: Raw clip dicts from any discovery source.

    Returns:
        Approved clips sorted by final_score DESC, each with:
        'ai_quality_score', 'ai_analyzed', and 'final_score' added.
    """
    from scorer import passes_quality_filter, calculate_final_score, content_safety_filter
    from clip_analyzer import batch_analyze_clips

    auto_approved: List[Dict] = []
    needs_ai: List[Dict] = []
    rule_rejected_count    = 0
    safety_rejected_count  = 0
    language_rejected_count = 0

    for clip in clips:
        if not is_english_content(clip):
            language_rejected_count += 1
            print(f"  Skipped non-English: {clip.get('title','')[:60]}")
            continue

        if not content_safety_filter(clip):
            safety_rejected_count += 1
            print(f"  Safety rejected: {clip.get('title','')[:55]}")
            continue

        passed, reason = passes_quality_filter(clip)
        if not passed:
            rule_rejected_count += 1
            logger.debug("Quality filter rejected: %s — %s", clip.get("title", "")[:50], reason)
            continue
        if reason.startswith("AUTO_APPROVE"):
            clip["ai_quality_score"] = 75.0
            clip["ai_analyzed"]      = 0
            auto_approved.append(clip)
        else:
            needs_ai.append(clip)

    ai_approved: List[Dict] = []
    ai_rejected_count = 0
    if needs_ai:
        estimated_cost = len(needs_ai) * 0.001
        print(
            f"\n  Quality filter: {len(auto_approved)} auto | "
            f"{len(needs_ai)} → AI | {rule_rejected_count} rule-rejected"
            f"  (AI est. cost: ${estimated_cost:.3f})"
        )
        ai_approved = batch_analyze_clips(needs_ai)
        ai_rejected_count = len(needs_ai) - len(ai_approved)
        for clip in ai_approved:
            clip["ai_analyzed"] = 1
    else:
        print(
            f"\n  Quality filter: {len(auto_approved)} auto-approved | "
            f"0 → AI | {rule_rejected_count} rule-rejected"
        )

    all_approved = auto_approved + ai_approved
    for clip in all_approved:
        clip["final_score"] = calculate_final_score(clip)

    all_approved.sort(key=lambda c: float(c.get("final_score") or 0), reverse=True)

    total_rejected = language_rejected_count + safety_rejected_count + rule_rejected_count + ai_rejected_count
    print(
        f"  Pool total: {len(all_approved)} approved | "
        f"{total_rejected} rejected "
        f"({language_rejected_count} non-English, {safety_rejected_count} safety, "
        f"{rule_rejected_count} rule, {ai_rejected_count} AI)"
    )
    if all_approved:
        top = all_approved[0]
        print(
            f"  Top clip: [{top.get('final_score', 0):.1f}] "
            f"{top.get('viral_title') or top.get('title','')[:55]}"
        )

    return all_approved


def _normalize_source(clip: Dict) -> Dict:
    """
    Ensure clip['source'] is one of 'twitch', 'youtube', 'kick'.

    Medal, Streamable, and Twitter clips are initially tagged with their
    discovery platform.  This function tries to trace the creator back to
    their primary streaming platform via find_original_source().
    Falls back to 'twitch' if no match is found.
    """
    source = (clip.get("source") or "").lower()
    if source in ("twitch", "youtube", "kick"):
        return clip

    try:
        from viral_discovery import find_original_source
        origin = find_original_source(
            clip.get("title") or "",
            clip.get("viral_title") or "",
            clip.get("creator_name") or "",
        )
        if origin:
            clip["source"] = origin["source"]
            if not clip.get("creator_name"):
                clip["creator_name"] = origin["creator_name"]
            return clip
    except Exception:
        pass

    clip["source"] = "twitch"
    return clip


# ══════════════════════════════════════════════════════════════════════════════
# Viral discovery pool refresh (Reddit / YouTube Shorts / Medal / Streamable / Twitter)
# ══════════════════════════════════════════════════════════════════════════════

def refresh_viral_discovery_pool(
    db_path: Optional[Path] = None,
    max_clips: int = 100,
) -> Dict[str, int]:
    """
    Gather clips from all viral discovery sources, run the two-layer quality
    filter, and insert approved clips into the shared pool.

    Sources (no credentials required):
        - Reddit (r/LivestreamFail and 20+ subreddits)
        - YouTube Shorts (42 search queries + 8 channel scrapers)
        - Medal.tv (trending + TIER1 creator search)
        - Streamable (Google search + yt-dlp metadata)
        - Twitter/X (Nitter search + yt-dlp metadata)

    Args:
        db_path:   Override database path.
        max_clips: Maximum clips from Reddit + YouTube Shorts (default 100).

    Returns:
        Dict with clips_added, reddit_added, youtube_shorts_added,
        medal_added, streamable_added, twitter_added.
    """
    run_id = shared_pool.log_pool_run_start("viral_discovery", db_path=db_path)
    clips_added        = 0
    reddit_added       = 0
    yt_added           = 0
    medal_added        = 0
    streamable_added   = 0
    twitter_added      = 0

    _zero = {
        "clips_added": 0, "reddit_added": 0, "youtube_shorts_added": 0,
        "medal_added": 0, "streamable_added": 0, "twitter_added": 0,
    }

    try:
        from viral_discovery import (
            discover_viral_clips,
            fetch_youtube_gaming_trending,
            fetch_streamable_clips,
            fetch_twitter_clips,
        )

        # ── 1. Reddit + YouTube Shorts ────────────────────────────────────────
        import os as _os
        _on_railway = bool(
            _os.environ.get("RAILWAY_ENVIRONMENT") or _os.environ.get("RAILWAY_PROJECT_ID")
        )
        if _on_railway:
            logger.info(
                "refresh_viral_discovery_pool: Railway detected — skipping Reddit "
                "(v.redd.it blocked); fetching YouTube Shorts + YouTube Gaming + Streamable + Twitter…"
            )
        else:
            logger.info(
                "refresh_viral_discovery_pool: discovering viral clips "
                "(Reddit + YouTube Shorts + YouTube Gaming + Streamable + Twitter)…"
            )
        clips = discover_viral_clips(
            include_youtube_shorts=True,
            include_reddit=not _on_railway,
            max_total=max_clips,
        )
        logger.info(
            "refresh_viral_discovery_pool: Reddit+YT Shorts returned %d clips",
            len(clips),
        )

        # ── 2. YouTube Gaming Trending (replaces Medal.tv) ────────────────────
        try:
            gaming_clips = fetch_youtube_gaming_trending(max_clips=50)
            clips.extend(gaming_clips)
            print(f"  YouTube Gaming Trending: {len(gaming_clips)} clips found")
        except Exception as exc:
            logger.warning("YouTube Gaming Trending fetch error (non-fatal): %s", exc)
            print(f"  YouTube Gaming Trending: error — {exc}")

        # ── 3. Streamable ─────────────────────────────────────────────────────
        try:
            streamable_clips = fetch_streamable_clips()
            clips.extend(streamable_clips)
            print(f"  Streamable: {len(streamable_clips)} clips found")
        except Exception as exc:
            logger.warning("Streamable fetch error (non-fatal): %s", exc)
            print(f"  Streamable: error — {exc}")

        # ── 4. Twitter ────────────────────────────────────────────────────────
        try:
            twitter_clips = fetch_twitter_clips()
            clips.extend(twitter_clips)
            print(f"  Twitter: {len(twitter_clips)} clips found")
        except Exception as exc:
            logger.warning("Twitter fetch error (non-fatal): %s", exc)
            print(f"  Twitter: error — {exc}")

        # ── 5. Normalise sources + deduplicate by URL ─────────────────────────
        seen_urls: set = set()
        unique_clips: List[Dict] = []
        reddit_skipped = 0
        for clip in clips:
            clip = _normalize_source(clip)
            url = clip.get("url", "")
            if _on_railway and "v.redd.it" in url:
                reddit_skipped += 1
                continue
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_clips.append(clip)
            elif not clip.get("clip_id"):
                # generate a fallback clip_id from URL hash
                import hashlib
                h = hashlib.md5(url.encode()).hexdigest()[:12]
                clip["clip_id"] = f"viral_url_{h}"

        if reddit_skipped:
            logger.info(
                "refresh_viral_discovery_pool: skipped %d v.redd.it URL(s) (Railway — 403 blocked)",
                reddit_skipped,
            )
        logger.info(
            "refresh_viral_discovery_pool: %d unique clips after deduplication, "
            "running quality filter…",
            len(unique_clips),
        )

        # ── 6. Two-layer quality filter ───────────────────────────────────────
        approved = filter_and_score_clips(unique_clips)

        # ── 7. Insert into pool ───────────────────────────────────────────────
        for clip in approved:
            try:
                if shared_pool.add_clip_to_pool(clip, db_path=db_path):
                    clips_added += 1
                    dsrc = clip.get("discovery_source", "")
                    if dsrc == "reddit_trending":
                        reddit_added += 1
                    elif dsrc == "youtube_shorts_trending":
                        yt_added += 1
                    elif dsrc == "medal_trending":
                        medal_added += 1
                    elif dsrc == "streamable_trending":
                        streamable_added += 1
                    elif dsrc == "twitter_trending":
                        twitter_added += 1
                    logger.debug(
                        "  + [%s/%s] %s (final=%.1f)",
                        dsrc,
                        clip.get("creator_name", "?"),
                        clip.get("title", "")[:50],
                        clip.get("final_score", 0),
                    )
            except Exception as exc:
                logger.debug(
                    "refresh_viral_discovery_pool: error inserting clip: %s", exc
                )

        logger.info(
            "refresh_viral_discovery_pool: complete — %d new clip(s) "
            "(reddit=%d, yt=%d, medal=%d, streamable=%d, twitter=%d).",
            clips_added, reddit_added, yt_added,
            medal_added, streamable_added, twitter_added,
        )
        shared_pool.complete_pool_run(
            run_id, clips_added=clips_added, clips_expired=0,
            status="completed", db_path=db_path,
        )
        return {
            "clips_added":        clips_added,
            "reddit_added":       reddit_added,
            "youtube_shorts_added": yt_added,
            "medal_added":        medal_added,
            "streamable_added":   streamable_added,
            "twitter_added":      twitter_added,
        }

    except Exception as exc:
        logger.error(
            "refresh_viral_discovery_pool: unexpected error: %s", exc, exc_info=True
        )
        shared_pool.complete_pool_run(
            run_id, clips_added=clips_added, clips_expired=0,
            status="error", db_path=db_path,
        )
        return _zero


# ══════════════════════════════════════════════════════════════════════════════
# Combined pool refresh
# ══════════════════════════════════════════════════════════════════════════════

def refresh_all_pools(
    user_config: Dict[str, Any],
    user_prefs: Optional[Dict] = None,
    db_path: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run a full shared pool refresh cycle for both Twitch and YouTube.

    Checks pool freshness first — if the pool was refreshed within
    pool_refresh_interval_hours (default 6), this is a no-op unless
    force=True.

    Steps:
        1. Check freshness (skip if fresh and not forced).
        2. Ensure shared pool tables exist.
        3. Expire stale clips and reservations.
        4. Refresh Twitch pool (TIER1_TWITCH whitelist only).
        5. Refresh YouTube pool (TIER1_YOUTUBE whitelist only).
        6. Refresh Kick pool (TIER1_KICK whitelist only).
        7. Log a summary and return result dict.

    This is the primary entry point for the scheduler. It is safe to call
    even when credentials are not configured — each fetcher returns 0 silently.

    Args:
        user_config: Dict loaded from config.yaml (API credentials).
        user_prefs:  Dict loaded from preferences.yaml (optional, used for
                     target_channels extraction for YouTube Method 3).
        db_path:     Override database path (useful for tests).
        force:       If True, skip freshness check and always refresh.

    Returns:
        Dict with keys:
            "skipped"               (bool) — True if the pool was already fresh.
            "twitch_added"          (int)  — New Twitch clips added.
            "youtube_added"         (int)  — New YouTube clips added.
            "kick_added"            (int)  — New Kick clips added.
            "viral_discovery_added" (int)  — New viral-discovery clips added.
            "total_added"           (int)  — Total new clips added.
            "expired"               (int)  — Clips removed by expiry.
            "total_pool"            (int)  — Active clips in pool after refresh.
            "duration_sec"          (float)— Wall-clock time in seconds.
    """
    import time

    refresh_hours = int((user_prefs or {}).get("pool_refresh_interval_hours", 6))

    # 1. Freshness check
    if not force and check_pool_freshness(hours=refresh_hours):
        logger.info(
            "Shared pool is fresh (refreshed within %d hours) — skipping.",
            refresh_hours,
        )
        return {
            "skipped":                True,
            "twitch_added":           0,
            "global_clips_added":     0,
            "category_clips_added":   0,
            "youtube_added":          0,
            "kick_added":             0,
            "viral_discovery_added":  0,
            "reddit_added":           0,
            "youtube_gaming_added":   0,
            "streamable_added":       0,
            "twitter_added":          0,
            "total_added":            0,
            "expired":                0,
            "total_pool":             shared_pool.get_pool_stats(db_path=db_path).get("total_clips", 0),
            "duration_sec":           0.0,
        }

    t_start = time.time()

    # 2. Ensure tables exist
    shared_pool.initialize_shared_pool_tables(db_path=db_path)

    # 3. Expire stale clips
    expired = shared_pool.expire_old_clips(db_path=db_path)

    # 4. Twitch (whitelist + global + category)
    twitch_enabled = (user_prefs or {}).get("twitch_enabled", True)
    twitch_added        = 0
    global_clips_added  = 0
    category_clips_added = 0
    if twitch_enabled:
        twitch_result = refresh_twitch_pool(user_config, db_path=db_path)
        if isinstance(twitch_result, dict):
            twitch_added         = twitch_result.get("twitch_added", 0)
            global_clips_added   = twitch_result.get("global_clips_added", 0)
            category_clips_added = twitch_result.get("category_clips_added", 0)
        else:
            twitch_added = int(twitch_result)
    else:
        logger.info("Twitch fetching disabled (twitch_enabled=false) — skipping.")

    # 5. YouTube (whitelist-only: TIER1_YOUTUBE)
    youtube_enabled = (user_prefs or {}).get("youtube_enabled", True)
    youtube_added = 0
    if youtube_enabled:
        youtube_added = refresh_youtube_pool(user_config, db_path=db_path)
    else:
        logger.info("YouTube fetching disabled (youtube_enabled=false) — skipping.")

    # 7. Kick (respects kick_enabled preference — no credentials needed)
    kick_enabled = (user_prefs or {}).get("kick_enabled", True)
    kick_added = 0
    if kick_enabled:
        kick_added = refresh_kick_pool(db_path=db_path)
    else:
        logger.info("Kick fetching disabled (kick_enabled=false) — skipping.")

    # 8. Viral discovery (Reddit + YouTube Shorts — no credentials needed)
    viral_result = {"clips_added": 0, "reddit_added": 0, "youtube_shorts_added": 0}
    try:
        viral_result = refresh_viral_discovery_pool(db_path=db_path)
    except Exception as exc:
        logger.warning("refresh_viral_discovery_pool failed (non-fatal): %s", exc)

    viral_added      = viral_result.get("clips_added", 0)
    reddit_added     = viral_result.get("reddit_added", 0)
    medal_added      = viral_result.get("medal_added", 0)
    streamable_added = viral_result.get("streamable_added", 0)
    twitter_added    = viral_result.get("twitter_added", 0)

    # 9. Summary
    stats      = shared_pool.get_pool_stats(db_path=db_path)
    total_pool = stats.get("total_clips", 0)
    total      = (
        twitch_added + global_clips_added + category_clips_added
        + youtube_added + kick_added + viral_added
    )
    duration   = round(time.time() - t_start, 1)

    logger.info(
        "refresh_all_pools complete in %.1fs: "
        "twitch=%d global=%d category=%d youtube=%d kick=%d "
        "viral=%d (reddit=%d gaming=%d streamable=%d twitter=%d) "
        "expired=%d total_pool=%d",
        duration,
        twitch_added, global_clips_added, category_clips_added,
        youtube_added, kick_added,
        viral_added, reddit_added, medal_added, streamable_added, twitter_added,
        expired, total_pool,
    )

    return {
        "skipped":                False,
        "twitch_added":           twitch_added,
        "global_clips_added":     global_clips_added,
        "category_clips_added":   category_clips_added,
        "youtube_added":          youtube_added,
        "kick_added":             kick_added,
        "viral_discovery_added":  viral_added,
        "reddit_added":           reddit_added,
        "youtube_gaming_added":   medal_added,   # medal_added key reused for backward compat
        "streamable_added":       streamable_added,
        "twitter_added":          twitter_added,
        "total_added":            total,
        "expired":                expired,
        "total_pool":             total_pool,
        "duration_sec":           duration,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Freshness delegation
# ══════════════════════════════════════════════════════════════════════════════

def check_pool_freshness(hours: int = 6, db_path: Optional[Path] = None) -> bool:
    """
    Return True if the shared pool was successfully refreshed within the last
    `hours` hours.

    Delegates to shared_pool.check_pool_freshness().

    Args:
        hours:   Look-back window in hours (default 6).
        db_path: Override database path (useful for tests).

    Returns:
        True if the pool is considered fresh; False if a refresh is needed.
    """
    return shared_pool.check_pool_freshness(hours=hours, db_path=db_path)


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    print("=" * 60)
    print("pool_fetcher.py  —  self-test")
    print("=" * 60)

    # Use a temp DB to avoid touching clipcast.db
    tmp_db = Path(tempfile.mktemp(suffix="_pool_fetcher_test.db"))
    print(f"\nUsing temp DB: {tmp_db}\n")

    # Unconfigured credentials (placeholder values) — all fetches should
    # return 0 gracefully without raising any exceptions.
    fake_config: Dict[str, Any] = {
        "twitch": {
            "client_id":     "YOUR_TWITCH_CLIENT_ID_HERE",
            "client_secret": "YOUR_TWITCH_CLIENT_SECRET_HERE",
        },
        "youtube": {
            "api_key": "YOUR_YOUTUBE_DATA_API_KEY_HERE",
        },
    }
    fake_prefs: Dict[str, Any] = {
        "target_youtube_channels": ["@SomeChannel"],
        "target_games": [],
        "minimum_views": 0,
    }

    try:
        # 1. Initialize base + pool tables
        print("1. Initializing database tables...")
        database.initialize_database(db_path=tmp_db)
        shared_pool.initialize_shared_pool_tables(db_path=tmp_db)
        print("   OK")

        # 2. check_pool_freshness before any run
        print("\n2. check_pool_freshness() before any run...")
        fresh = check_pool_freshness(hours=6, db_path=tmp_db)
        print(f"   Fresh: {fresh}  (expected False)")
        assert not fresh, "Pool should not be fresh before any run"

        # 3. refresh_all_pools with unconfigured credentials
        print("\n3. refresh_all_pools() with unconfigured credentials...")
        result = refresh_all_pools(
            user_config=fake_config,
            user_prefs=fake_prefs,
            db_path=tmp_db,
        )
        print(f"   Result: {result}")
        print(f"   twitch_added:  {result['twitch_added']}  (expected 0)")
        print(f"   youtube_added: {result['youtube_added']}  (expected 0)")
        print(f"   expired:       {result['expired']}")
        print(f"   total_pool:    {result['total_pool']}")
        assert result["twitch_added"] == 0, "Unconfigured Twitch should return 0"
        assert result["youtube_added"] == 0, "Unconfigured YouTube should return 0"
        print("   Graceful (no crash) — OK")

        # 4. Manually log a completed run and verify freshness
        print("\n4. Simulating a completed pool run...")
        run_id = shared_pool.log_pool_run_start("twitch", db_path=tmp_db)
        shared_pool.complete_pool_run(
            run_id,
            clips_added=10,
            clips_expired=2,
            status="completed",
            db_path=tmp_db,
        )
        fresh_now = check_pool_freshness(hours=6, db_path=tmp_db)
        print(f"   Fresh after simulated run: {fresh_now}  (expected True)")
        assert fresh_now, "Pool should be fresh after a completed run"

        # 5. Pool stats after simulated run
        print("\n5. Pool stats after simulated run...")
        stats = shared_pool.get_pool_stats(db_path=tmp_db)
        print(f"   total_clips:  {stats['total_clips']}  (expected 0 — no real clips added)")
        print(f"   last_runs:    {len(stats['last_runs'])} run(s) logged")
        latest = stats["last_runs"][0] if stats["last_runs"] else {}
        print(
            f"   Latest run:   status={latest.get('status')} "
            f"added={latest.get('clips_added')}"
        )
        assert len(stats["last_runs"]) >= 1, "Expected at least one run logged"

        # 6. _get_quota_level — should not crash
        print("\n6. Testing _get_quota_level()...")
        level = _get_quota_level()
        print(f"   Quota level: '{level}'  (expected 'ok', 'warn', or 'critical')")
        assert level in ("ok", "warn", "critical"), f"Unexpected level: {level}"

        print("\n" + "=" * 60)
        print("All pool_fetcher.py tests PASSED.")
        print("=" * 60)

    except AssertionError as ae:
        print(f"\nASSERTION FAILED: {ae}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\nUNEXPECTED ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if tmp_db.exists():
            tmp_db.unlink()
            print(f"\nTemp DB cleaned up: {tmp_db}")
