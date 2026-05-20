"""
tiktok_shop/script_generator.py
================================
Generates TikTok-optimised voiceover scripts for affiliate products using Claude.

Script structure (proven TikTok Shop format):
  1. Hook        (0–2s)  — Stop-the-scroll opening line. Bold claim or question.
  2. Problem     (2–8s)  — Agitate the pain point the product solves.
  3. Solution    (8–20s) — Introduce the product naturally, key benefits.
  4. Social Proof(20–28s)— Reviews, stats, or trust signals.
  5. CTA         (28–35s)— Urgency-based call to action with affiliate link mention.

Usage:
    from tiktok_shop.script_generator import generate_script
    result = generate_script(product_id=1, api_key="sk-ant-...")
"""

import json
import logging
import os
from typing import Any, Dict, Optional

import anthropic

from tiktok_shop import database as ts_db

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

# ── Voice style options (passed in preferences) ────────────────────────────────
VOICE_STYLES = {
    "excited":      "upbeat, enthusiastic, fast-paced — like you just discovered something amazing",
    "calm":         "calm, trustworthy, measured — like a knowledgeable friend giving advice",
    "conversational": "casual and relatable — like texting a friend about a product you love",
    "authoritative": "confident and direct — like a specialist recommending the best option",
}

DEFAULT_VOICE_STYLE = "conversational"

# ── System prompt ──────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are an expert TikTok Shop affiliate content writer. You write short-form voiceover scripts that drive product sales through authentic, engaging storytelling.

Your scripts follow this exact structure:
1. HOOK (0-2s): One punchy sentence that stops scrolling. Use bold claims, surprising facts, or relatable questions. Never start with "I" or the product name.
2. PROBLEM (2-8s): Agitate the pain point. Make the viewer feel seen.
3. SOLUTION (8-20s): Introduce the product naturally. Focus on 2-3 key benefits, not features. Be specific.
4. SOCIAL PROOF (20-28s): Add credibility — reviews, number of buyers, before/after.
5. CTA (28-35s): Create urgency. Tell them exactly what to do. Mention the link in bio.

Rules:
- Total script should be 80-110 words (fits in ~35 seconds at natural speaking pace)
- Never sound like an ad. Sound like a recommendation from a real person.
- Use "you" language, not "people" or "users"
- Avoid superlatives like "amazing", "incredible", "life-changing" — they trigger scepticism
- Include ONE specific detail that makes it feel authentic (a real use case, a specific stat)
- End every script with a clear action: "Link's in my bio" or "Check the link below"

Respond ONLY with a JSON object — no prose, no markdown, no code fences.

JSON schema:
{
  "script": "<full voiceover script, no section labels>",
  "hook": "<just the opening hook sentence>",
  "caption": "<TikTok caption under 150 chars with 3-5 relevant hashtags>",
  "estimated_seconds": <int>
}"""


_USER_PROMPT = """Write a TikTok Shop affiliate voiceover script for this product.

Product: {product_name}
Category: {category}
Price: ${price_usd}
Commission Rate: {commission_rate}%
Key Selling Points from Analysis: {score_reasoning}
Voice Style: {voice_style}

The content creator is faceless — no personal story, no face reveal. The script will be read by an AI voiceover over product footage and review screenshots."""


# ══════════════════════════════════════════════════════════════════════════════
# Core generation
# ══════════════════════════════════════════════════════════════════════════════

def generate_script(
    product_id: int,
    api_key: Optional[str] = None,
    voice_style: str = DEFAULT_VOICE_STYLE,
    user_id: int = 1,
) -> Dict[str, Any]:
    """
    Generate a TikTok Shop voiceover script for a product and save a video job.

    Args:
        product_id:   ID of the product in ts_products table.
        api_key:      Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        voice_style:  One of: excited, calm, conversational, authoritative.
        user_id:      User ID for multi-tenant support.

    Returns:
        Dict with keys: video_id, script, hook, caption, estimated_seconds, product_name
    """
    ts_db.init_tables()

    product = ts_db.get_product(product_id)
    if not product:
        raise ValueError(f"Product #{product_id} not found in database.")

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("No Anthropic API key provided. Set ANTHROPIC_API_KEY env var.")

    style_description = VOICE_STYLES.get(voice_style, VOICE_STYLES[DEFAULT_VOICE_STYLE])

    prompt = _USER_PROMPT.format(
        product_name=product["product_name"],
        category=product.get("category", "General"),
        price_usd=product.get("price_usd", 0),
        commission_rate=product.get("commission_rate", 0),
        score_reasoning=product.get("score_reasoning", "High demand, low competition."),
        voice_style=style_description,
    )

    client = anthropic.Anthropic(api_key=key)

    logger.info("Generating script for product #%d: %s", product_id, product["product_name"])

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON: %s\nRaw: %s", e, raw)
        raise ValueError(f"Script generation failed — Claude returned invalid JSON: {e}")

    script = result.get("script", "")
    caption = result.get("caption", f"#{product['product_name'].replace(' ', '')} #TikTokShop #AffiliateFinds")
    estimated_seconds = result.get("estimated_seconds", 35)
    hook = result.get("hook", script[:80])

    # Save video job to database
    video_id = ts_db.insert_video({
        "user_id":    user_id,
        "product_id": product_id,
        "script":     script,
        "caption":    caption,
        "status":     "pending",
    })

    # Mark product as queued
    ts_db.update_product_status(product_id, "video_queued")

    logger.info(
        "Script saved as video #%d (%d words, ~%ds)",
        video_id,
        len(script.split()),
        estimated_seconds,
    )

    return {
        "video_id":         video_id,
        "product_id":       product_id,
        "product_name":     product["product_name"],
        "script":           script,
        "hook":             hook,
        "caption":          caption,
        "estimated_seconds": estimated_seconds,
        "voice_style":      voice_style,
        "commission_rate":  product.get("commission_rate", 0),
        "price_usd":        product.get("price_usd", 0),
    }


def bulk_generate_scripts(
    min_score: float = 65.0,
    max_videos: int = 10,
    api_key: Optional[str] = None,
    voice_style: str = DEFAULT_VOICE_STYLE,
    user_id: int = 1,
) -> list:
    """
    Auto-generate scripts for all approved/scored products above a score threshold.
    Useful for batch processing after a Kalodata import.

    Args:
        min_score:   Only generate for products scoring above this.
        max_videos:  Cap on how many to generate in one run.
        api_key:     Anthropic API key.
        voice_style: Voice style for all generated scripts.
        user_id:     User ID.

    Returns:
        List of generation result dicts.
    """
    products = ts_db.get_products(status="scored", user_id=user_id, limit=max_videos * 2)
    eligible = [p for p in products if p["opportunity_score"] >= min_score][:max_videos]

    if not eligible:
        logger.info("No eligible products found (score ≥ %.0f, status=scored).", min_score)
        return []

    logger.info("Bulk generating %d scripts...", len(eligible))
    results = []

    for product in eligible:
        try:
            result = generate_script(
                product_id=product["id"],
                api_key=api_key,
                voice_style=voice_style,
                user_id=user_id,
            )
            results.append(result)
        except Exception as e:
            logger.error("Failed for product #%d '%s': %s", product["id"], product["product_name"], e)

    logger.info("Bulk generation complete: %d/%d succeeded.", len(results), len(eligible))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# CLI test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    product_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    voice = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VOICE_STYLE

    result = generate_script(product_id=product_id, voice_style=voice)

    print(f"\n{'='*60}")
    print(f"SCRIPT for: {result['product_name']}")
    print(f"Voice style: {result['voice_style']} | ~{result['estimated_seconds']}s")
    print(f"{'='*60}")
    print(f"\n🪝 HOOK:\n{result['hook']}\n")
    print(f"📝 FULL SCRIPT:\n{result['script']}\n")
    print(f"📱 CAPTION:\n{result['caption']}\n")
    print(f"✅ Saved as video #{result['video_id']}")
