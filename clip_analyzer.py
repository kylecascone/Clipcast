"""
clip_analyzer.py
================
AI-powered clip quality scorer and title rewriter (Layer 2 of the two-layer
quality filter).

Uses Claude Haiku to rate clip quality and generate an optimised viral title.
It is NOT a gatekeeper — the bar for rejection is very high (<10% target).
Almost every clip that passed Layer 1 should be accepted; only genuinely
un-postable content (completely silent, private joke with zero context,
pure stream-filler) is rejected.

If ANTHROPIC_API_KEY is not set, all clips pass with a default score of 55.

Cost estimate: ~$0.001 per clip with claude-haiku-4-5.

Usage
-----
    from clip_analyzer import analyze_clip_with_ai, batch_analyze_clips
    result = analyze_clip_with_ai(clip)
    accepted = batch_analyze_clips(clips_needing_ai)
"""

import json
import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _sanitize(text: str, max_len: int = 500) -> str:
    """Strip characters that can break JSON payloads and truncate."""
    if not text:
        return ""
    # Remove control characters (except tab/newline which are safe in JSON strings)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Replace curly braces that could corrupt the f-string JSON template
    text = text.replace("{", "(").replace("}", ")")
    return text[:max_len]


def analyze_clip_with_ai(clip: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ask Claude Haiku to score a clip and suggest the best viral title.

    This is a scorer + title rewriter, NOT a binary gatekeeper.
    Rejection threshold is very high — only reject content that is
    completely un-postable.  Target rejection rate: under 10%.

    Args:
        clip: ClipCast clip dict with at least title, creator_name, duration.

    Returns:
        Dict with keys:
            should_post (bool)             — post or skip (almost always True)
            ai_quality_score (float)       — 0–100 composite
            skip_reason (str|None)         — reason only if actually rejected
            viral_title_suggestion (str)   — always provided, improved title
    """
    _default = {
        "should_post":            True,
        "ai_quality_score":       55.0,
        "skip_reason":            None,
        "viral_title_suggestion": None,
    }

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("analyze_clip_with_ai: ANTHROPIC_API_KEY not set — skipping AI analysis")
        return _default

    try:
        import anthropic
    except ImportError:
        logger.debug("analyze_clip_with_ai: anthropic package not installed — skipping")
        return _default

    client = anthropic.Anthropic(api_key=api_key)

    raw_title  = clip.get("viral_title") or clip.get("title") or ""
    title      = _sanitize(raw_title, 500)
    creator    = _sanitize(clip.get("creator_name") or "", 100)
    category   = _sanitize(clip.get("category") or "", 100)
    duration   = clip.get("duration") or clip.get("duration_sec") or 0
    theme      = _sanitize(clip.get("theme") or "", 200)
    transcript = _sanitize(clip.get("transcript_preview") or "", 400)
    view_count = clip.get("view_count") or 0
    upvotes    = clip.get("upvotes") or 0
    source     = _sanitize(clip.get("discovery_source") or "", 100)

    prompt = f"""You are a viral TikTok hook writer for a short clip posting account with 2 million followers. Your titles stop people mid-scroll.

Every title you write must:
- Immediately tell viewers what the exciting/funny/shocking moment IS
- Create curiosity or strong emotion in under 8 words
- Use present tense and active language
- End with ONE highly relevant emoji
- Never be a generic label — always describe the MOMENT not the object

BAD titles (never write these):
- 'pikachu cake' — just a label, no emotion, no hook
- 'funny moment' — too vague
- 'gaming clip' — means nothing
- 'xQc reacts' — to what?? always finish the thought

GOOD titles for the same content:
- 'She built this Pikachu cake LIVE on stream 🎂'
- 'Streamer spends 8 hours making this cake on camera 😱'
- 'Chat went insane when she revealed this cake 🔥'
- 'Tyler1 loses his mind after getting destroyed in ranked 💀'
- 'Kai Cenat breaks down crying after fan donation 😭'
- 'IShowSpeed gets banned live for THIS moment 😳'
- 'Streamer catches cheater mid-game and goes off 🤯'

Content types and how to title them:
- Cooking/art streams: focus on the reveal, the reaction, the time spent
- Gaming rage: what happened that caused the rage
- Wholesome moments: what happened that was heartwarming
- Bans/drama: what they did and why it matters
- Donations/gifts: the emotional reaction
- Fails: what went wrong and how bad
- Records/achievements: what was accomplished

Analyze this clip:
Title: {title}
Creator: {creator}
Category: {category}
Duration: {duration} seconds
Theme: {theme}
Views: {view_count:,}
Upvotes: {upvotes:,}
Source: {source}
Transcript: {transcript or 'Not available'}

Respond ONLY with JSON:
{{
  "self_contained_score": <0-10>,
  "entertainment_score": <0-10>,
  "tiktok_fit_score": <0-10>,
  "should_post": <true or false>,
  "skip_reason": <null or one sentence only if truly unwatchable>,
  "viral_title_suggestion": <punchy hook title under 60 chars with emoji>
}}

CRITICAL: viral_title_suggestion must describe the MOMENT not just label the content. Make someone NEED to watch it.
Rejection rate target: under 10%. Your job is scoring and titling, not gatekeeping."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)

        ai_score = (
            float(result.get("entertainment_score", 5)) * 0.4
            + float(result.get("tiktok_fit_score",  5)) * 0.4
            + float(result.get("self_contained_score", 5)) * 0.2
        ) * 10.0

        suggestion = result.get("viral_title_suggestion") or title or None

        return {
            "should_post":            bool(result.get("should_post", True)),
            "ai_quality_score":       round(ai_score, 1),
            "skip_reason":            result.get("skip_reason"),
            "viral_title_suggestion": suggestion,
        }

    except anthropic.BadRequestError as exc:
        logger.warning(
            "analyze_clip_with_ai: 400 Bad Request for clip title %r — skipping. Error: %s",
            raw_title[:120],
            exc,
        )
        return _default

    except Exception as exc:
        logger.warning("analyze_clip_with_ai: API error: %s", exc)
        return _default


def batch_analyze_clips(clips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Run AI quality scoring on a list of clips and return only accepted ones.

    Prints a summary line after each rejection (rare).
    Sorts accepted clips by ai_quality_score DESC.
    Also always sets viral_title_suggestion even when AI is unavailable.

    Args:
        clips: Clips that passed Layer 1 filter but were not auto-approved.

    Returns:
        List of accepted clip dicts, each with 'ai_quality_score' added.
        Rejected clips are dropped.
    """
    accepted: List[Dict[str, Any]] = []
    rejected_count = 0

    for clip in clips:
        result = analyze_clip_with_ai(clip)
        if not result.get("should_post", True):
            rejected_count += 1
            logger.debug(
                "AI rejected: %s — %s",
                clip.get("title", "")[:50],
                result.get("skip_reason", ""),
            )
            print(
                f"  AI rejected: {clip.get('title','')[:50]} "
                f"— {result.get('skip_reason','')}"
            )
            continue

        clip["ai_quality_score"] = result["ai_quality_score"]
        suggestion = result.get("viral_title_suggestion")
        if suggestion:
            clip["viral_title"] = suggestion
        accepted.append(clip)

    accepted.sort(key=lambda x: float(x.get("ai_quality_score") or 0), reverse=True)
    print(
        f"  AI scorer: {len(clips)} analyzed, "
        f"{len(accepted)} accepted, {rejected_count} rejected"
    )
    return accepted
