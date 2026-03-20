"""
templates.py
============
Defines four distinct visual templates as configuration dictionaries.
Each template function returns a spec dict that editor.py uses to
apply styling, transitions, and overlay elements during video editing.

Templates:
  1  Classic Viral  — Large bold white captions bottom third, clean fade, TikTok standard
  2  Hype Cut       — Red/white Impact font, high-energy badge, fast hard cuts
  3  Reaction Style — Cinematic letterbox, caption in black bar, dramatic look
  4  Minimal Clean  — Almost no text, just a small creator credit bottom-right

All templates enforce:
  - 9:16 vertical aspect ratio (1080×1920 or scaled equivalent)
  - Streamer/channel credit between clips
  - Audio normalization
  - Max clips per compilation (enforced by compiler.py, not here)
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Output video spec (constant across all templates) ─────────────────────────
OUTPUT_WIDTH  = 1080
OUTPUT_HEIGHT = 1920
OUTPUT_FPS    = 30
OUTPUT_ASPECT = "9:16"

# ── Font paths ─────────────────────────────────────────────────────────────────
# These are standard system fonts available on macOS and most Linux distros.
# If a font is not found, moviepy will fall back to its default font.
FONT_SANS_SERIF = "Arial"          # Template 1
FONT_IMPACT     = "Impact"         # Template 2
FONT_ROUNDED    = "Arial-Rounded-MT-Bold"  # Template 3
FONT_BOLD       = "Arial-Bold"     # Template 4

# ── Colors ─────────────────────────────────────────────────────────────────────
WHITE       = (255, 255, 255)
BLACK       = (0, 0, 0)
RED         = (220, 30, 30)
DARK_RED    = (160, 0, 0)
YELLOW      = (255, 220, 0)
SOFT_PURPLE = (120, 80, 180)
SOFT_BLUE   = (70, 130, 200)
CORAL       = (255, 100, 80)
SEMI_BLACK  = (0, 0, 0, 180)       # RGBA — semi-transparent black
SEMI_WHITE  = (255, 255, 255, 200)  # RGBA — semi-transparent white


# ══════════════════════════════════════════════════════════════════════════════
# Template functions
# ══════════════════════════════════════════════════════════════════════════════

def get_template(template_id: int) -> Dict[str, Any]:
    """
    Return the template spec dict for the given template ID.

    Args:
        template_id: Integer 1–4 corresponding to a template.

    Returns:
        Template spec dict used by editor.py.

    Raises:
        ValueError: If template_id is not in range 1–4.
    """
    templates = {
        1: template_classic_viral,
        2: template_hype_cut,
        3: template_reaction_style,
        4: template_minimal_clean,
    }
    if template_id not in templates:
        raise ValueError(
            f"Unknown template_id '{template_id}'. Must be 1, 2, 3, or 4."
        )
    return templates[template_id]()


def template_classic_viral() -> Dict[str, Any]:
    """
    Template 1 — Classic Viral.

    Aesthetic: Clean, TikTok-standard, broadly appealing.
    Best for: Any clip. The safe default that performs consistently.

    Spec:
      - Facecam split top / gameplay bottom when facecam detected; fullscreen fallback
      - Word-by-word captions with yellow highlight on current word
      - Creator name in the top-left corner (small badge)
      - Clean fade-in / fade-out transitions (0.5 second)
    """
    return {
        "id": 1,
        "name": "Classic Viral",

        # ── Video output ───────────────────────────────────────────────────────
        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "fps": OUTPUT_FPS,
        "background_color": BLACK,

        # ── Transitions ────────────────────────────────────────────────────────
        "transition_type": "fade",
        "transition_duration": 0.5,

        # ── Layout — facecam split configuration ──────────────────────────────
        "layout": {
            "facecam_split": 0.45,       # Top section height fraction
            "divider_color": (255, 255, 255),  # White separator line
            "divider_width": 4,
            "zoom_amount": 0.05,
        },

        # ── Captions — word-by-word, yellow highlight ─────────────────────────
        "caption": {
            "enabled": True,
            "position": ("center", 0.82),
            "font": FONT_BOLD,
            "font_size": 72,
            "font_color": WHITE,
            "stroke_color": BLACK,
            "stroke_width": 4,
            "highlight_color": (255, 220, 0),  # Yellow current-word accent
            "bg_color": (0, 0, 0, 200),
            "bg_padding": (24, 14),
            "bg_rounded": False,
            "max_chars_per_line": 28,
            "word_by_word": True,
        },

        # ── Creator badge — top-left corner, understated ───────────────────────
        "credit_card": {
            "enabled": True,
            "position": (0.04, 0.04),
            "font": FONT_SANS_SERIF,
            "font_size": 36,
            "font_color": WHITE,
            "bg_color": (0, 0, 0, 160),
            "bg_padding": (18, 10),
            "bg_rounded": True,
            "show_duration": 3.0,
            "prefix": "",
        },

        "clip_counter": {
            "enabled": False,
        },

        "border": {
            "enabled": False,
        },

        # Subtle gradient at the bottom to anchor the caption
        "gradient_overlay": {
            "enabled": True,
            "position": "bottom",
            "height_fraction": 0.40,
            "start_alpha": 0,
            "end_alpha": 140,
            "color": (0, 0, 0),
        },

        "timestamp_watermark": {
            "enabled": False,
        },
    }


def template_hype_cut() -> Dict[str, Any]:
    """
    Template 2 — Hype Energy.

    Aesthetic: High-energy, fast, red/white gaming aesthetic.
    Best for: Competitive gaming, big plays, hype moments.

    Spec:
      - Facecam split top / gameplay bottom (red divider) when facecam detected
      - Word-by-word captions with red highlight on current word
      - Creator name displayed as a prominent red badge at the top
      - Hard cut transitions — no fade, straight to the action
    """
    return {
        "id": 2,
        "name": "Hype Energy",

        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "fps": OUTPUT_FPS,
        "background_color": (20, 0, 0),

        "transition_type": "cut",
        "transition_duration": 0.0,

        # ── Layout — red divider ───────────────────────────────────────────────
        "layout": {
            "facecam_split": 0.45,
            "divider_color": (220, 30, 30),  # Red separator
            "divider_width": 4,
            "zoom_amount": 0.05,
        },

        "caption": {
            "enabled": True,
            "position": ("center", 0.87),
            "font": FONT_IMPACT,
            "font_size": 72,
            "font_color": WHITE,
            "stroke_color": DARK_RED,
            "stroke_width": 4,
            "highlight_color": (255, 80, 80),  # Red current-word accent
            "bg_color": (180, 0, 0, 210),
            "bg_padding": (24, 14),
            "bg_rounded": False,
            "max_chars_per_line": 26,
            "word_by_word": True,
        },

        "credit_card": {
            "enabled": True,
            "position": ("center", 0.07),
            "font": FONT_IMPACT,
            "font_size": 56,
            "font_color": WHITE,
            "bg_color": (200, 0, 0, 230),
            "bg_padding": (28, 14),
            "bg_rounded": False,
            "show_duration": 2.5,
            "prefix": "",
        },

        "clip_counter": {
            "enabled": True,
            "position": (0.93, 0.05),
            "font": FONT_IMPACT,
            "font_size": 40,
            "font_color": WHITE,
            "bg_color": DARK_RED,
            "bg_padding": (14, 7),
            "format": "{n}/{total}",
        },

        "border": {
            "enabled": False,
        },

        "gradient_overlay": {
            "enabled": False,
        },

        "timestamp_watermark": {
            "enabled": False,
        },
    }


def template_reaction_style() -> Dict[str, Any]:
    """
    Template 3 — Streamer Reaction.

    Aesthetic: Large facecam section emphasizing the streamer's reaction.
    Best for: Shocking moments, rage clips, anything that benefits from tension.

    Spec:
      - Larger facecam split (55%) to feature the reaction
      - Purple divider separating facecam from gameplay
      - Word-by-word captions with purple highlight
      - Hard cuts — no transition softening
    """
    return {
        "id": 3,
        "name": "Streamer Reaction",

        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "fps": OUTPUT_FPS,
        "background_color": (10, 0, 20),

        "transition_type": "cut",
        "transition_duration": 0.0,

        # ── Layout — larger facecam section, purple divider ───────────────────
        "layout": {
            "facecam_split": 0.55,
            "divider_color": (120, 80, 180),  # Purple separator
            "divider_width": 4,
            "zoom_amount": 0.05,
        },

        # Letterbox bars kept for fallback fullscreen mode
        "letterbox": {
            "enabled": False,
        },

        # Caption with purple highlight
        "caption": {
            "enabled": True,
            "position": ("center", 0.935),
            "font": FONT_SANS_SERIF,
            "font_size": 72,
            "font_color": WHITE,
            "stroke_color": BLACK,
            "stroke_width": 4,
            "highlight_color": (180, 100, 255),  # Purple current-word accent
            "bg_color": None,
            "bg_padding": (20, 0),
            "bg_rounded": False,
            "max_chars_per_line": 34,
            "word_by_word": True,
        },

        # Creator name inside the top letterbox bar
        "credit_card": {
            "enabled": True,
            "position": ("center", 0.065),
            "font": FONT_SANS_SERIF,
            "font_size": 40,
            "font_color": (220, 220, 220),
            "bg_color": None,
            "bg_padding": (20, 0),
            "bg_rounded": False,
            "show_duration": 99.0,          # Show for entire clip
            "prefix": "",
        },

        "clip_counter": {
            "enabled": False,
        },

        "border": {
            "enabled": False,
        },

        "gradient_overlay": {
            "enabled": False,
        },

        "timestamp_watermark": {
            "enabled": False,
        },
    }


def template_minimal_clean() -> Dict[str, Any]:
    """
    Template 4 — Minimal Pro.

    Aesthetic: Almost invisible UI — the content is everything.
    Best for: High-quality clips that don't need text to explain themselves.

    Spec:
      - Facecam split with white divider when facecam detected; fullscreen fallback
      - No captions — let the gameplay speak
      - Tiny creator credit in the bottom-right corner only
      - Clean fade transitions
    """
    return {
        "id": 4,
        "name": "Minimal Pro",

        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "fps": OUTPUT_FPS,
        "background_color": BLACK,

        "transition_type": "fade",
        "transition_duration": 0.4,

        # ── Layout — clean white divider ──────────────────────────────────────
        "layout": {
            "facecam_split": 0.45,
            "divider_color": (255, 255, 255),  # White separator
            "divider_width": 4,
            "zoom_amount": 0.05,
        },

        # No large captions — minimal text overlay only
        "caption": {
            "enabled": False,
        },

        # Small creator watermark — bottom right, barely visible
        "credit_card": {
            "enabled": True,
            "position": (0.97, 0.96),
            "font": FONT_SANS_SERIF,
            "font_size": 28,
            "font_color": (200, 200, 200, 140),  # Light, semi-transparent
            "bg_color": None,
            "bg_padding": (0, 0),
            "bg_rounded": False,
            "show_duration": 99.0,
            "prefix": "@",
        },

        "clip_counter": {
            "enabled": False,
        },

        "border": {
            "enabled": False,
        },

        "gradient_overlay": {
            "enabled": False,
        },

        "timestamp_watermark": {
            "enabled": False,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Helpers used by editor.py
# ══════════════════════════════════════════════════════════════════════════════

def get_all_template_names() -> Dict[int, str]:
    """Return a dict of {id: name} for all templates."""
    return {
        1: "Classic Viral",
        2: "Hype Energy",
        3: "Streamer Reaction",
        4: "Minimal Pro",
    }


def describe_template(template_id: int) -> str:
    """Return a one-line description of a template for display purposes."""
    descriptions = {
        1: "Facecam split + word-by-word yellow captions, clean fade, broad appeal",
        2: "Red divider facecam split, red highlighted captions, hard cuts, competitive gaming",
        3: "Large facecam section (55%), purple divider, purple highlighted captions, reactions",
        4: "Facecam split, no captions, tiny creator credit, minimal clean aesthetic",
    }
    return descriptions.get(template_id, "Unknown template")


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing templates.py...\n")

    for tid in range(1, 5):
        spec = get_template(tid)
        print(f"Template {tid}: {spec['name']}")
        print(f"  Description:   {describe_template(tid)}")
        print(f"  Transition:    {spec['transition_type']} ({spec['transition_duration']}s)")
        cap = spec.get("caption", {})
        if cap.get("enabled", True):
            print(f"  Caption style: font={cap.get('font')}, "
                  f"size={cap.get('font_size')}, "
                  f"highlight={cap.get('highlight_color')}, "
                  f"word_by_word={cap.get('word_by_word', False)}")
        else:
            print(f"  Caption style: disabled")
        layout = spec.get("layout", {})
        if layout:
            print(f"  Layout:        facecam_split={layout.get('facecam_split')}, "
                  f"divider={layout.get('divider_color')}")
        print(f"  Credit card:   enabled={spec.get('credit_card', {}).get('enabled')}")
        print(f"  Clip counter:  enabled={spec.get('clip_counter', {}).get('enabled')}")
        print(f"  Border:        enabled={spec.get('border', {}).get('enabled')}")
        print(f"  Gradient:      enabled={spec.get('gradient_overlay', {}).get('enabled')}")
        print(f"  Timestamp:     enabled={spec.get('timestamp_watermark', {}).get('enabled')}")
        print()

    # Test error handling
    try:
        get_template(99)
    except ValueError as e:
        print(f"✓ ValueError correctly raised for invalid template: {e}")

    print("\nTemplates test complete.")
