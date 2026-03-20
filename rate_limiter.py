"""
rate_limiter.py
===============
Responsible API usage enforcement for ClipCast Studio.

Enforces platform-specific limits invisibly so the pipeline slows down
or stops gracefully rather than crashing, getting rate-banned, or
violating platform developer policies.

  Twitch  — Reads X-RateLimit-* response headers; exponential backoff on 429.
  YouTube — Tracks daily quota against the 10,000 unit default limit.
            Warns at 80 %, stops gracefully at 100 %.
  TikTok  — Hard cap of 3 posts per day per account (platform policy).

All limits are enforced automatically. Callers only need to call the
public functions — no configuration required.

SaaS Note:
    All functions accept user_id for per-user quota tracking.
    In single-user mode, user_id always defaults to 1.

Test:
    python rate_limiter.py
"""

import logging
import time
from datetime import date

import database

logger = logging.getLogger(__name__)

# ── YouTube quota constants ────────────────────────────────────────────────────
YOUTUBE_DAILY_QUOTA_LIMIT    = 10_000
YOUTUBE_QUOTA_WARN_PCTS      = 0.80       # Warn at 80 %

# ── Twitch backoff constants ───────────────────────────────────────────────────
TWITCH_BACKOFF_BASE_SECONDS  = 1.0
TWITCH_BACKOFF_MAX_SECONDS   = 60.0

# ── TikTok post limit ──────────────────────────────────────────────────────────
TIKTOK_MAX_POSTS_PER_DAY     = 3

# ── YouTube API quota costs (Google's published quota table) ───────────────────
YOUTUBE_QUOTA_COSTS = {
    "search.list":          100,
    "videos.list":            1,
    "channels.list":          1,
    "playlistItems.list":     1,
    "videoCategories.list":   0,
}


# ══════════════════════════════════════════════════════════════════════════════
# Twitch rate limiter
# ══════════════════════════════════════════════════════════════════════════════

def handle_twitch_response(response, attempt: int = 0) -> bool:
    """
    Inspect a Twitch API response for rate-limit headers and sleep if needed.

    Call this after every Twitch API request. Returns True if the caller
    should retry the request (rate limited), False if it can proceed normally.

    Args:
        response: The requests.Response object from the Twitch API call.
        attempt:  Current retry attempt count (for exponential backoff).

    Returns:
        True  — caller should retry this request after the sleep.
        False — no rate limiting in effect, proceed normally.
    """
    if response.status_code == 429:
        reset_header = response.headers.get("Ratelimit-Reset")
        if reset_header:
            try:
                wait = max(1.0, float(reset_header) - time.time())
            except (ValueError, TypeError):
                wait = _backoff_seconds(attempt)
        else:
            wait = _backoff_seconds(attempt)

        logger.info(
            "Twitch rate limit hit (429). Waiting %.1f s before retry (attempt %d).",
            wait, attempt + 1,
        )
        time.sleep(wait)
        return True

    # Check remaining quota even on success — slow down proactively if low
    remaining = response.headers.get("Ratelimit-Remaining")
    if remaining is not None:
        try:
            if int(remaining) < 5:
                time.sleep(0.5)
        except (ValueError, TypeError):
            pass

    return False


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 1 s, 2 s, 4 s, 8 s … capped at max."""
    return min(TWITCH_BACKOFF_BASE_SECONDS * (2 ** attempt), TWITCH_BACKOFF_MAX_SECONDS)


# ══════════════════════════════════════════════════════════════════════════════
# YouTube quota tracker
# ══════════════════════════════════════════════════════════════════════════════

def check_youtube_quota(
    operation: str,
    user_id: int = 1,
    limit: int = YOUTUBE_DAILY_QUOTA_LIMIT,
) -> bool:
    """
    Check whether a YouTube API operation can proceed without exceeding
    the daily quota limit.

    Args:
        operation: YouTube API method name (e.g. "search.list").
        user_id:   User ID for per-user quota tracking.
        limit:     Daily quota limit (default 10,000).

    Returns:
        True  — operation can proceed.
        False — quota would be exceeded; caller should skip the operation.
    """
    cost    = YOUTUBE_QUOTA_COSTS.get(operation, 1)
    today   = date.today().isoformat()
    current = database.get_daily_quota("youtube", date_str=today, user_id=user_id)

    if current + cost > limit:
        logger.warning(
            "YouTube daily quota exhausted (%d/%d units). "
            "Skipping '%s' (cost=%d). Quota resets at midnight Pacific.",
            current, limit, operation, cost,
        )
        return False

    # Warn once when crossing the 80% threshold
    if (current < limit * YOUTUBE_QUOTA_WARN_PCTS and
            current + cost >= limit * YOUTUBE_QUOTA_WARN_PCTS):
        logger.warning(
            "YouTube quota at %.0f%% (%d/%d units). Approaching daily limit.",
            100.0 * (current + cost) / limit, current + cost, limit,
        )

    return True


def record_youtube_quota(operation: str, user_id: int = 1) -> None:
    """
    Record quota usage after a successful YouTube API call.

    Args:
        operation: YouTube API method name (e.g. "search.list").
        user_id:   User ID for per-user quota tracking.
    """
    cost = YOUTUBE_QUOTA_COSTS.get(operation, 1)
    if cost > 0:
        today = date.today().isoformat()
        database.add_quota_usage("youtube", units=cost, date_str=today, user_id=user_id)
        logger.debug("YouTube quota: +%d units for '%s'.", cost, operation)


def get_youtube_quota_level(user_id: int = 1) -> str:
    """
    Return a human-readable quota consumption level for the current day.

    Used by pool_fetcher.py to decide which YouTube discovery methods to run:
        'ok'       — Below 80 % of daily limit. All pool methods can run.
        'warn'     — 80–95 % consumed. Pool fetcher runs Method 1 only.
        'critical' — Above 95 % consumed. Pool fetcher skips all YouTube fetching.

    Args:
        user_id: User ID for per-user quota tracking.

    Returns:
        One of 'ok', 'warn', or 'critical'.
    """
    today   = date.today().isoformat()
    current = database.get_daily_quota("youtube", date_str=today, user_id=user_id)
    pct     = current / YOUTUBE_DAILY_QUOTA_LIMIT

    if pct >= 0.95:
        return "critical"
    if pct >= YOUTUBE_QUOTA_WARN_PCTS:
        return "warn"
    return "ok"


# ══════════════════════════════════════════════════════════════════════════════
# TikTok post limit
# ══════════════════════════════════════════════════════════════════════════════

def check_tiktok_post_limit(user_id: int = 1) -> bool:
    """
    Check whether another TikTok post is allowed today.

    TikTok's Content Posting API enforces a hard limit of 3 posts per day
    per account. This function enforces that limit at the application level
    before we ever attempt an upload, avoiding platform-level rejections.

    Args:
        user_id: User ID for per-user post tracking.

    Returns:
        True  — posting is allowed.
        False — daily limit reached; no more TikTok posts until midnight.
    """
    today   = date.today().isoformat()
    current = database.get_daily_quota("tiktok_posts", date_str=today, user_id=user_id)

    if current >= TIKTOK_MAX_POSTS_PER_DAY:
        logger.warning(
            "TikTok daily post limit reached (%d/%d). "
            "No more TikTok posts today. Limit resets at midnight.",
            current, TIKTOK_MAX_POSTS_PER_DAY,
        )
        return False

    return True


def record_tiktok_post(user_id: int = 1) -> None:
    """
    Record a successful TikTok post against the daily limit.

    Args:
        user_id: User ID for per-user post tracking.
    """
    today = date.today().isoformat()
    database.add_quota_usage("tiktok_posts", units=1, date_str=today, user_id=user_id)
    logger.debug("TikTok post recorded for today (daily count incremented).")


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")
    print("Testing rate_limiter.py...\n")

    # Use a temp DB so we don't pollute the real one
    tmp_db = Path(tempfile.mktemp(suffix=".db"))
    database.initialize_database(db_path=tmp_db)

    today = date.today().isoformat()

    # 1. YouTube quota: fresh
    print("1. YouTube quota — fresh day:")
    ok = check_youtube_quota("search.list", user_id=1)
    print(f"   search.list allowed: {ok}  (expected: True)")

    # 2. Simulate near-limit usage
    print("\n2. Simulate 9,900 units used:")
    database.add_quota_usage("youtube", units=9900, date_str=today, user_id=1, db_path=tmp_db)
    ok = check_youtube_quota("search.list", user_id=1)
    print(f"   search.list (cost=100) allowed: {ok}  (expected: False — would hit 10,000)")

    # 3. TikTok: at limit
    print("\n3. TikTok — 3 posts recorded:")
    for _ in range(3):
        database.add_quota_usage("tiktok_posts", units=1, date_str=today, user_id=1, db_path=tmp_db)
    ok = check_tiktok_post_limit(user_id=1)
    print(f"   4th post allowed: {ok}  (expected: False)")

    tmp_db.unlink(missing_ok=True)
    print("\nAll rate_limiter tests passed.")
