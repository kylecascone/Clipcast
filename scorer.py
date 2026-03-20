"""
scorer.py
=========
Calculates a quality score (0–100) for each clip to determine which clips
get included in compilations and in what order.

Scoring components:
  - View velocity     : Views per hour since clip was created (40%).
  - Duration match    : How well the clip duration fits the preferred range (35%).
  - Audio energy      : Peak loudness (LUFS) measured by FFmpeg (25%).
  - Target bonus      : Extra points for clips from configured target streamers.
  - Manual override   : Manual clips always receive 100.0 (maximum priority).

The scorer also tags each clip as:
  - solo_worthy       : True if the clip is long and high-quality enough to post alone.
  - pairing_candidate : True if the clip works well as part of a compilation.
"""

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from preferences import get_clip_length_range

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Scoring weights (sum to 1.0)
#   View velocity   40%
#   Duration match  35%
#   Audio energy    25%
# ══════════════════════════════════════════════════════════════════════════════

WEIGHT_VIEW_VELOCITY  = 0.40
WEIGHT_DURATION_MATCH = 0.35
WEIGHT_AUDIO_ENERGY   = 0.25

# Bonus applied to clips from configured target streamers (additive, out of 100)
TARGET_STREAMER_BONUS   = 10.0

# Bonus for Tier 1 viral creators (replaces target bonus when higher)
TIER1_CREATOR_BONUS     = 30.0

# Bonus when clip title contains 2+ viral signal words
VIRAL_TITLE_BONUS       = 15.0

# Bonus for YouTube clips with strong like-to-view engagement (>= 5%)
YOUTUBE_ENGAGEMENT_BONUS = 10.0

# View velocity thresholds (views per hour)
# A clip at or above MAX_VPH gets a perfect view-velocity sub-score.
VIEW_VELOCITY_MIN_VPH   = 0.0
VIEW_VELOCITY_MAX_VPH   = 5000.0

# Audio energy (LUFS) thresholds — FFmpeg loudnorm integrated loudness
# Louder (closer to 0 LUFS) = more energy.
# Typical speech is around -23 LUFS; exciting gaming clips peak around -14 LUFS.
AUDIO_ENERGY_MIN_LUFS   = -40.0  # Very quiet — low score
AUDIO_ENERGY_MAX_LUFS   = -8.0   # Very loud — high score

# A clip is tagged solo_worthy if its score is above this threshold
SOLO_WORTHY_THRESHOLD   = 60.0

# A clip is tagged pairing_candidate if its score is above this threshold
# (lower bar — works in compilations but may not be strong enough alone)
PAIRING_CANDIDATE_THRESHOLD = 35.0


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def score_clip(
    clip: Dict[str, Any],
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
) -> Dict[str, Any]:
    """
    Calculate the quality score for a single clip and update it in-place.

    Adds the following keys to the clip dict:
        score           (float)  — Overall score 0–100.
        is_solo_worthy  (bool)   — True if the clip can stand alone as a post.
        pairing_candidate (bool) — True if the clip works well in a compilation.
        score_breakdown (dict)   — Sub-scores for debugging/transparency.

    Args:
        clip: Clip dict (output from a fetcher or database.get_clip()).
        user_prefs: User preferences. If None, loaded from preferences.yaml.
        user_id: Reserved for SaaS. Currently unused.

    Returns:
        The clip dict with score fields added/updated.
    """
    # Manual clips always get maximum score
    if clip.get("mode") == "manual" or clip.get("source") == "manual":
        clip["score"] = 100.0
        clip["is_solo_worthy"] = True
        clip["pairing_candidate"] = True
        clip["score_breakdown"] = {"manual_override": 100.0}
        logger.debug("Clip '%s' is manual — assigned score 100.0", clip.get("title"))
        return clip

    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    # ── Sub-score 1: View velocity ─────────────────────────────────────────────
    velocity_score = _score_view_velocity(clip)

    # ── Sub-score 2: Duration match ────────────────────────────────────────────
    duration_score = _score_duration_match(
        clip.get("duration"), user_prefs.get("clip_length", "medium")
    )

    # ── Sub-score 3: Audio energy ──────────────────────────────────────────────
    audio_score = _score_audio_energy(clip.get("local_path"))

    # ── Weighted total ─────────────────────────────────────────────────────────
    raw_score = (
        velocity_score * WEIGHT_VIEW_VELOCITY  * 100 +
        duration_score * WEIGHT_DURATION_MATCH * 100 +
        audio_score    * WEIGHT_AUDIO_ENERGY   * 100
    )

    # ── Creator bonuses ────────────────────────────────────────────────────────
    target_streamers = [s.lower() for s in user_prefs.get("target_streamers", [])]
    target_channels  = [c.lower() for c in user_prefs.get("target_youtube_channels", [])]
    creator = (clip.get("creator_name") or "").lower()

    is_target = (
        creator in target_streamers or
        any(creator in ch for ch in target_channels)
    )

    # Tier 1 viral creator bonus (takes precedence over generic target bonus)
    try:
        from viral_creators import is_tier1_creator
        is_tier1 = is_tier1_creator(creator)
    except ImportError:
        is_tier1 = False

    if is_tier1:
        creator_bonus = TIER1_CREATOR_BONUS
    elif is_target:
        creator_bonus = TARGET_STREAMER_BONUS
    else:
        creator_bonus = 0.0
    raw_score += creator_bonus

    # ── Viral title signals bonus ──────────────────────────────────────────────
    title_lower = (clip.get("title") or "").lower()
    try:
        from viral_creators import VIRAL_TITLE_SIGNALS
        matched_signals = sum(1 for sig in VIRAL_TITLE_SIGNALS if sig in title_lower)
    except ImportError:
        matched_signals = 0
    title_bonus = VIRAL_TITLE_BONUS if matched_signals >= 2 else 0.0
    raw_score += title_bonus

    # ── YouTube engagement bonus (like-to-view ratio >= 5%) ───────────────────
    engagement_bonus = 0.0
    if clip.get("source") == "youtube":
        like_count  = int(clip.get("like_count") or 0)
        view_count  = int(clip.get("view_count") or 0)
        if view_count > 0 and like_count > 0:
            if (like_count / view_count) >= 0.05:
                engagement_bonus = YOUTUBE_ENGAGEMENT_BONUS
    raw_score += engagement_bonus

    # Clamp to [0, 100]
    final_score = max(0.0, min(100.0, raw_score))

    clip["score"] = round(final_score, 2)
    clip["is_solo_worthy"] = final_score >= SOLO_WORTHY_THRESHOLD
    clip["pairing_candidate"] = final_score >= PAIRING_CANDIDATE_THRESHOLD
    clip["score_breakdown"] = {
        "view_velocity":     round(velocity_score * WEIGHT_VIEW_VELOCITY  * 100, 2),
        "duration_match":    round(duration_score * WEIGHT_DURATION_MATCH * 100, 2),
        "audio_energy":      round(audio_score    * WEIGHT_AUDIO_ENERGY   * 100, 2),
        "creator_bonus":     creator_bonus,
        "title_bonus":       title_bonus,
        "engagement_bonus":  engagement_bonus,
        "final":             clip["score"],
    }

    logger.debug(
        "Scored '%s': %.1f  (vel=%.2f dur=%.2f audio=%.2f "
        "creator=%.0f title=%.0f engage=%.0f)",
        clip.get("title", "")[:40],
        final_score,
        velocity_score,
        duration_score,
        audio_score,
        creator_bonus,
        title_bonus,
        engagement_bonus,
    )
    return clip


def score_clips(
    clips: List[Dict[str, Any]],
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
) -> List[Dict[str, Any]]:
    """
    Score a list of clips and return them sorted by score descending.

    Args:
        clips: List of clip dicts.
        user_prefs: User preferences.
        user_id: Reserved for SaaS.

    Returns:
        The same list, each clip updated with score fields, sorted best-first.
    """
    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    for clip in clips:
        score_clip(clip, user_prefs=user_prefs, user_id=user_id)

    clips.sort(key=lambda c: c.get("score", 0), reverse=True)
    logger.info(
        "Scored %d clip(s). Top score: %.1f | Bottom: %.1f",
        len(clips),
        clips[0]["score"] if clips else 0,
        clips[-1]["score"] if clips else 0,
    )
    return clips


def filter_by_min_score(
    clips: List[Dict[str, Any]],
    min_score: float,
) -> tuple[List[Dict], List[Dict]]:
    """
    Split clips into (passed, skipped) lists based on minimum score.

    Args:
        clips: Scored clip dicts.
        min_score: Minimum score threshold.

    Returns:
        Tuple of (clips_above_threshold, clips_below_threshold).
    """
    passed  = [c for c in clips if c.get("score", 0) >= min_score]
    skipped = [c for c in clips if c.get("score", 0) < min_score]
    logger.info(
        "Score filter (min=%.1f): %d passed, %d skipped.",
        min_score, len(passed), len(skipped),
    )
    return passed, skipped


# ══════════════════════════════════════════════════════════════════════════════
# Sub-score functions (private)
# ══════════════════════════════════════════════════════════════════════════════

def _score_view_velocity(clip: Dict[str, Any]) -> float:
    """
    Returns a 0–1 score based on how fast the clip is accumulating views.

    Views-per-hour = view_count / hours_since_created.
    Normalized between VIEW_VELOCITY_MIN_VPH and VIEW_VELOCITY_MAX_VPH.
    """
    view_count = clip.get("view_count") or 0
    created_at = clip.get("created_at")

    if not view_count:
        return 0.0

    # Calculate hours since clip was created
    hours_old = 24.0  # Default to 24 hours if we can't parse the timestamp
    if created_at:
        try:
            # Handle both Z suffix and +00:00 offset formats
            ts_str = str(created_at).replace("Z", "+00:00")
            created_dt = datetime.fromisoformat(ts_str)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - created_dt
            hours_old = max(delta.total_seconds() / 3600, 0.1)  # Avoid div-by-zero
        except (ValueError, TypeError):
            logger.debug("Could not parse created_at: '%s'. Using 24h default.", created_at)

    views_per_hour = view_count / hours_old

    # Normalize to 0–1
    score = (views_per_hour - VIEW_VELOCITY_MIN_VPH) / (VIEW_VELOCITY_MAX_VPH - VIEW_VELOCITY_MIN_VPH)
    return max(0.0, min(1.0, score))


def _score_duration_match(duration_sec: Optional[float], clip_length_pref: str) -> float:
    """
    Returns a 0–1 score based on how well the clip duration fits the preferred range.

    Perfect score if duration is within the target range.
    Score degrades linearly as duration moves outside the range.
    """
    if duration_sec is None:
        return 0.5  # Unknown duration — neutral score

    min_sec, max_sec = get_clip_length_range(clip_length_pref)
    target_mid = (min_sec + max_sec) / 2

    if min_sec <= duration_sec <= max_sec:
        return 1.0  # Perfect match

    # How far outside the range is the clip?
    if duration_sec < min_sec:
        distance = min_sec - duration_sec
        tolerance = min_sec  # 100% penalty at 0 seconds
    else:
        distance = duration_sec - max_sec
        tolerance = max_sec  # 100% penalty at 2× max_sec

    score = max(0.0, 1.0 - (distance / tolerance))
    return score


def _score_audio_energy(local_path: Optional[str]) -> float:
    """
    Measure integrated audio loudness using FFmpeg's loudnorm filter.
    Returns a 0–1 score where 1.0 = maximum energy.

    If the file has not been downloaded yet (local_path is None), returns
    a neutral score of 0.5.

    Uses FFmpeg's ebur128 audio filter for LUFS measurement.
    Requires FFmpeg to be installed and accessible on PATH.
    """
    if not local_path:
        return 0.5  # No local file yet — neutral score

    from pathlib import Path
    if not Path(local_path).exists():
        logger.debug("Audio score: file not found at %s, using neutral.", local_path)
        return 0.5

    try:
        # Run FFmpeg loudnorm analysis (outputs JSON to stderr)
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", local_path,
                "-af", "loudnorm=print_format=json",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Parse the JSON block from stderr
        stderr = result.stderr
        json_start = stderr.rfind("{")
        json_end   = stderr.rfind("}") + 1

        if json_start == -1 or json_end == 0:
            logger.debug("FFmpeg loudnorm: no JSON found in output. Using neutral score.")
            return 0.5

        loudnorm_data = json.loads(stderr[json_start:json_end])
        input_i = float(loudnorm_data.get("input_i", AUDIO_ENERGY_MIN_LUFS))

        # Normalize LUFS to 0–1 (clamped)
        score = (input_i - AUDIO_ENERGY_MIN_LUFS) / (AUDIO_ENERGY_MAX_LUFS - AUDIO_ENERGY_MIN_LUFS)
        return max(0.0, min(1.0, score))

    except FileNotFoundError:
        logger.warning(
            "FFmpeg not found. Install FFmpeg to enable audio energy scoring. "
            "Using neutral score (0.5). Install with:  brew install ffmpeg"
        )
        return 0.5
    except subprocess.TimeoutExpired:
        logger.warning("FFmpeg audio analysis timed out for '%s'. Using neutral score.", local_path)
        return 0.5
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.debug("Audio energy parse error: %s. Using neutral score.", e)
        return 0.5


# ══════════════════════════════════════════════════════════════════════════════
# Two-layer quality filter
# ══════════════════════════════════════════════════════════════════════════════

# Tier-1 creator names used for auto-approval signal detection.
_TIER1_NAMES = [
    "xqc", "kaicenat", "kai_cenat", "ishowspeed", "adinross",
    "jynxzi", "shroud", "nickmercs", "caseoh", "moistcr1tikal",
    "pokimane", "hasanabi", "ludwig", "mizkif", "trainwreck",
    "n3on", "jidion", "sneako", "emiru", "forsen",
    "summit1g", "sodapoppin", "timthetatman", "nmplol",
    # YouTube mega-creators
    "mrbeast", "dream", "tommyinnit", "georgenotfound",
    "tubbo", "quackity", "sapnap", "ranboo",
    "jacksepticeye", "markiplier", "pewdiepie",
    # Additional streamers
    "hasan", "amouranth", "caseoh", "neon", "fanum",
]

# Viral signal words used for positive-signal detection.
_VIRAL_WORDS = [
    "loses it", "freaks out", "insane", "unbelievable", "wtf",
    "crazy", "funny", "hilarious", "shocking", "incredible",
    "viral", "banned", "exposed", "reacts", "reaction",
    "world record", "first time", "never seen", "caught",
    "falls", "fails", "wins", "clutch", "rage", "crying",
    "breaks down", "goes off", "cant believe", "unexpected",
    "walked out", "surprise", "donated", "swatted", "proposed",
    # Extended viral vocabulary
    "knockout", "amazing", "impossible", "record", "hacked",
    "glitch", "bug", "speed", "moments", "best of", "top",
    "legendary", "epic", "destroyed", "humiliated", "speechless",
    "no way", "unreal", "absurd", "extreme", "insane play",
    "career ending", "best play", "worst ever", "first ever",
    "comeback", "choke", "throws", "trolled", "one shot",
]

# Patterns that suggest a title is context-dependent and not self-contained.
_CONTEXT_PATTERNS = [
    r"^\w+\s*$",           # Single bare word
    r"^clip\s*\d*$",       # Just "clip" or "clip123"
    r"\bday \d+\b",
    r"\bstream \d+\b",
    r"\bepisode \d+\b",
    r"\bpart \d+\b",
    r"^\?\?\?",
    r"^(lol|lmao|xd)\s*$",
]

# Words that suggest a transcript starts mid-sentence.
_MID_SENTENCE_STARTERS = [
    "and", "but", "so", "because", "then", "also",
    "or", "yet", "though", "although", "however",
]

# Discovery sources that count as positive viral signals.
_VIRAL_DISCOVERY_SOURCES = {
    "reddit_trending", "tiktok_trending", "twitter_trending",
    "youtube_shorts_trending", "youtube_gaming_trending",
    "streamable_trending", "twitch_api",
}

# Source base scores for calculate_viral_potential_score.
_SOURCE_BASE_SCORES: Dict[str, float] = {
    "reddit_trending":            60.0,
    "tiktok_trending":            55.0,
    "youtube_shorts_trending":    50.0,
    "twitter_trending":           45.0,
    "youtube_gaming_trending":    42.0,
    "streamable_trending":        35.0,
    "twitch_api":                 25.0,
    "youtube_api":                20.0,
    "direct_api":                 20.0,
    "kick_api":                   15.0,
}


def content_safety_filter(clip: Dict[str, Any]) -> bool:
    """
    Block content that could get the account banned or cause legal/PR issues.

    Returns True (safe to continue) or False (reject immediately).
    Runs before any other filter — unsafe clips never reach the AI analyzer.
    """
    title = (clip.get("viral_title") or clip.get("title") or "").lower()

    _BANNED_TERMS = [
        # Sexual violence
        "sexual assault", "rape", "raping", "raped", "rapist",
        "molest", "molested", "molesting", "grope", "groped",
        "child abuse", "sexually assault", "sex offend", "sexually harass",
        # Self-harm
        "suicide", "self harm", "self-harm", "overdose",
        # Mass violence
        "mass shooting", "terrorist", "genocide",
        # CSAM
        "child porn", "cp ", "csam",
        # Explicit content
        "nude", "naked", "onlyfans leak", "nsfw",
        "porn", "porno", "pornography",
        # Slurs (title-level check — also caught at caption render time via censor_word)
        "nigger", "faggot", "chink", "spic", "kike", "wetback",
        # Platform ban triggers
        "doxxing", "swatting gone wrong",
    ]

    return not any(term in title for term in _BANNED_TERMS)


def passes_quality_filter(clip: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Layer 1 quality gate — fast rule-based filter run before any AI analysis.

    Returns:
        (True,  reason) — clip passes; reason is either 'AUTO_APPROVE:...'
                          or 'NEEDS_AI_ANALYSIS'.
        (False, reason) — clip rejected; reason is a short description.
    """
    title      = (clip.get("viral_title") or clip.get("title") or "").strip()
    title_lower = title.lower()
    duration   = float(clip.get("duration") or clip.get("duration_sec") or 0)
    creator    = (clip.get("creator_name") or "").lower()
    transcript = clip.get("transcript_preview") or ""
    view_count = int(clip.get("view_count") or 0)
    upvotes    = int(clip.get("upvotes") or 0)
    source     = clip.get("discovery_source") or ""

    # ── Hard rejections ─────────────────────────────────────────────────────
    if 0 < duration < 20:
        return False, f"Too short ({duration:.0f}s)"
    if duration > 180:
        return False, f"Too long ({duration:.0f}s)"
    if not title or len(title) < 5:
        return False, "Title missing or too short"

    for pattern in _CONTEXT_PATTERNS:
        if re.search(pattern, title_lower):
            return False, "Context-dependent title"

    if transcript:
        words = transcript.strip().lower().split()
        if words and words[0] in _MID_SENTENCE_STARTERS:
            return False, "Starts mid-sentence"

    # ── Strong-signal auto-approve ───────────────────────────────────────────
    if upvotes >= 1000:
        return True, "AUTO_APPROVE:reddit_viral"
    if upvotes >= 500 and source == "reddit_trending":
        return True, "AUTO_APPROVE:reddit_strong"
    if view_count >= 1_000_000:
        return True, "AUTO_APPROVE:million_views"
    if view_count >= 500_000 and source in ("youtube_shorts_trending", "tiktok_trending"):
        return True, "AUTO_APPROVE:trending_high_views"
    if source == "reddit_trending" and upvotes >= 200:
        return True, "AUTO_APPROVE:reddit_moderate"

    # ── Positive signals check before handing to AI ─────────────────────────
    has_positive = any([
        any(w in title_lower for w in _VIRAL_WORDS),
        any(t in creator for t in _TIER1_NAMES),
        view_count >= 10_000,
        upvotes >= 50,
        source in _VIRAL_DISCOVERY_SOURCES,
    ])

    if not has_positive:
        return False, "No positive viral signals"

    return True, "NEEDS_AI_ANALYSIS"


def calculate_viral_potential_score(clip: Dict[str, Any]) -> float:
    """
    Compute viral potential score (0–100) based on discovery source, engagement,
    creator tier, viral title signals, and duration.

    Source base scores (from _SOURCE_BASE_SCORES):
        reddit_trending            → 60   tiktok_trending            → 55
        youtube_shorts_trending    → 50   twitter_trending           → 45
        youtube_gaming_trending    → 42   streamable_trending        → 35
        twitch_api                 → 25   youtube_api / direct_api   → 20
        kick_api                   → 15   (unknown)                  → 20

    Bonuses:
        +15  tier1 creator
        +8   viral signal word in title
        +10  view_count >= 1M
        +8   view_count >= 100K
        +5   view_count >= 10K
        +10  upvotes >= 5000
        +7   upvotes >= 1000
        +5   upvotes >= 200
        +5   duration 30–90 s (sweet spot)
        -5   duration < 20 s or > 150 s
    """
    discovery_source = clip.get("discovery_source") or ""
    base  = _SOURCE_BASE_SCORES.get(discovery_source, 20.0)
    score = base

    # View count bonus
    view_count = int(clip.get("view_count") or 0)
    if view_count >= 1_000_000:
        score += 10.0
    elif view_count >= 100_000:
        score += 8.0
    elif view_count >= 10_000:
        score += 5.0

    # Upvote bonus (Reddit)
    upvotes = int(clip.get("upvotes") or 0)
    if upvotes >= 5_000:
        score += 10.0
    elif upvotes >= 1_000:
        score += 7.0
    elif upvotes >= 200:
        score += 5.0

    # Tier-1 creator bonus
    creator = (clip.get("creator_name") or "").lower()
    try:
        from viral_creators import is_tier1_creator
        if is_tier1_creator(creator):
            score += 15.0
    except ImportError:
        if any(t in creator for t in _TIER1_NAMES):
            score += 15.0

    # Viral signal in title
    title_lower = (clip.get("viral_title") or clip.get("title") or "").lower()
    if any(w in title_lower for w in _VIRAL_WORDS):
        score += 8.0

    # Duration bonus/penalty
    duration = float(clip.get("duration") or clip.get("duration_sec") or 0)
    if 30.0 <= duration <= 90.0:
        score += 5.0
    elif duration < 20.0 or duration > 150.0:
        score -= 5.0

    return min(round(score, 1), 100.0)


def calculate_final_score(clip: Dict[str, Any]) -> float:
    """
    Compute the final clip score combining viral potential + AI quality + freshness.

    Formula:
        final = (viral_score × 0.4) + (ai_quality_score × 0.4) + (freshness × 10)
    Capped at 100.

    Args:
        clip: Clip dict.  Expected to have 'score' (viral), 'ai_quality_score',
              and optionally 'freshness_score' (0–1, default 0.5).

    Returns:
        Final score float (0–100).
    """
    viral_score      = calculate_viral_potential_score(clip)
    ai_score         = float(clip.get("ai_quality_score") or 50.0)
    freshness        = float(clip.get("freshness_score") or 0.5)
    freshness_bonus  = freshness * 10.0

    final = (viral_score * 0.4) + (ai_score * 0.4) + freshness_bonus
    return min(round(final, 1), 100.0)


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")

    print("Testing scorer.py...")
    print()

    try:
        from preferences import load_preferences
        prefs = load_preferences()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Test clips
    test_clips = [
        {
            "title": "Insane clutch — target streamer",
            "source": "twitch",
            "mode": "auto",
            "creator_name": prefs.get("target_streamers", ["xqc"])[0],
            "view_count": 25000,
            "duration": 65.0,
            "created_at": "2025-01-01T10:00:00Z",
            "local_path": None,
        },
        {
            "title": "Average clip — unknown streamer",
            "source": "twitch",
            "mode": "auto",
            "creator_name": "unknownstreamer99",
            "view_count": 500,
            "duration": 30.0,
            "created_at": "2025-01-01T10:00:00Z",
            "local_path": None,
        },
        {
            "title": "My manual clip",
            "source": "manual",
            "mode": "manual",
            "creator_name": None,
            "view_count": 0,
            "duration": 90.0,
            "created_at": None,
            "local_path": None,
        },
    ]

    scored = score_clips(test_clips, user_prefs=prefs)

    print(f"{'Title':<40}  {'Score':>6}  {'Solo':>5}  {'Pair':>5}")
    print("-" * 65)
    for clip in scored:
        print(
            f"  {clip['title']:<38}  {clip['score']:>6.1f}  "
            f"{'Yes' if clip.get('is_solo_worthy') else 'No':>5}  "
            f"{'Yes' if clip.get('pairing_candidate') else 'No':>5}"
        )
        if "score_breakdown" in clip:
            bd = clip["score_breakdown"]
            print(f"    breakdown: {bd}")

    print()

    # Test filter
    min_score = prefs.get("minimum_clip_quality_score", 40)
    passed, skipped = filter_by_min_score(scored, min_score)
    print(f"\nFilter (min_score={min_score}): {len(passed)} passed, {len(skipped)} skipped")
    print("\nScorer test complete.")
