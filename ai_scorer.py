"""
ai_scorer.py
============
NOTE: This module is currently disabled and reserved for a future paid tier
update. It is not called anywhere in the active pipeline. Do not import or
invoke it from scorer.py or any other active module.

Claude-powered virality scoring for ClipCast Studio.

Sends clip metadata (title, creator, game, view_count, duration, source) to
the Claude API and receives a 0–100 virality score broken down into 6 dimensions:

  1. hook_strength      — How likely a viewer stops scrolling in the first 2s.
  2. emotional_trigger  — Strength of emotional reaction (hype, shock, humour).
  3. shareability       — How likely viewers will share or stitch this clip.
  4. trend_alignment    — How well the content fits current gaming trends.
  5. creator_fame_bonus — Creator's broader cultural reach (solo clout factor).
  6. replay_value       — Whether viewers would watch again (loops).

Each dimension is scored 0–100 and the composite score is a weighted average:
  hook_strength × 0.25 + emotional_trigger × 0.20 + shareability × 0.20 +
  trend_alignment × 0.15 + creator_fame_bonus × 0.10 + replay_value × 0.10

Caching
-------
Scores are cached in the ``ai_scores`` SQLite table (added to clipcast.db by
this module on first import). Cache key = MD5 of the clip metadata dict
(title, creator_name, view_count, duration, source). Cache never expires; the
score for the same clip metadata will always be the same.

Cost awareness
--------------
Each scoring call costs ~300–500 input tokens + ~200 output tokens.
The cache eliminates redundant calls. A quota guard in preferences.yaml
(ai_scorer_enabled: true/false) lets the user turn scoring off entirely.

Usage
-----
    from ai_scorer import score_clip_ai
    result = score_clip_ai(clip, api_key="sk-ant-...")
    # result: {composite: float, dimensions: dict, cached: bool, error: str|None}

Test
----
    python ai_scorer.py
"""

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

import database

logger = logging.getLogger(__name__)

# ── Model to use ───────────────────────────────────────────────────────────────
AI_SCORER_MODEL = "claude-haiku-4-5-20251001"   # Fast + cheap; upgrade to sonnet for more nuance

# ── Dimension weights (must sum to 1.0) ───────────────────────────────────────
DIMENSION_WEIGHTS: Dict[str, float] = {
    "hook_strength":      0.25,
    "emotional_trigger":  0.20,
    "shareability":       0.20,
    "trend_alignment":    0.15,
    "creator_fame_bonus": 0.10,
    "replay_value":       0.10,
}

# ── Prompt template ────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are a viral content analyst specialising in short-form gaming clips for TikTok, YouTube Shorts, and Instagram Reels.

You will receive metadata about a gaming clip and must score it on 6 virality dimensions. Respond ONLY with a valid JSON object — no prose, no markdown, no code fences.

JSON schema (all values are integers 0–100):
{
  "hook_strength": <int>,
  "emotional_trigger": <int>,
  "shareability": <int>,
  "trend_alignment": <int>,
  "creator_fame_bonus": <int>,
  "replay_value": <int>,
  "reasoning": "<one short sentence per dimension, pipe-separated>"
}

Scoring guide:
- hook_strength: 0 = slow start, nothing grabs attention | 100 = instantly shocking or meme-able moment
- emotional_trigger: 0 = neutral/informational | 100 = extreme hype, rage, shock, or laughter
- shareability: 0 = niche appeal only | 100 = non-gamer friends will share this
- trend_alignment: 0 = obscure game/format | 100 = top trending game + viral format right now
- creator_fame_bonus: 0 = unknown streamer | 100 = globally famous creator (xQc, Ninja, etc.)
- replay_value: 0 = one-time watch | 100 = loop-worthy, will watch 10+ times
"""

_USER_PROMPT_TEMPLATE = """Rate this gaming clip:

Title: {title}
Creator: {creator_name}
Game: {game}
Source: {source}
View count: {view_count:,}
Duration: {duration:.0f} seconds
"""


# ══════════════════════════════════════════════════════════════════════════════
# Cache table initialization
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_cache_table(db_path: Optional[Path] = None) -> None:
    """Create the ai_scores cache table if it does not already exist."""
    conn = database.get_connection(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_scores (
                cache_key   TEXT PRIMARY KEY,
                composite   REAL NOT NULL,
                dimensions  TEXT NOT NULL,   -- JSON object
                reasoning   TEXT,
                scored_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    except Exception as exc:
        logger.debug("ai_scores table init: %s", exc)
    finally:
        conn.close()


_cache_table_ready = False


def _init_cache(db_path: Optional[Path] = None) -> None:
    global _cache_table_ready
    if not _cache_table_ready:
        _ensure_cache_table(db_path)
        _cache_table_ready = True


# ══════════════════════════════════════════════════════════════════════════════
# Cache helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_cache_key(clip: Dict[str, Any]) -> str:
    """MD5 of the fields that uniquely identify a clip's content."""
    payload = json.dumps({
        "title":        (clip.get("title") or "").strip().lower(),
        "creator_name": (clip.get("creator_name") or "").strip().lower(),
        "view_count":   int(clip.get("view_count") or 0),
        "duration":     round(float(clip.get("duration") or clip.get("duration_sec") or 0), 0),
        "source":       clip.get("source", ""),
    }, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()


def _get_cached_score(cache_key: str, db_path: Optional[Path] = None) -> Optional[Dict]:
    """Return cached AI score or None if not cached."""
    conn = database.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT composite, dimensions, reasoning FROM ai_scores WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row:
            return {
                "composite":  float(row["composite"]),
                "dimensions": json.loads(row["dimensions"]),
                "reasoning":  row["reasoning"],
                "cached":     True,
                "error":      None,
            }
        return None
    except Exception:
        return None
    finally:
        conn.close()


def _store_cached_score(
    cache_key: str,
    composite: float,
    dimensions: Dict[str, float],
    reasoning: str,
    db_path: Optional[Path] = None,
) -> None:
    """Persist a score to the cache table."""
    conn = database.get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_scores "
            "(cache_key, composite, dimensions, reasoning) VALUES (?, ?, ?, ?)",
            (cache_key, composite, json.dumps(dimensions), reasoning),
        )
        conn.commit()
    except Exception as exc:
        logger.debug("ai_scores cache write failed: %s", exc)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Claude API call
# ══════════════════════════════════════════════════════════════════════════════

def _call_claude(
    clip: Dict[str, Any],
    api_key: str,
) -> Dict[str, Any]:
    """
    Send clip metadata to Claude and parse the JSON response.

    Returns a result dict with keys:
        composite (float), dimensions (dict), reasoning (str), error (str|None).
    """
    try:
        import anthropic
    except ImportError:
        return {
            "composite": 50.0,
            "dimensions": {d: 50 for d in DIMENSION_WEIGHTS},
            "reasoning": "anthropic package not installed — using neutral score",
            "error": "anthropic not installed",
        }

    duration = float(clip.get("duration") or clip.get("duration_sec") or 0)
    view_count = int(clip.get("view_count") or 0)
    game = (
        clip.get("game") or
        clip.get("title", "")[:20] or
        "Unknown Game"
    )

    user_msg = _USER_PROMPT_TEMPLATE.format(
        title=clip.get("title") or "Untitled",
        creator_name=clip.get("creator_name") or "Unknown",
        game=game,
        source=clip.get("source") or "unknown",
        view_count=view_count,
        duration=duration,
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=AI_SCORER_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        data = json.loads(raw_text)

        # Extract and validate dimensions
        dimensions: Dict[str, float] = {}
        for dim in DIMENSION_WEIGHTS:
            val = data.get(dim)
            if val is None:
                logger.warning("AI scorer: missing dimension '%s' in response", dim)
                dimensions[dim] = 50.0
            else:
                dimensions[dim] = max(0.0, min(100.0, float(val)))

        reasoning = data.get("reasoning", "")

        # Weighted composite
        composite = sum(
            dimensions[dim] * weight
            for dim, weight in DIMENSION_WEIGHTS.items()
        )
        composite = round(max(0.0, min(100.0, composite)), 2)

        logger.debug(
            "AI score for '%s': composite=%.1f  dims=%s",
            (clip.get("title") or "")[:40],
            composite,
            {d: round(v) for d, v in dimensions.items()},
        )
        return {
            "composite":  composite,
            "dimensions": dimensions,
            "reasoning":  reasoning,
            "cached":     False,
            "error":      None,
        }

    except json.JSONDecodeError as exc:
        logger.warning("AI scorer: JSON parse error: %s  raw=%r", exc, raw_text[:200])
        return {
            "composite": 50.0,
            "dimensions": {d: 50.0 for d in DIMENSION_WEIGHTS},
            "reasoning": "JSON parse error — using neutral score",
            "cached": False,
            "error": str(exc),
        }
    except Exception as exc:
        logger.warning("AI scorer: API call failed: %s", exc)
        return {
            "composite": 50.0,
            "dimensions": {d: 50.0 for d in DIMENSION_WEIGHTS},
            "reasoning": f"API error: {exc}",
            "cached": False,
            "error": str(exc),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def score_clip_ai(
    clip: Dict[str, Any],
    api_key: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Return an AI virality score for a clip, using the cache when available.

    If ``api_key`` is None or empty, loads ``anthropic_api_key`` from
    config.yaml automatically. If no key is configured, returns a neutral
    score (50.0) with error='no_api_key'.

    Args:
        clip:    Clip dict with at least 'title', 'creator_name', 'view_count',
                 'duration', 'source'.
        api_key: Anthropic API key. Pass None to auto-load from config.yaml.
        db_path: Override database path (useful for tests).

    Returns:
        Dict with keys:
            composite   (float) — Weighted score 0–100.
            dimensions  (dict)  — Per-dimension scores.
            reasoning   (str)   — Claude's one-line reasoning per dimension.
            cached      (bool)  — True if result came from cache.
            error       (str|None) — Error message if scoring failed.
    """
    _init_cache(db_path)

    # ── Resolve API key ────────────────────────────────────────────────────────
    if not api_key:
        try:
            from preferences import load_config
            cfg = load_config()
            api_key = cfg.get("anthropic", {}).get("api_key", "")
        except Exception:
            api_key = ""

    if not api_key or api_key.startswith("YOUR_"):
        return {
            "composite":  50.0,
            "dimensions": {d: 50.0 for d in DIMENSION_WEIGHTS},
            "reasoning":  "No API key — using neutral score",
            "cached":     False,
            "error":      "no_api_key",
        }

    # ── Cache lookup ───────────────────────────────────────────────────────────
    cache_key = _make_cache_key(clip)
    cached = _get_cached_score(cache_key, db_path)
    if cached:
        logger.debug("AI score: cache hit for '%s'", (clip.get("title") or "")[:40])
        return cached

    # ── Fresh Claude call ──────────────────────────────────────────────────────
    result = _call_claude(clip, api_key)
    if not result.get("error"):
        _store_cached_score(
            cache_key,
            result["composite"],
            result["dimensions"],
            result.get("reasoning", ""),
            db_path,
        )

    return result


def score_clips_ai(
    clips: list,
    api_key: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list:
    """
    Score a list of clips and attach AI score fields to each clip dict.

    Adds keys to each clip:
        ai_score        (float) — Composite AI virality score 0–100.
        ai_dimensions   (dict)  — Per-dimension breakdown.
        ai_cached       (bool)  — Whether score came from cache.

    Args:
        clips:   List of clip dicts.
        api_key: Anthropic API key (auto-loaded from config.yaml if None).
        db_path: Override database path.

    Returns:
        The same list, each clip updated with AI score fields.
    """
    _init_cache(db_path)

    # Resolve once
    if not api_key:
        try:
            from preferences import load_config
            cfg = load_config()
            api_key = cfg.get("anthropic", {}).get("api_key", "")
        except Exception:
            api_key = ""

    for clip in clips:
        result = score_clip_ai(clip, api_key=api_key, db_path=db_path)
        clip["ai_score"]      = result["composite"]
        clip["ai_dimensions"] = result.get("dimensions", {})
        clip["ai_cached"]     = result.get("cached", False)

    return clips


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(name)s  %(message)s")

    print("=" * 60)
    print("ai_scorer.py  —  self-test")
    print("=" * 60)

    tmp_db = Path(tempfile.mktemp(suffix="_ai_scorer_test.db"))
    print(f"\nUsing temp DB: {tmp_db}")

    try:
        database.initialize_database(db_path=tmp_db)
        _ensure_cache_table(db_path=tmp_db)
        print("Cache table created: OK")

        test_clip = {
            "title":        "xQc rage quits after 30 second 1v4 clutch",
            "creator_name": "xQc",
            "game":         "Valorant",
            "source":       "twitch",
            "view_count":   125_000,
            "duration":     62.0,
        }

        print(f"\nTest clip: '{test_clip['title']}'")
        print("Calling score_clip_ai() — will use neutral score (no real API key in test)...")

        result = score_clip_ai(test_clip, api_key=None, db_path=tmp_db)

        print(f"  composite:  {result['composite']}")
        print(f"  cached:     {result['cached']}")
        print(f"  error:      {result['error']}")
        print(f"  dimensions: {result['dimensions']}")
        assert result["composite"] >= 0.0 and result["composite"] <= 100.0, \
            "Composite score out of range"
        assert isinstance(result["dimensions"], dict), "dimensions must be a dict"
        print("Score range check: OK")

        # Test cache: call again with same clip — should return cached=True
        print("\nCalling again (cache miss expected since no real score was stored)...")
        result2 = score_clip_ai(test_clip, api_key=None, db_path=tmp_db)
        print(f"  cached: {result2['cached']}  error: {result2['error']}")
        # (Cache only stores successful API responses, so no_api_key results are not cached)

        # Test cache_key stability
        key1 = _make_cache_key(test_clip)
        key2 = _make_cache_key({**test_clip, "url": "https://example.com/different_url"})
        assert key1 == key2, "Cache key should not include url"
        print(f"\nCache key stability (url excluded): OK  key={key1[:16]}...")

        # Test score_clips_ai
        clips = [test_clip, {**test_clip, "title": "Another clip", "view_count": 1000}]
        scored = score_clips_ai(clips, db_path=tmp_db)
        assert all("ai_score" in c for c in scored), "ai_score key missing"
        assert all("ai_dimensions" in c for c in scored), "ai_dimensions key missing"
        print(f"\nscore_clips_ai on 2 clips: OK")
        for c in scored:
            print(f"  '{c['title'][:35]}': ai_score={c['ai_score']}  cached={c['ai_cached']}")

        print("\n" + "=" * 60)
        print("All ai_scorer.py tests PASSED.")
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
