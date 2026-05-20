"""
tiktok_shop/product_analyzer.py
================================
Ingests a Kalodata CSV export and scores every product using Claude.

Scoring dimensions (0–100 each):
  1. commission_score  — Higher commission % = better earnings per sale
  2. price_score       — Sweet spot is $20–$80 (impulse buy range)
  3. demand_score      — Monthly revenue signals real buyer demand
  4. saturation_score  — Low creator count relative to ad spend = opportunity gap
  5. trend_score       — Category trend alignment (beauty, wellness, gadgets score higher)

Composite = weighted average of all 5 dimensions.

Usage:
    from tiktok_shop.product_analyzer import analyze_csv
    results = analyze_csv("kalodata_export.csv", api_key="sk-ant-...")
"""

import csv
import json
import logging
import os
import time
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

from tiktok_shop import database as ts_db

logger = logging.getLogger(__name__)

# ── Model ─────────────────────────────────────────────────────────────────────
MODEL = "claude-haiku-4-5-20251001"  # Fast + cheap for bulk scoring

# ── Scoring weights (must sum to 1.0) ─────────────────────────────────────────
WEIGHTS = {
    "commission_score": 0.25,
    "price_score":      0.20,
    "demand_score":     0.25,
    "saturation_score": 0.20,
    "trend_score":      0.10,
}

# ── High-opportunity TikTok Shop categories ────────────────────────────────────
HOT_CATEGORIES = {
    "beauty", "skincare", "makeup", "haircare", "wellness",
    "supplements", "fitness", "gadgets", "tech accessories",
    "home", "kitchen", "pet", "fashion accessories",
}

# ── System prompt ──────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are a TikTok Shop affiliate marketing expert. You analyse products and score their earning potential for faceless TikTok content creators.

Score each product on 5 dimensions (0–100 each). Respond ONLY with a valid JSON object — no prose, no markdown, no code fences.

JSON schema:
{
  "commission_score": <int 0-100>,
  "price_score": <int 0-100>,
  "demand_score": <int 0-100>,
  "saturation_score": <int 0-100>,
  "trend_score": <int 0-100>,
  "reasoning": "<one sentence per dimension, pipe-separated>"
}

Scoring guide:
- commission_score: 0=under 5% commission | 50=10% | 100=25%+ commission
- price_score: 0=under $5 or over $200 (bad for impulse) | 100=$25-$60 (perfect impulse buy range)
- demand_score: 0=no monthly revenue data | 50=moderate sales | 100=high monthly revenue with strong reviews
- saturation_score: 0=thousands of creators already promoting | 100=high brand ad spend but very few creators (gap opportunity)
- trend_score: 0=declining or niche category | 100=top trending TikTok category right now (beauty, wellness, gadgets)"""


_USER_PROMPT = """Score this TikTok Shop product for affiliate earning potential:

Product: {product_name}
Category: {category}
Price: ${price_usd}
Commission Rate: {commission_rate}%
Monthly Revenue: ${monthly_revenue}
Brand Ad Spend Score: {ad_spend_score}/100
Number of Creators Promoting It: {creator_count}
Average Video Views: {avg_views}"""


# ══════════════════════════════════════════════════════════════════════════════
# CSV parsing — handles Kalodata's export format
# ══════════════════════════════════════════════════════════════════════════════

# Map from possible Kalodata column names → our internal field names
_COLUMN_MAP = {
    # Product name variations
    "product name":     "product_name",
    "name":             "product_name",
    "title":            "product_name",
    "product title":    "product_name",

    # URL
    "product url":      "product_url",
    "url":              "product_url",
    "link":             "product_url",

    # Category
    "category":         "category",
    "niche":            "category",

    # Commission
    "commission rate":  "commission_rate",
    "commission":       "commission_rate",
    "commission %":     "commission_rate",

    # Price
    "price":            "price_usd",
    "price (usd)":      "price_usd",
    "retail price":     "price_usd",

    # Revenue
    "monthly revenue":  "monthly_revenue",
    "revenue":          "monthly_revenue",
    "sales revenue":    "monthly_revenue",

    # Ad spend
    "ad spend":         "ad_spend_score",
    "ad spend score":   "ad_spend_score",
    "brand ad spend":   "ad_spend_score",

    # Creator count
    "creators":         "creator_count",
    "creator count":    "creator_count",
    "number of videos": "creator_count",
    "video count":      "creator_count",

    # Views
    "avg views":        "avg_views",
    "average views":    "avg_views",
    "views":            "avg_views",
}


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", "").replace("$", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return default


def parse_kalodata_csv(csv_path: str) -> List[Dict[str, Any]]:
    """
    Parse a Kalodata CSV export into a list of product dicts.
    Handles flexible column naming — Kalodata changes column names between exports.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    products = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalise headers
        raw_headers = reader.fieldnames or []
        header_map = {}
        for raw in raw_headers:
            normalised = raw.strip().lower()
            if normalised in _COLUMN_MAP:
                header_map[raw] = _COLUMN_MAP[normalised]

        if not header_map:
            raise ValueError(
                f"No recognised Kalodata columns found. Headers: {raw_headers}\n"
                "Expected columns like: Product Name, Category, Commission Rate, Price, Monthly Revenue"
            )

        for row in reader:
            product: Dict[str, Any] = {
                "product_name":   "",
                "product_url":    "",
                "category":       "Unknown",
                "commission_rate": 0.0,
                "price_usd":      0.0,
                "monthly_revenue": 0.0,
                "ad_spend_score": 0.0,
                "creator_count":  0,
                "avg_views":      0.0,
            }
            for raw_col, internal_field in header_map.items():
                val = row.get(raw_col, "")
                if internal_field in ("commission_rate", "price_usd", "monthly_revenue", "ad_spend_score", "avg_views"):
                    product[internal_field] = _safe_float(val)
                elif internal_field == "creator_count":
                    product[internal_field] = _safe_int(val)
                else:
                    product[internal_field] = str(val).strip()

            # Skip rows with no product name
            if not product["product_name"]:
                continue

            products.append(product)

    logger.info("Parsed %d products from %s", len(products), path.name)
    return products


# ══════════════════════════════════════════════════════════════════════════════
# Claude scoring
# ══════════════════════════════════════════════════════════════════════════════

def _score_product_with_claude(product: Dict[str, Any], client: anthropic.Anthropic) -> Dict[str, Any]:
    """Call Claude to score a single product. Returns enriched product dict."""
    prompt = _USER_PROMPT.format(
        product_name=product["product_name"],
        category=product.get("category", "Unknown"),
        price_usd=product.get("price_usd", 0),
        commission_rate=product.get("commission_rate", 0),
        monthly_revenue=product.get("monthly_revenue", 0),
        ad_spend_score=product.get("ad_spend_score", 0),
        creator_count=product.get("creator_count", 0),
        avg_views=product.get("avg_views", 0),
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        scores = json.loads(raw)

        # Compute composite score
        composite = sum(
            scores.get(dim, 0) * weight
            for dim, weight in WEIGHTS.items()
        )

        product["opportunity_score"] = round(composite, 1)
        product["score_breakdown"] = json.dumps({
            k: scores.get(k, 0) for k in WEIGHTS
        })
        product["score_reasoning"] = scores.get("reasoning", "")

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning("Claude scoring failed for '%s': %s", product["product_name"], e)
        product["opportunity_score"] = 0.0
        product["score_breakdown"] = json.dumps({k: 0 for k in WEIGHTS})
        product["score_reasoning"] = "Scoring failed — review manually."

    return product


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def analyze_csv(
    csv_path: str,
    api_key: Optional[str] = None,
    user_id: int = 1,
    min_score: float = 50.0,
    delay_between_calls: float = 0.3,
) -> List[Dict[str, Any]]:
    """
    Full pipeline: parse CSV → score with Claude → save to database.

    Args:
        csv_path:              Path to Kalodata CSV export.
        api_key:               Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        user_id:               User ID for multi-tenant support.
        min_score:             Only save products scoring above this threshold.
        delay_between_calls:   Seconds between Claude API calls (rate limiting).

    Returns:
        List of scored product dicts saved to the database.
    """
    ts_db.init_tables()

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("No Anthropic API key provided. Set ANTHROPIC_API_KEY env var.")

    client = anthropic.Anthropic(api_key=key)
    products = parse_kalodata_csv(csv_path)

    if not products:
        logger.warning("No products found in CSV.")
        return []

    logger.info("Scoring %d products with Claude...", len(products))
    saved = []

    for i, product in enumerate(products, 1):
        logger.info("[%d/%d] Scoring: %s", i, len(products), product["product_name"])
        scored = _score_product_with_claude(product, client)

        if scored["opportunity_score"] < min_score:
            logger.info("  → Score %.1f below threshold %.1f — skipping.", scored["opportunity_score"], min_score)
            continue

        scored["user_id"] = user_id
        scored["status"] = "scored"
        product_id = ts_db.insert_product(scored)
        scored["id"] = product_id
        saved.append(scored)
        logger.info("  → Score: %.1f — saved as product #%d", scored["opportunity_score"], product_id)

        if i < len(products):
            time.sleep(delay_between_calls)

    logger.info("Analysis complete. %d/%d products saved (score ≥ %.0f).", len(saved), len(products), min_score)
    return saved


def get_top_products(limit: int = 10, user_id: int = 1) -> List[Dict[str, Any]]:
    """Retrieve top-scored products from the database."""
    return ts_db.get_products(user_id=user_id, limit=limit)


# ══════════════════════════════════════════════════════════════════════════════
# CLI test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python -m tiktok_shop.product_analyzer <path_to_kalodata.csv>")
        sys.exit(1)

    results = analyze_csv(sys.argv[1])
    print(f"\n{'='*60}")
    print(f"TOP PRODUCTS (sorted by opportunity score)")
    print(f"{'='*60}")
    for p in sorted(results, key=lambda x: x["opportunity_score"], reverse=True)[:10]:
        print(f"\n{p['opportunity_score']:5.1f}  {p['product_name']}")
        print(f"       ${p['price_usd']:.2f} | {p['commission_rate']:.1f}% commission | {p['creator_count']} creators")
        print(f"       {p['score_reasoning'][:120]}")
