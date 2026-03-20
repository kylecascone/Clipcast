"""
preferences.py
==============
Loads, validates, and saves preferences from preferences.yaml.
Includes an interactive setup_wizard() for first-run configuration.

SaaS Note:
    In multi-user mode, load_preferences() will accept a user_id and load
    preferences from the database or a per-user file path instead of the
    global preferences.yaml. The user_id parameter is included now so
    call sites never need to change.
"""

import yaml
import logging
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
PREFERENCES_FILE = BASE_DIR / "preferences.yaml"

# ── Valid option sets ──────────────────────────────────────────────────────────
VALID_CLIP_LENGTHS = [
    "extra_short", "short", "medium_short", "medium", "long", "extra_long"
]
VALID_TEMPLATES = [1, 2, 3, 4]
VALID_CAPTION_STYLES = [1, 2, 3, 4]
VALID_POSTING_BEHAVIORS  = ["immediate", "scheduled"]
VALID_PLATFORMS          = ["tiktok", "youtube_shorts", "instagram_reels"]
VALID_BRANDING_POSITIONS = ["top_left", "top_right", "bottom_left", "bottom_right"]
VALID_ATTRIBUTION_FORMATS = ["standard", "handle"]

# ── Clip length metadata (ranges, descriptions, monetization status) ──────────
CLIP_LENGTH_INFO = {
    "extra_short": {
        "range": "15–30 seconds",
        "description": "Ultra punchy moments, great for views and follower growth",
        "monetizable": False,
        "min_sec": 15,
        "max_sec": 30,
    },
    "short": {
        "range": "30–45 seconds",
        "description": "Quick highlight moments",
        "monetizable": False,
        "min_sec": 30,
        "max_sec": 45,
    },
    "medium_short": {
        "range": "45–60 seconds",
        "description": "Near-monetization threshold, compiler targets 60 s when possible",
        "monetizable": False,
        "min_sec": 45,
        "max_sec": 60,
    },
    "medium": {
        "range": "60–90 seconds",
        "description": "Sweet spot for TikTok monetization and retention (RECOMMENDED)",
        "monetizable": True,
        "min_sec": 60,
        "max_sec": 90,
    },
    "long": {
        "range": "90–120 seconds",
        "description": "Best for storytelling compilations or multi-clip packages",
        "monetizable": True,
        "min_sec": 90,
        "max_sec": 120,
    },
    "extra_long": {
        "range": "120–180 seconds",
        "description": "Maximum length, best for recap or top-moments style content",
        "monetizable": True,
        "min_sec": 120,
        "max_sec": 180,
    },
}

logger = logging.getLogger(__name__)
console = Console()

# ── Session-level flag so monetization warning shows only once per run ─────────
_monetization_warning_shown = False


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def load_preferences(
    user_id: int = 1,
    preferences_file: Optional[Path] = None,
) -> dict:
    """
    Load and validate preferences from preferences.yaml.

    Args:
        user_id: Reserved for SaaS multi-user support. Currently unused.
        preferences_file: Override the default file path (useful for testing).

    Returns:
        Validated preferences dictionary.

    Raises:
        FileNotFoundError: preferences.yaml does not exist.
        ValueError: A preference value is invalid.
    """
    file_path = preferences_file or PREFERENCES_FILE

    if not file_path.exists():
        raise FileNotFoundError(
            f"preferences.yaml not found at {file_path}.\n"
            "Run:  python main.py --setup  to configure ClipCast Studio."
        )

    with open(file_path, "r") as f:
        prefs = yaml.safe_load(f)

    if not prefs:
        raise ValueError(
            "preferences.yaml is empty.\n"
            "Run:  python main.py --setup  to configure ClipCast Studio."
        )

    _validate_preferences(prefs)
    _check_monetization_warning(prefs)

    logger.debug("Preferences loaded for user_id=%d", user_id)
    return prefs


def save_preferences(
    prefs: dict,
    user_id: int = 1,
    preferences_file: Optional[Path] = None,
) -> None:
    """
    Save a preferences dictionary back to preferences.yaml.

    Args:
        prefs: The preferences dictionary to save.
        user_id: Reserved for SaaS multi-user support. Currently unused.
        preferences_file: Override the default file path (useful for testing).
    """
    file_path = preferences_file or PREFERENCES_FILE

    with open(file_path, "w") as f:
        yaml.dump(prefs, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    logger.info("Preferences saved to %s", file_path)
    console.print(f"[green]✓ Preferences saved to {file_path}[/green]")


def get_clip_length_range(clip_length: str) -> tuple[int, int]:
    """
    Return the (min_seconds, max_seconds) tuple for a clip_length key.

    Args:
        clip_length: A key from CLIP_LENGTH_INFO (e.g. "medium").

    Returns:
        Tuple of (min_seconds, max_seconds). Defaults to (60, 90) if key unknown.
    """
    info = CLIP_LENGTH_INFO.get(clip_length)
    if not info:
        logger.warning("Unknown clip_length '%s', defaulting to medium range.", clip_length)
        return (60, 90)
    return (info["min_sec"], info["max_sec"])


def load_config(config_file: Optional[Path] = None) -> dict:
    """
    Load API credentials from config.yaml.

    Args:
        config_file: Override the default file path (useful for testing).

    Returns:
        Config dictionary with API credentials.

    Raises:
        FileNotFoundError: config.yaml does not exist.
    """
    file_path = config_file or (BASE_DIR / "config.yaml")

    if not file_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {file_path}.\n"
            "Copy config.yaml and fill in your API credentials."
        )

    with open(file_path, "r") as f:
        config = yaml.safe_load(f)

    if not config:
        raise ValueError("config.yaml is empty. Fill in your API credentials.")

    return config


# ══════════════════════════════════════════════════════════════════════════════
# Validation (private)
# ══════════════════════════════════════════════════════════════════════════════

def _validate_preferences(prefs: dict) -> None:
    """Validate all preference values. Raises ValueError on first bad value."""

    # clip_length
    clip_length = prefs.get("clip_length")
    if clip_length not in VALID_CLIP_LENGTHS:
        raise ValueError(
            f"Invalid clip_length '{clip_length}'. "
            f"Must be one of: {', '.join(VALID_CLIP_LENGTHS)}"
        )

    # minimum_clip_quality_score
    score = prefs.get("minimum_clip_quality_score", 0)
    if not isinstance(score, (int, float)) or not (0 <= score <= 100):
        raise ValueError("minimum_clip_quality_score must be a number between 0 and 100.")

    # minimum_views (optional, defaults to 0 — any non-negative int is valid)
    min_views = prefs.get("minimum_views", 0)
    if not isinstance(min_views, int) or min_views < 0:
        raise ValueError("minimum_views must be a non-negative integer.")

    # attribution_format (optional)
    attr_fmt = prefs.get("attribution_format")
    if attr_fmt is not None and attr_fmt not in VALID_ATTRIBUTION_FORMATS:
        raise ValueError(
            f"attribution_format must be one of: {', '.join(VALID_ATTRIBUTION_FORMATS)}"
        )

    # max_clips_per_compilation
    max_clips = prefs.get("max_clips_per_compilation", 1)
    if not isinstance(max_clips, int) or max_clips not in (1, 2, 3):
        raise ValueError("max_clips_per_compilation must be 1, 2, or 3.")

    # post_frequency
    post_freq = prefs.get("post_frequency", 1)
    if not isinstance(post_freq, int) or post_freq not in (1, 2, 3):
        raise ValueError("post_frequency must be 1, 2, or 3.")

    # posting_times count must match post_frequency
    posting_times = prefs.get("posting_times", [])
    if not isinstance(posting_times, list) or len(posting_times) != post_freq:
        raise ValueError(
            f"posting_times must contain exactly {post_freq} time(s) "
            f"to match post_frequency. Found {len(posting_times)}."
        )

    # Template values
    for field in ("default_video_template", "manual_mode_default_template"):
        val = prefs.get(field, 1)
        if val not in VALID_TEMPLATES:
            raise ValueError(f"'{field}' must be one of: {VALID_TEMPLATES}")

    # Caption style
    caption_style = prefs.get("default_caption_style", 1)
    if caption_style not in VALID_CAPTION_STYLES:
        raise ValueError(f"default_caption_style must be one of: {VALID_CAPTION_STYLES}")

    # Manual posting behavior
    behavior = prefs.get("manual_mode_posting_behavior", "scheduled")
    if behavior not in VALID_POSTING_BEHAVIORS:
        raise ValueError(
            f"manual_mode_posting_behavior must be one of: {VALID_POSTING_BEHAVIORS}"
        )

    # global_pool_size (optional int, 1–200)
    pool_size = prefs.get("global_pool_size")
    if pool_size is not None:
        if not isinstance(pool_size, int) or not (1 <= pool_size <= 200):
            raise ValueError("global_pool_size must be an integer between 1 and 200.")

    # youtube_lookback_days (optional int, 1–30)
    lookback = prefs.get("youtube_lookback_days")
    if lookback is not None:
        if not isinstance(lookback, int) or not (1 <= lookback <= 30):
            raise ValueError("youtube_lookback_days must be an integer between 1 and 30.")

    # youtube_pool_search_queries (optional list of strings)
    queries = prefs.get("youtube_pool_search_queries")
    if queries is not None:
        if not isinstance(queries, list) or not queries:
            raise ValueError("youtube_pool_search_queries must be a non-empty list of strings.")
        for q in queries:
            if not isinstance(q, str) or not q.strip():
                raise ValueError(
                    "Each entry in youtube_pool_search_queries must be a non-empty string."
                )

    # pool_refresh_interval_hours (optional int, 1–24)
    prf_hours = prefs.get("pool_refresh_interval_hours")
    if prf_hours is not None:
        if not isinstance(prf_hours, int) or not (1 <= prf_hours <= 24):
            raise ValueError("pool_refresh_interval_hours must be an integer between 1 and 24.")

    # clip_reservation_hours (optional int, 1–168)
    res_hours = prefs.get("clip_reservation_hours")
    if res_hours is not None:
        if not isinstance(res_hours, int) or not (1 <= res_hours <= 168):
            raise ValueError("clip_reservation_hours must be an integer between 1 and 168.")

    # custom_editor_output_quality (optional str)
    ce_quality = prefs.get("custom_editor_output_quality")
    if ce_quality is not None and ce_quality not in ("low", "medium", "high"):
        raise ValueError("custom_editor_output_quality must be 'low', 'medium', or 'high'.")

    # custom_editor_default_template (optional int)
    ce_template = prefs.get("custom_editor_default_template")
    if ce_template is not None and ce_template not in VALID_TEMPLATES:
        raise ValueError(f"custom_editor_default_template must be one of: {VALID_TEMPLATES}")

    # Animated caption style (1–4)
    anim_style = prefs.get("animated_caption_style")
    if anim_style is not None and anim_style not in VALID_TEMPLATES:
        raise ValueError(f"animated_caption_style must be 1, 2, 3, or 4.")

    # Animated caption font size (positive int)
    font_size = prefs.get("animated_caption_font_size")
    if font_size is not None:
        if not isinstance(font_size, int) or font_size < 20 or font_size > 200:
            raise ValueError("animated_caption_font_size must be an integer between 20 and 200.")

    # Smart hashtag max (1–30)
    ht_max = prefs.get("smart_hashtags_max")
    if ht_max is not None:
        if not isinstance(ht_max, int) or not (1 <= ht_max <= 30):
            raise ValueError("smart_hashtags_max must be an integer between 1 and 30.")

    # Learning lookback days (1–365)
    lookback = prefs.get("learning_lookback_days")
    if lookback is not None:
        if not isinstance(lookback, int) or not (1 <= lookback <= 365):
            raise ValueError("learning_lookback_days must be an integer between 1 and 365.")

    # best_post_hours (list of ints 0–23)
    best_hours = prefs.get("best_post_hours")
    if best_hours is not None:
        if not isinstance(best_hours, list):
            raise ValueError("best_post_hours must be a list of hours (0–23).")
        for h in best_hours:
            if not isinstance(h, int) or not (0 <= h <= 23):
                raise ValueError(f"best_post_hours contains invalid hour: {h}. Must be 0–23.")

    # Boolean flags
    for field in (
        "allow_clips_from_non_target_streamers",
        "allow_short_clips",
        "allow_youtube_trending",
        "youtube_global_pool_enabled",
        "clip_preview_required",
        "youtube_enabled",
        "twitch_enabled",
        "use_shared_pool",
        "custom_editor_auto_queue",
        "animated_captions_enabled",
        "smart_hashtags_enabled",
        "thumbnail_enabled",
        "learning_enabled",
    ):
        val = prefs.get(field)
        if val is not None and not isinstance(val, bool):
            raise ValueError(f"'{field}' must be true or false.")

    # target_platforms (optional — defaults to ["tiktok"] if absent)
    platforms = prefs.get("target_platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or not platforms:
            raise ValueError("target_platforms must be a non-empty list.")
        for p in platforms:
            if p not in VALID_PLATFORMS:
                raise ValueError(
                    f"Invalid platform '{p}'. "
                    f"Must be one of: {', '.join(VALID_PLATFORMS)}"
                )

    # branding (optional dict)
    branding = prefs.get("branding")
    if branding is not None:
        if not isinstance(branding, dict):
            raise ValueError("branding must be a mapping.")
        for pos_key in ("watermark_position", "channel_name_position"):
            pos = branding.get(pos_key, "bottom_right")
            if pos not in VALID_BRANDING_POSITIONS:
                raise ValueError(
                    f"branding.{pos_key} must be one of: "
                    f"{', '.join(VALID_BRANDING_POSITIONS)}"
                )
        opacity = branding.get("watermark_opacity", 0.7)
        if not isinstance(opacity, (int, float)) or not (0.0 <= float(opacity) <= 1.0):
            raise ValueError(
                "branding.watermark_opacity must be a number between 0.0 and 1.0."
            )


def _check_monetization_warning(prefs: dict) -> None:
    """Show a one-time per-session warning when clip_length is below monetization threshold."""
    global _monetization_warning_shown
    if _monetization_warning_shown:
        return

    clip_length = prefs.get("clip_length", "medium")
    info = CLIP_LENGTH_INFO.get(clip_length, {})

    if not info.get("monetizable", True):
        console.print(
            Panel(
                f"[yellow bold]Monetization Notice[/yellow bold]\n\n"
                f"Clip length is set to [bold]{clip_length}[/bold] ({info.get('range', '')}).\n"
                f"Videos under 60 seconds do [bold]not[/bold] qualify for the "
                f"[bold]TikTok Creator Rewards Program[/bold].\n\n"
                f"Your clips will still be processed and posted normally.\n"
                f"To enable monetization, change clip_length to [bold]medium[/bold] or "
                f"longer in preferences.yaml.",
                title="[yellow]TikTok Monetization[/yellow]",
                border_style="yellow",
            )
        )
        _monetization_warning_shown = True


# ══════════════════════════════════════════════════════════════════════════════
# Setup Wizard
# ══════════════════════════════════════════════════════════════════════════════

def _show_legal_notice() -> None:
    """Display LEGAL.md on first-run setup. Silently skipped if file is missing."""
    legal_file = BASE_DIR / "LEGAL.md"
    if not legal_file.exists():
        return
    try:
        lines = legal_file.read_text(encoding="utf-8").splitlines()
        preview = "\n".join(lines[:60])
        if len(lines) > 60:
            preview += f"\n\n[dim]... ({len(lines) - 60} more lines — see LEGAL.md)[/dim]"
        console.print(
            Panel(
                preview,
                title="[bold yellow]Legal Notice — Please Read Before Continuing[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        Confirm.ask("I have read and understood the legal notice above", default=True)
    except Exception:
        pass  # Never let a missing/unreadable LEGAL.md block setup


def setup_wizard(user_id: int = 1) -> dict:
    """
    Interactive terminal wizard for configuring all preferences.
    Walks through every setting with explanations and saves to preferences.yaml.

    Loads any existing preferences.yaml first so every prompt defaults to the
    current saved value — pressing Enter always keeps the existing setting.

    Args:
        user_id: Reserved for SaaS multi-user support. Currently unused.

    Returns:
        The completed and saved preferences dictionary.
    """
    # ── Load existing preferences as defaults (empty dict on first run) ────────
    existing: dict = {}
    try:
        existing = load_preferences()
    except (FileNotFoundError, ValueError):
        pass  # First-time setup — no file yet, hardcoded fallbacks used below

    def _ev(key: str, fallback):
        """Return existing value for key, or fallback if not set."""
        return existing.get(key, fallback)

    def _el(key: str, fallback: str = "") -> str:
        """Return existing list value joined as a comma string, or fallback."""
        vals = existing.get(key, [])
        if vals:
            return ",".join(str(v) for v in vals)
        return fallback

    # ── Show LEGAL.md on first run (when no existing prefs found) ─────────────
    if not existing:
        _show_legal_notice()

    console.print(
        Panel(
            "[bold cyan]ClipCast Studio — Setup Wizard[/bold cyan]\n\n"
            "This wizard will walk you through every preference setting.\n"
            "Press [bold]Enter[/bold] to accept the default shown in [default brackets].\n"
            "You can re-run this any time with:  [bold]python main.py --setup[/bold]",
            border_style="cyan",
        )
    )

    prefs = {}

    # ── Step 1: Source Targets ─────────────────────────────────────────────────
    console.rule("[bold]Step 1 — Source Targets[/bold]")
    console.print(
        "These are the Twitch streamers and YouTube channels ClipCast will monitor "
        "in automated mode to find viral clips."
    )

    streamers_raw = Prompt.ask(
        "Twitch streamers to monitor [dim](comma-separated usernames)[/dim]",
        default=_el("target_streamers", "xqc,shroud,pokimane"),
    )
    prefs["target_streamers"] = [s.strip() for s in streamers_raw.split(",") if s.strip()]

    youtube_raw = Prompt.ask(
        "YouTube channels to monitor [dim](comma-separated @handles or channel IDs)[/dim]",
        default=_el("target_youtube_channels", "@MoistCr1TiKaL,@Sykkuno"),
    )
    prefs["target_youtube_channels"] = [c.strip() for c in youtube_raw.split(",") if c.strip()]

    games_raw = Prompt.ask(
        "Games to filter clips by [dim](comma-separated, leave empty for all games)[/dim]",
        default=_el("target_games", ""),
    )
    prefs["target_games"] = [g.strip() for g in games_raw.split(",") if g.strip()]

    # ── Step 2: Clip Length ────────────────────────────────────────────────────
    console.rule("[bold]Step 2 — Target Clip Length[/bold]")
    console.print("Choose your target video length. This affects compilation and TikTok monetization.\n")

    for key, info in CLIP_LENGTH_INFO.items():
        mon = "[green]✓ Monetizable[/green]" if info["monetizable"] else "[yellow]✗ Not monetizable[/yellow]"
        console.print(f"  [cyan]{key:<14}[/cyan]  {info['range']:<17}  {info['description']}  {mon}")

    clip_length = Prompt.ask(
        "\nClip length",
        choices=VALID_CLIP_LENGTHS,
        default=_ev("clip_length", "medium"),
    )
    prefs["clip_length"] = clip_length

    # ── Step 3: Quality & Compilation ─────────────────────────────────────────
    console.rule("[bold]Step 3 — Quality & Compilation[/bold]")

    score_str = Prompt.ask(
        "Minimum clip quality score [dim](0–100, clips below this are skipped)[/dim]",
        default=str(_ev("minimum_clip_quality_score", 40)),
    )
    prefs["minimum_clip_quality_score"] = int(score_str)

    prefs["allow_clips_from_non_target_streamers"] = Confirm.ask(
        "Allow clips from non-target Twitch streamers to fill out compilations?",
        default=_ev("allow_clips_from_non_target_streamers", False),
    )

    console.print(
        "\n[dim]Global Twitch pool: when enabled, ClipCast automatically discovers\n"
        "the top live streamers right now (by viewer count) and pulls their\n"
        "best clips into the pool — regardless of game or category.\n"
        "This creates a constantly rotating pool of thousands of fresh clips\n"
        "daily. Larger values = more unique content across all users.\n"
        "Recommended: 50. Range: 1–200.[/dim]"
    )
    pool_size_str = Prompt.ask(
        "Global Twitch pool size [dim](top N trending live streamers to pull from)[/dim]",
        default=str(_ev("global_pool_size", 50)),
    )
    prefs["global_pool_size"] = max(1, min(200, int(pool_size_str)))

    console.print(
        "\n[dim]YouTube trending discovery: when enabled, ClipCast also searches\n"
        "YouTube's trending videos (beyond your channel list) to find\n"
        "additional high-scoring clips — similar to the Twitch setting above.[/dim]"
    )
    prefs["allow_youtube_trending"] = Confirm.ask(
        "Search YouTube trending videos beyond your channel list?",
        default=_ev("allow_youtube_trending", True),
    )

    console.print(
        "\n[dim]YouTube global pool: dramatically expands your daily content pool\n"
        "using three discovery methods — trending chart (50 videos), viral\n"
        "search queries (5 queries × 10 results), and related channel discovery.\n"
        "Combined with the Twitch global pool, this gives you access to\n"
        "thousands of fresh viral clips every day — enough to serve hundreds\n"
        "of users without anyone ever getting the same clip twice.[/dim]"
    )
    prefs["youtube_global_pool_enabled"] = Confirm.ask(
        "Enable YouTube global pool discovery (recommended)?",
        default=_ev("youtube_global_pool_enabled", True),
    )

    max_clips_str = Prompt.ask(
        "Maximum clips per compilation [dim](1, 2, or 3)[/dim]",
        default=str(_ev("max_clips_per_compilation", 2)),
    )
    prefs["max_clips_per_compilation"] = int(max_clips_str)

    # ── Step 4: Posting Schedule ───────────────────────────────────────────────
    console.rule("[bold]Step 4 — Posting Schedule[/bold]")

    post_freq_str = Prompt.ask(
        "How many times per day should ClipCast post? [dim](1, 2, or 3)[/dim]",
        default=str(_ev("post_frequency", 2)),
    )
    post_frequency = int(post_freq_str)
    prefs["post_frequency"] = post_frequency

    console.print(f"Enter [bold]{post_frequency}[/bold] posting time(s) in 24-hour HH:MM format:")
    existing_times = existing.get("posting_times", [])
    fallback_times = ["09:00", "18:00", "21:00"]
    posting_times = []
    for i in range(post_frequency):
        # Use existing saved time if available, then fallback defaults
        if i < len(existing_times):
            slot_default = existing_times[i]
        else:
            slot_default = fallback_times[i]
        t = Prompt.ask(f"  Posting time {i + 1}", default=slot_default)
        posting_times.append(t)
    prefs["posting_times"] = posting_times

    prefs["allow_short_clips"] = Confirm.ask(
        "Allow one supplemental short clip post per day (in addition to scheduled posts)?",
        default=_ev("allow_short_clips", False),
    )

    # ── Step 5: Templates ─────────────────────────────────────────────────────
    console.rule("[bold]Step 5 — Visual Templates[/bold]")
    console.print("  [cyan]1[/cyan]  Clean and Simple  — White credits, clean captions, fade transitions")
    console.print("  [cyan]2[/cyan]  Hype Gaming       — Bold red/black, Impact font, clip counters, fast cuts")
    console.print("  [cyan]3[/cyan]  Chill Vibes       — Gradient overlays, rounded captions, crossfades")
    console.print("  [cyan]4[/cyan]  Viral Moments     — Animated captions, reaction border, hard cuts\n")

    auto_template_str = Prompt.ask(
        "Default template for [bold]automated[/bold] mode",
        choices=["1", "2", "3", "4"],
        default=str(_ev("default_video_template", 1)),
    )
    prefs["default_video_template"] = int(auto_template_str)

    manual_template_str = Prompt.ask(
        "Default template for [bold]manual[/bold] mode [dim](can be different)[/dim]",
        choices=["1", "2", "3", "4"],
        default=str(_ev("manual_mode_default_template", int(auto_template_str))),
    )
    prefs["manual_mode_default_template"] = int(manual_template_str)

    # ── Step 6: Caption Styles ─────────────────────────────────────────────────
    console.rule("[bold]Step 6 — Caption Style[/bold]")
    console.print('  [cyan]1[/cyan]  Hype          — "He had NO idea this was coming | Game | #fyp #viral"')
    console.print('  [cyan]2[/cyan]  Storytelling  — "The moment everything changed for Streamer | #fyp"')
    console.print('  [cyan]3[/cyan]  Question Hook — "Would YOU have done the same thing? | Game | #fyp"')
    console.print('  [cyan]4[/cyan]  Minimal       — "Streamer on Game | #gaming #fyp"\n')
    console.print("[dim]Each style has 3–4 variations that rotate automatically.[/dim]\n")

    caption_str = Prompt.ask(
        "Default caption style for automated mode",
        choices=["1", "2", "3", "4"],
        default=str(_ev("default_caption_style", 1)),
    )
    prefs["default_caption_style"] = int(caption_str)

    # ── Step 7: Manual Mode Behavior ──────────────────────────────────────────
    console.rule("[bold]Step 7 — Manual Mode Behavior[/bold]")
    console.print(
        "When you drop a file into clips/manual or use --manual with a URL, "
        "should ClipCast post it:"
    )
    console.print("  [cyan]immediate[/cyan]  — Process and post right away")
    console.print("  [cyan]scheduled[/cyan]  — Add to the next available posting slot\n")

    behavior = Prompt.ask(
        "Manual mode posting behavior",
        choices=["immediate", "scheduled"],
        default=_ev("manual_mode_posting_behavior", "scheduled"),
    )
    prefs["manual_mode_posting_behavior"] = behavior

    # ── Step 8: Target Platforms ───────────────────────────────────────────────
    console.rule("[bold]Step 8 — Target Platforms[/bold]")
    console.print(
        "Which platforms should ClipCast post your videos to?\n"
        "Add credentials for each platform in config.yaml before enabling.\n"
    )
    console.print("  [cyan]tiktok[/cyan]             — TikTok (primary platform)")
    console.print("  [cyan]youtube_shorts[/cyan]     — YouTube Shorts")
    console.print("  [cyan]instagram_reels[/cyan]    — Instagram Reels\n")

    existing_platforms = existing.get("target_platforms", ["tiktok"])
    platforms_default = ",".join(existing_platforms)
    platforms_raw = Prompt.ask(
        "Target platforms [dim](comma-separated)[/dim]",
        default=platforms_default,
    )
    parsed_platforms = [p.strip() for p in platforms_raw.split(",") if p.strip() in VALID_PLATFORMS]
    prefs["target_platforms"] = parsed_platforms or ["tiktok"]

    # ── Step 9: Branding ───────────────────────────────────────────────────────
    console.rule("[bold]Step 9 — Branding[/bold]")
    console.print(
        "Optional watermark image and channel name overlay on every video.\n"
        "Leave watermark path empty to disable the image overlay.\n"
    )

    existing_branding = existing.get("branding") or {}

    watermark_image = Prompt.ask(
        "Watermark image path [dim](PNG with transparency, leave empty to disable)[/dim]",
        default=existing_branding.get("watermark_image", ""),
    )

    if watermark_image:
        watermark_position = Prompt.ask(
            "Watermark position",
            choices=VALID_BRANDING_POSITIONS,
            default=existing_branding.get("watermark_position", "bottom_right"),
        )
        opacity_str = Prompt.ask(
            "Watermark opacity [dim](0.0–1.0)[/dim]",
            default=str(existing_branding.get("watermark_opacity", 0.7)),
        )
    else:
        watermark_position = "bottom_right"
        opacity_str = "0.7"

    show_channel_name = Confirm.ask(
        "Show channel name text overlay on every video?",
        default=existing_branding.get("show_channel_name", False),
    )

    channel_name_text = ""
    channel_name_position = "bottom_left"
    if show_channel_name:
        channel_name_text = Prompt.ask(
            "Channel name text [dim](e.g. @YourChannel)[/dim]",
            default=existing_branding.get("channel_name_text", ""),
        )
        channel_name_position = Prompt.ask(
            "Channel name position",
            choices=VALID_BRANDING_POSITIONS,
            default=existing_branding.get("channel_name_position", "bottom_left"),
        )

    prefs["branding"] = {
        "watermark_image":       watermark_image,
        "watermark_position":    watermark_position,
        "watermark_opacity":     float(opacity_str),
        "show_channel_name":     show_channel_name,
        "channel_name_text":     channel_name_text,
        "channel_name_position": channel_name_position,
    }

    # ── Step 10: Intro / Outro ─────────────────────────────────────────────────
    console.rule("[bold]Step 10 — Intro / Outro[/bold]")
    console.print(
        "Optional video clips prepended (intro) and appended (outro) to every\n"
        "finished video. Auto-scaled to 9:16 if needed. Leave empty to skip.\n"
    )

    prefs["intro_clip_path"] = Prompt.ask(
        "Intro clip path [dim](leave empty to skip)[/dim]",
        default=_ev("intro_clip_path", ""),
    )
    prefs["outro_clip_path"] = Prompt.ask(
        "Outro clip path [dim](leave empty to skip)[/dim]",
        default=_ev("outro_clip_path", ""),
    )

    # ── Step 11: Preview / Approval ────────────────────────────────────────────
    console.rule("[bold]Step 11 — Video Preview & Approval[/bold]")
    console.print(
        "When enabled, processed videos wait for your manual approval\n"
        "before being added to the posting queue.\n"
        "Run [bold]python main.py --preview[/bold] to review and approve each one.\n"
    )

    prefs["clip_preview_required"] = Confirm.ask(
        "Require manual approval before posting?",
        default=_ev("clip_preview_required", False),
    )

    # ── Step 12: Shared Pool & Source Control ──────────────────────────────────
    console.rule("[bold]Step 12 — Shared Content Pool[/bold]")
    console.print(
        "The shared pool fetches clips once every few hours and caches them\n"
        "in the local database. All pipeline runs draw from this pool instead\n"
        "of making fresh API calls each time — saving YouTube quota and\n"
        "speeding up each pipeline cycle.\n"
    )

    prefs["use_shared_pool"] = Confirm.ask(
        "Use shared content pool (recommended — reduces API calls)?",
        default=_ev("use_shared_pool", True),
    )

    prefs["twitch_enabled"] = Confirm.ask(
        "Fetch clips from Twitch?",
        default=_ev("twitch_enabled", True),
    )
    prefs["youtube_enabled"] = Confirm.ask(
        "Fetch clips from YouTube?",
        default=_ev("youtube_enabled", True),
    )

    if prefs["use_shared_pool"]:
        refresh_str = Prompt.ask(
            "Pool refresh interval [dim](hours between pool refreshes, 1–24)[/dim]",
            default=str(_ev("pool_refresh_interval_hours", 6)),
        )
        prefs["pool_refresh_interval_hours"] = max(1, min(24, int(refresh_str)))

        reservation_str = Prompt.ask(
            "Clip reservation window [dim](hours a clip is reserved per user, 1–168)[/dim]",
            default=str(_ev("clip_reservation_hours", 48)),
        )
        prefs["clip_reservation_hours"] = max(1, min(168, int(reservation_str)))
    else:
        prefs["pool_refresh_interval_hours"] = _ev("pool_refresh_interval_hours", 6)
        prefs["clip_reservation_hours"]      = _ev("clip_reservation_hours", 48)

    # ── Step 13: Custom Editor ─────────────────────────────────────────────────
    console.rule("[bold]Step 13 — Custom Clip Editor[/bold]")
    console.print(
        "The custom editor (python main.py --edit) lets you trim, caption,\n"
        "crop, and add music to clips interactively before posting.\n"
    )

    ce_quality_str = Prompt.ask(
        "Editor export quality [dim](low / medium / high)[/dim]",
        choices=["low", "medium", "high"],
        default=_ev("custom_editor_output_quality", "medium"),
    )
    prefs["custom_editor_output_quality"] = ce_quality_str

    ce_template_str = Prompt.ask(
        "Default editor template [dim](1–4)[/dim]",
        choices=["1", "2", "3", "4"],
        default=str(_ev("custom_editor_default_template", 1)),
    )
    prefs["custom_editor_default_template"] = int(ce_template_str)

    prefs["custom_editor_auto_queue"] = Confirm.ask(
        "Automatically add exported clips to posting queue?",
        default=_ev("custom_editor_auto_queue", False),
    )

    # ── Step 14: Animated Captions ─────────────────────────────────────────────
    console.rule("[bold]Step 14 — Animated Captions[/bold]")
    console.print(
        "Animated captions generate word-by-word subtitle overlays instead of\n"
        "static burned-in text. Each word animates individually as it's spoken —\n"
        "TikTok-style karaoke captions.\n"
        "[dim]Requires:  pip install faster-whisper[/dim]\n"
    )
    console.print("  [cyan]1[/cyan]  Bounce  — words drop in from above")
    console.print("  [cyan]2[/cyan]  Fade    — words fade in and out smoothly")
    console.print("  [cyan]3[/cyan]  Scale   — words zoom from 150% down to normal size")
    console.print("  [cyan]4[/cyan]  Pop     — words flash bright then settle to colour\n")

    prefs["animated_captions_enabled"] = Confirm.ask(
        "Enable animated word-by-word captions?",
        default=_ev("animated_captions_enabled", False),
    )

    if prefs["animated_captions_enabled"]:
        anim_style_str = Prompt.ask(
            "Animated caption style",
            choices=["1", "2", "3", "4"],
            default=str(_ev("animated_caption_style", 1)),
        )
        prefs["animated_caption_style"] = int(anim_style_str)

        font_size_str = Prompt.ask(
            "Animated caption font size [dim](20–200, 80 recommended for 1080×1920)[/dim]",
            default=str(_ev("animated_caption_font_size", 80)),
        )
        prefs["animated_caption_font_size"] = max(20, min(200, int(font_size_str)))
    else:
        prefs["animated_caption_style"]     = _ev("animated_caption_style", 1)
        prefs["animated_caption_font_size"]  = _ev("animated_caption_font_size", 80)

    # ── Step 15: Smart Hashtags ────────────────────────────────────────────────
    console.rule("[bold]Step 15 — Smart Hashtags[/bold]")
    console.print(
        "Smart hashtags generate a curated, game-specific hashtag set for each\n"
        "post instead of the same hardcoded tags every time. Tags are selected\n"
        "based on the clip's game, platform source, and detected emotional energy.\n"
        "[dim]TikTok recommends 3–8 relevant tags. Too many can reduce reach.[/dim]\n"
    )

    prefs["smart_hashtags_enabled"] = Confirm.ask(
        "Enable smart game-specific hashtag generation?",
        default=_ev("smart_hashtags_enabled", True),
    )

    if prefs["smart_hashtags_enabled"]:
        ht_max_str = Prompt.ask(
            "Maximum hashtags per post [dim](1–30, recommended: 8–10)[/dim]",
            default=str(_ev("smart_hashtags_max", 10)),
        )
        prefs["smart_hashtags_max"] = max(1, min(30, int(ht_max_str)))
    else:
        prefs["smart_hashtags_max"] = _ev("smart_hashtags_max", 10)

    # ── Step 16: Thumbnail Generation ─────────────────────────────────────────
    console.rule("[bold]Step 16 — Thumbnail Generation[/bold]")
    console.print(
        "ClipCast can automatically extract a thumbnail from each processed video\n"
        "by locating the peak-energy frame (loudest moment). The thumbnail gets\n"
        "a creator name and score badge overlay baked in.\n"
        "Used as the cover image for YouTube Shorts uploads.\n"
    )

    prefs["thumbnail_enabled"] = Confirm.ask(
        "Generate thumbnails automatically for each video?",
        default=_ev("thumbnail_enabled", True),
    )

    # ── Step 17: Performance Learner ───────────────────────────────────────────
    console.rule("[bold]Step 17 — Performance Learner[/bold]")
    console.print(
        "The performance learner analyses your post history to find which\n"
        "templates, caption styles, and posting times get the most views.\n"
        "Run [bold]python main.py --learn[/bold] to automatically update your\n"
        "preferences based on real performance data.\n"
        "[dim]Requires at least 3 posts per setting before a recommendation is made.[/dim]\n"
    )

    prefs["learning_enabled"] = Confirm.ask(
        "Allow --learn to write optimised settings back to preferences.yaml?",
        default=_ev("learning_enabled", True),
    )

    if prefs["learning_enabled"]:
        lookback_str = Prompt.ask(
            "Days of post history to analyse [dim](1–365)[/dim]",
            default=str(_ev("learning_lookback_days", 30)),
        )
        prefs["learning_lookback_days"] = max(1, min(365, int(lookback_str)))
    else:
        prefs["learning_lookback_days"] = _ev("learning_lookback_days", 30)

    # best_post_hours is auto-managed by --learn; preserve without prompting
    prefs["best_post_hours"] = _ev("best_post_hours", [])

    # ── Preserve advanced settings not surfaced in wizard steps ───────────────
    # These can be edited directly in preferences.yaml; re-running --setup
    # must not silently discard them.
    prefs["minimum_views"]               = _ev("minimum_views", 100)
    prefs["attribution_format"]          = _ev("attribution_format", "standard")
    prefs["youtube_lookback_days"]       = _ev("youtube_lookback_days", 14)
    prefs["youtube_pool_search_queries"] = _ev(
        "youtube_pool_search_queries",
        [
            "reaction video compilation",
            "gaming highlights best moments",
            "funny moments compilation",
            "shocking moments caught on camera",
            "sports highlights best plays",
        ],
    )

    # ── Summary & Save ─────────────────────────────────────────────────────────
    console.rule("[bold green]Setup Complete![/bold green]")
    console.print("\nYour preferences:\n")
    for key, value in prefs.items():
        console.print(f"  [cyan]{key:<42}[/cyan] {value}")

    if Confirm.ask("\nSave these preferences?", default=True):
        save_preferences(prefs)
        console.print(
            "\n[bold green]All set! Run your first automated cycle with:[/bold green]\n"
            "  [bold]python main.py --test[/bold]   (process clips, save locally, no posting)\n"
            "  [bold]python main.py --run[/bold]    (process clips and post to TikTok)\n"
        )
    else:
        console.print("[yellow]Preferences not saved. Run --setup again when you're ready.[/yellow]")

    return prefs


# ── Quick self-test when run directly ─────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if "--wizard" in sys.argv:
        setup_wizard()
    else:
        try:
            prefs = load_preferences()
            console.print("[green]preferences.yaml loaded and validated successfully.[/green]")
            console.print(f"  clip_length            : {prefs.get('clip_length')}")
            console.print(f"  post_frequency         : {prefs.get('post_frequency')}")
            console.print(f"  posting_times          : {prefs.get('posting_times')}")
            console.print(f"  default_video_template : {prefs.get('default_video_template')}")
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)
