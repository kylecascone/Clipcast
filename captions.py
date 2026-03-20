"""
captions.py
===========
Transcribes clip audio using faster-whisper (runs locally, no API key needed)
and generates TikTok captions in the configured style.

Caption styles:
  1  Hype          — "He had NO idea this was coming | Game | #gaming #fyp #viral"
  2  Storytelling  — "The moment everything changed for Streamer | #fyp"
  3  Question Hook — "Would YOU have done the same thing? | Game | #fyp"
  4  Minimal       — "Streamer on Game | #gaming #fyp"

Each style has 3–4 variations. The rotation state is stored in
caption_state.json to ensure the same variation is never used twice in a row.

SaaS Note:
    The rotation state is stored per-user. Pass user_id to all functions
    and the state file path will be namespaced per user. Currently uses
    a single caption_state.json for single-user mode.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.prompt import Prompt, Confirm

logger = logging.getLogger(__name__)
console = Console()

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CAPTION_STATE_FILE = BASE_DIR / "caption_state.json"

# ══════════════════════════════════════════════════════════════════════════════
# Profanity filter
# ══════════════════════════════════════════════════════════════════════════════

HARD_CENSOR_WORDS: set = {
    # Sexual violence — replace entire word with ***
    "rape", "raping", "raped", "rapist", "molest", "molested",
    "molesting", "grope", "groped",
    # Slurs — replace with ***
    "nigger", "nigga", "faggot", "retard", "chink", "spic",
    "kike", "wetback", "tranny",
    # Explicit sexual
    "fuck", "fucking", "fucked", "fucker", "fucks", "fuckin", "fuk", "fukk",
    "shit", "shitting", "shitty", "bullshit",
    "cock", "dick", "pussy", "cunt", "ass", "asshole",
    "bitch", "whore", "slut",
    "porn", "porno", "pornography",
    # Violence
    "kill", "killing", "murder", "murdered",
    # Drugs
    "cocaine", "heroin", "meth", "fentanyl",
}

SOFT_CENSOR_WORDS: set = {
    # Keep first and last letter, replace middle with asterisks
    "damn", "dammit", "damned", "hell",
    "crap", "crappy",
    "bastard", "bastards",
    "piss", "balls",
    "jackass", "dumbass", "badass",
}


def censor_word(word: str) -> str:
    """
    Return a censored version of the word if it is on the censor list.

    Hard censor (sexual violence, slurs, explicit): replace entire word with ***
    Soft censor (mild profanity): keep first and last letter, replace middle with *

    Partial-match check catches inflected forms (raping, rapist, etc.) for any
    hard-censor root >= 4 characters.

    Non-profanity words are returned unchanged.
    """
    clean = re.sub(r"[^a-zA-Z]", "", word).lower()

    if clean in HARD_CENSOR_WORDS:
        return "***"

    if clean in SOFT_CENSOR_WORDS:
        if len(clean) <= 2:
            return "*" * len(clean)
        return clean[0] + "*" * (len(clean) - 2) + clean[-1]

    # Partial match: catch inflected forms of hard-censor roots
    for banned in HARD_CENSOR_WORDS:
        if len(banned) >= 4 and clean.startswith(banned):
            return "***"

    return word


def censor_caption_text(text: str) -> str:
    """Censor every word in a full caption string."""
    return " ".join(censor_word(w) for w in text.split())


# ── Whisper model size ─────────────────────────────────────────────────────────
# Options: "tiny", "base", "small", "medium", "large-v3"
# "base" is a good balance of speed and accuracy for gaming clips.
# "small" is better quality, "medium" is excellent but slower.
# faster-whisper downloads model weights from Hugging Face on first use (~150MB for base).
WHISPER_MODEL = "base"

# ── faster-whisper device / compute settings ──────────────────────────────────
# On Mac (no NVIDIA GPU): device="cpu", compute_type="int8" (fastest on CPU)
# On Linux with NVIDIA GPU: device="cuda", compute_type="float16"
WHISPER_DEVICE       = "cpu"
WHISPER_COMPUTE_TYPE = "int8"


# ══════════════════════════════════════════════════════════════════════════════
# Caption style definitions
# ══════════════════════════════════════════════════════════════════════════════

# Template variables available in all styles:
#   {streamer}  — creator_name from clip metadata
#   {game}      — game name (from clip or preferences target_games[0])
#   {game_tag}  — game name lowercased, spaces removed, for hashtag use

CAPTION_STYLES: Dict[int, Dict] = {
    1: {
        "name": "Hype",
        "variations": [
            "He had NO idea this was coming | {game} | #gaming #twitch #{game_tag} #fyp #viral",
            "They did NOT expect this... | {game} | #gaming #twitch #{game_tag} #fyp #viral",
            "This moment was UNBELIEVABLE | {game} | #gaming #twitch #{game_tag} #fyp #clip",
            "Nobody saw this coming 💀 | {game} | #gaming #{game_tag} #twitch #fyp #viral",
        ],
    },
    2: {
        "name": "Storytelling",
        "variations": [
            "The moment everything changed for {streamer} | {game} | #gaming #twitch #{game_tag} #fyp",
            "How {streamer} pulled off the impossible | {game} | #gaming #twitch #{game_tag} #fyp",
            "This is why {streamer} is different | {game} | #gaming #{game_tag} #fyp #clips",
            "The play that broke the internet | {game} | #gaming #twitch #{game_tag} #fyp",
        ],
    },
    3: {
        "name": "Question Hook",
        "variations": [
            "Would YOU have done the same thing? | {game} | #gaming #twitch #{game_tag} #fyp",
            "Could you pull this off in {game}? | #gaming #twitch #{game_tag} #fyp #viral",
            "What would you do in this situation? | {game} | #gaming #{game_tag} #fyp #clips",
            "How would YOU react to this? | {game} | #gaming #twitch #{game_tag} #fyp",
        ],
    },
    4: {
        "name": "Minimal",
        "variations": [
            "{streamer} on {game} | #gaming #twitch #{game_tag} #fyp",
            "{streamer} | {game} highlights | #gaming #{game_tag} #fyp #clips",
            "{game} clip | #gaming #{game_tag} #twitch #fyp",
        ],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# Rotation state
# ══════════════════════════════════════════════════════════════════════════════

def _load_rotation_state(user_id: int = 1) -> Dict:
    """Load caption rotation state from caption_state.json."""
    if not CAPTION_STATE_FILE.exists():
        return {}
    try:
        with open(CAPTION_STATE_FILE) as f:
            state = json.load(f)
        return state.get(str(user_id), {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_rotation_state(state: Dict, user_id: int = 1) -> None:
    """Save caption rotation state to caption_state.json."""
    all_state = {}
    if CAPTION_STATE_FILE.exists():
        try:
            with open(CAPTION_STATE_FILE) as f:
                all_state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    all_state[str(user_id)] = state
    with open(CAPTION_STATE_FILE, "w") as f:
        json.dump(all_state, f, indent=2)


def _next_variation_index(style_id: int, user_id: int = 1) -> int:
    """
    Return the next variation index for a style, never repeating consecutively.

    Args:
        style_id: Caption style number (1–4).
        user_id: User ID for state namespacing.

    Returns:
        Index into CAPTION_STYLES[style_id]["variations"].
    """
    state = _load_rotation_state(user_id)
    key = str(style_id)

    last_used   = state.get(key, {}).get("last_index", -1)
    num_variants = len(CAPTION_STYLES[style_id]["variations"])

    # Move to the next index, skip if it matches the last used one
    next_idx = (last_used + 1) % num_variants
    if next_idx == last_used and num_variants > 1:
        next_idx = (next_idx + 1) % num_variants

    state[key] = {"last_index": next_idx}
    _save_rotation_state(state, user_id)
    return next_idx


# ══════════════════════════════════════════════════════════════════════════════
# Caption generation
# ══════════════════════════════════════════════════════════════════════════════

def _make_game_tag(game_name: str) -> str:
    """Convert a game name to a hashtag-safe string (no spaces, lowercase)."""
    return re.sub(r"[^a-z0-9]", "", game_name.lower())


def generate_caption(
    clip: Dict[str, Any],
    style_id: int,
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
) -> str:
    """
    Generate a TikTok caption for a clip using the specified style.

    Automatically selects the next rotation variation (never repeats consecutively).

    Args:
        clip: Clip dict with at least 'creator_name' and optionally 'game'.
        style_id: Caption style number (1–4).
        user_prefs: User preferences (for fallback game name).
        user_id: Used for rotation state namespacing.

    Returns:
        Formatted caption string ready to post to TikTok.
    """
    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    if style_id not in CAPTION_STYLES:
        logger.warning("Unknown caption style %d, defaulting to style 1.", style_id)
        style_id = 1

    # Extract template variables
    streamer = clip.get("creator_name") or "Streamer"
    game = (
        clip.get("game") or
        (user_prefs.get("target_games") or ["Gaming"])[0]
    )
    game_tag = _make_game_tag(game)

    # Pick the next variation
    idx = _next_variation_index(style_id, user_id)
    template = CAPTION_STYLES[style_id]["variations"][idx]

    caption = template.format(
        streamer=streamer,
        game=game,
        game_tag=game_tag,
    )

    # ── Attribution suffix — always appended, cannot be disabled ──────────────
    # This is a legal and ethical safeguard. Streamer credit must always appear
    # in the caption regardless of style. Uses "ft." when the streamer name is
    # already present in the caption text, "clip via" otherwise.
    streamer_lower = streamer.lower()
    if streamer_lower in caption.lower():
        attribution = f"ft. {streamer}"
    else:
        attribution = f"clip via {streamer}"

    caption = f"{caption} | {attribution}"

    logger.debug(
        "Generated caption (style=%d var=%d): %s",
        style_id, idx, caption[:80],
    )
    return caption


def confirm_or_edit_caption(
    caption: str,
    clip: Dict[str, Any],
) -> str:
    """
    In manual mode with immediate posting, show the generated caption and
    let the user confirm or edit it before posting.

    Args:
        caption: Auto-generated caption string.
        clip: The clip being captioned (for context display).

    Returns:
        The final caption string (either confirmed or edited by the user).
    """
    console.print("\n[bold cyan]Caption Review[/bold cyan]")
    console.print(f"Clip:  [bold]{clip.get('title', 'Untitled')}[/bold]")
    console.print(f"\nAuto-generated caption:\n[yellow]{caption}[/yellow]\n")

    if Confirm.ask("Use this caption?", default=True):
        return caption

    edited = Prompt.ask(
        "Enter your custom caption",
        default=caption,
    )
    return edited


# ══════════════════════════════════════════════════════════════════════════════
# Whisper transcription (via faster-whisper)
# ══════════════════════════════════════════════════════════════════════════════

# Module-level model cache — loaded once and reused across calls in the same process.
_whisper_model_cache: Dict[str, Any] = {}


def _load_whisper_model(model_size: str) -> Any:
    """
    Load and cache a faster-whisper WhisperModel.

    Downloads weights from Hugging Face on first use (~150MB for "base").
    Subsequent calls return the cached model instantly.

    Args:
        model_size: One of "tiny", "base", "small", "medium", "large-v3".

    Returns:
        A loaded WhisperModel instance.

    Raises:
        ImportError: If faster-whisper is not installed.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ImportError(
            "faster-whisper is not installed.\n"
            "Install with:  pip install faster-whisper"
        )

    cache_key = f"{model_size}_{WHISPER_DEVICE}_{WHISPER_COMPUTE_TYPE}"
    if cache_key not in _whisper_model_cache:
        logger.info(
            "Loading faster-whisper model '%s' (device=%s, compute=%s)...",
            model_size, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
        )
        _whisper_model_cache[cache_key] = WhisperModel(
            model_size,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
        logger.info("Model loaded.")
    return _whisper_model_cache[cache_key]


def transcribe_clip(
    local_path: str,
    model_size: str = WHISPER_MODEL,
) -> Optional[str]:
    """
    Transcribe the audio from a clip using faster-whisper (local).

    Runs completely locally — no API key or internet needed after the first
    run (model weights are cached by Hugging Face hub after download).

    Args:
        local_path: Path to the video or audio file.
        model_size: Whisper model size ("tiny", "base", "small", "medium", "large-v3").
                    Larger = more accurate but slower and uses more RAM.

    Returns:
        Transcribed text string, or None if transcription fails.
    """
    if not Path(local_path).exists():
        logger.error("Transcription: file not found at '%s'", local_path)
        return None

    try:
        model = _load_whisper_model(model_size)

        logger.info("Transcribing '%s'...", Path(local_path).name)
        # faster-whisper returns a generator — consume it to get all text
        segments, _info = model.transcribe(local_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()

        logger.debug("Transcription: %s", text[:100])
        return text if text else None

    except ImportError as e:
        logger.warning("%s", e)
        return None
    except Exception as e:
        logger.error("Whisper transcription failed: %s", e)
        return None


def generate_subtitle_segments(
    local_path: str,
    model_size: str = WHISPER_MODEL,
) -> List[Dict[str, Any]]:
    """
    Generate time-aligned subtitle segments using faster-whisper.

    Returns a list of segments suitable for burning into the video via FFmpeg.
    Each segment dict has: start (float), end (float), text (str).

    Args:
        local_path: Path to the video file.
        model_size: Whisper model size.

    Returns:
        List of subtitle segment dicts, or empty list on failure.
    """
    if not Path(local_path).exists():
        return []

    try:
        model = _load_whisper_model(model_size)

        # faster-whisper returns a lazy generator — must consume before closing
        segments_gen, _info = model.transcribe(local_path, beam_size=5)
        segments = []
        for seg in segments_gen:
            text = seg.text.strip()
            if text:
                segments.append({
                    "start": seg.start,
                    "end":   seg.end,
                    "text":  text,
                })

        logger.debug(
            "Generated %d subtitle segments for '%s'",
            len(segments), Path(local_path).name,
        )
        return segments

    except ImportError as e:
        logger.warning("%s", e)
        return []
    except Exception as e:
        logger.error("Subtitle generation failed: %s", e)
        return []


def write_srt_file(
    segments: List[Dict[str, Any]],
    output_path: str,
) -> bool:
    """
    Write a list of subtitle segments to an .srt file.

    The .srt file can be passed to FFmpeg to burn subtitles into the video.

    Args:
        segments: List of {'start': float, 'end': float, 'text': str} dicts.
        output_path: Where to write the .srt file (e.g. "clips/raw/clip.srt").

    Returns:
        True on success, False on failure.
    """
    if not segments:
        logger.warning("No segments to write to SRT file.")
        return False

    def _format_timestamp(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, start=1):
                f.write(f"{i}\n")
                f.write(f"{_format_timestamp(seg['start'])} --> {_format_timestamp(seg['end'])}\n")
                f.write(f"{seg['text']}\n\n")

        logger.debug("SRT file written to '%s' (%d lines)", output_path, len(segments))
        return True

    except OSError as e:
        logger.error("Failed to write SRT file '%s': %s", output_path, e)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Animated word-by-word captions (ASS subtitle format)
# ══════════════════════════════════════════════════════════════════════════════

# Four animation styles for word-by-word subtitle rendering.
# Each maps to an ASS style block and per-word event tags.
ANIMATED_CAPTION_STYLE_NAMES = {
    1: "Bounce",   # words drop down from above with spring feel
    2: "Fade",     # words fade in and out smoothly
    3: "Scale",    # words zoom in from 150% → 100%
    4: "Pop",      # words flash bright white then settle to yellow
}

_ASS_HEADER_TEMPLATE = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 1
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# ASS colour format: &HAABBGGRR (alpha, blue, green, red — big-endian hex)
_STYLE_DEFINITIONS: Dict[int, str] = {
    # Bounce: bold yellow text, thick black border
    1: "Style: AnimCaption,Arial,{size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,20,20,60,1",
    # Fade: white text, subtle grey border
    2: "Style: AnimCaption,Arial,{size},&H00FFFFFF,&H000000FF,&H00333333,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,60,1",
    # Scale: bold cyan text, black border
    3: "Style: AnimCaption,Arial,{size},&H00FFFF00,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,20,20,60,1",
    # Pop: white text with bold, high contrast border
    4: "Style: AnimCaption,Arial,{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,20,20,60,1",
}

# Per-word event tag templates — {fy} is the font size reference for \\move
_WORD_TAGS: Dict[int, str] = {
    1: r"{\an2\move(%cx%,%cy_below%,%cx%,%cy%,0,150)\fad(100,80)}",   # Bounce down-to-up
    2: r"{\an2\fad(180,120)}",                                          # Fade in/out
    3: r"{\an2\t(\fscx150\fscy150)\t(0,200,\fscx100\fscy100)\fad(80,80)}",  # Scale
    4: r"{\an2\1c&H00FFFFFF&\t(0,100,\1c&H0000FFFF&)\fad(50,100)}",    # Pop yellow flash
}


def _ass_time(seconds: float) -> str:
    """Convert seconds to ASS timestamp format  H:MM:SS.cc"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    cs = int((s - int(s)) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def generate_word_segments(
    local_path: str,
    model_size: str = WHISPER_MODEL,
) -> List[Dict[str, Any]]:
    """
    Generate word-level time-aligned segments using faster-whisper.

    Returns individual word timings (not sentence-level). These are used
    by ``generate_animated_ass()`` to animate each word independently.

    Args:
        local_path: Path to the video or audio file.
        model_size: Whisper model size.

    Returns:
        List of dicts: [{start, end, word}]. Empty list on failure.
    """
    if not Path(local_path).exists():
        return []

    try:
        model = _load_whisper_model(model_size)
        segments_gen, _info = model.transcribe(
            local_path,
            beam_size=1,           # greedy decoding — 5x faster than beam_size=5
            word_timestamps=True,
            vad_filter=True,       # skip silence/music segments faster
        )
        words = []
        for seg in segments_gen:
            for w in (seg.words or []):
                word = w.word.strip()
                if word:
                    words.append({
                        "start": round(w.start, 3),
                        "end":   round(w.end, 3),
                        "word":  censor_word(word),
                    })
        logger.debug("generate_word_segments: %d words from '%s'",
                     len(words), Path(local_path).name)
        return words

    except ImportError as e:
        logger.warning("%s", e)
        return []
    except Exception as e:
        logger.error("Word-level segment generation failed: %s", e)
        return []


def generate_animated_ass(
    word_segments: List[Dict[str, Any]],
    animation_style: int = 1,
    video_width: int = 1080,
    video_height: int = 1920,
    font_size: int = 80,
) -> str:
    """
    Generate an ASS subtitle string with animated word-by-word captions.

    Each word appears individually timed, with the animation style applied
    as ASS override tags. The resulting string can be written to a .ass file
    and passed to FFmpeg's ``subtitles=`` filter.

    Animation styles:
        1 — Bounce: words drop in from slightly above, spring motion.
        2 — Fade:   words fade in and out smoothly.
        3 — Scale:  words scale from 150% → 100% on entry.
        4 — Pop:    words flash white then settle to yellow.

    Args:
        word_segments: Output of ``generate_word_segments()``.
        animation_style: 1–4 (default 1).
        video_width:  Output video width in pixels (default 1080).
        video_height: Output video height in pixels (default 1920).
        font_size:    Base font size in points (default 80).

    Returns:
        ASS subtitle content as a string. Empty string if no segments.
    """
    if not word_segments:
        return ""

    if animation_style not in _STYLE_DEFINITIONS:
        logger.warning("Unknown animation style %d, defaulting to 1", animation_style)
        animation_style = 1

    style_line = _STYLE_DEFINITIONS[animation_style].replace("{size}", str(font_size))
    header = _ASS_HEADER_TEMPLATE.format(
        width=video_width,
        height=video_height,
        style_line=style_line,
    )

    # Centre-X for all words; Y position = 85% down the frame
    cx = video_width // 2
    cy = int(video_height * 0.85)
    cy_below = cy + 40   # For bounce animation — start 40px below final pos

    tag_template = _WORD_TAGS[animation_style]
    events: List[str] = []

    for w in word_segments:
        start_ts = _ass_time(w["start"])
        # Keep word visible for its duration plus a short tail (100ms)
        end_ts   = _ass_time(w["end"] + 0.10)

        tags = (
            tag_template
            .replace("%cx%", str(cx))
            .replace("%cy%", str(cy))
            .replace("%cy_below%", str(cy_below))
        )
        text = censor_word(w["word"]).upper()   # Uppercase = TikTok style; profanity censored
        events.append(
            f"Dialogue: 0,{start_ts},{end_ts},AnimCaption,,0,0,0,,{tags}{text}"
        )

    return header + "\n".join(events) + "\n"


def write_ass_file(ass_content: str, output_path: str) -> bool:
    """
    Write ASS subtitle content to a file.

    Args:
        ass_content: String returned by ``generate_animated_ass()``.
        output_path: Full path to write the .ass file.

    Returns:
        True on success, False on failure.
    """
    if not ass_content:
        logger.warning("write_ass_file: empty ASS content — nothing to write.")
        return False
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        logger.debug("ASS file written to '%s'", output_path)
        return True
    except OSError as e:
        logger.error("Failed to write ASS file '%s': %s", output_path, e)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Smart hashtag generation
# ══════════════════════════════════════════════════════════════════════════════

# Game-specific hashtag sets — extend as needed
_GAME_HASHTAGS: Dict[str, List[str]] = {
    "valorant":          ["#valorant", "#valorantclips", "#valoranthighlights"],
    "fortnite":          ["#fortnite", "#fortniteclips", "#fn"],
    "minecraft":         ["#minecraft", "#minecraftclips", "#minecraftmemes"],
    "apex legends":      ["#apex", "#apexlegends", "#apexclips"],
    "call of duty":      ["#cod", "#warzone", "#callofduty", "#codclips"],
    "warzone":           ["#warzone", "#cod", "#warzoneClips"],
    "league of legends": ["#leagueoflegends", "#lol", "#lolclips"],
    "overwatch":         ["#overwatch", "#ow2", "#overwatchclips"],
    "counter-strike":    ["#cs2", "#csgo", "#counterstrike"],
    "cs2":               ["#cs2", "#csgo", "#counterstrike"],
    "gta":               ["#gta", "#gtav", "#grandtheftauto", "#gtaonline"],
    "rocket league":     ["#rocketleague", "#rl", "#rocketleagueclips"],
    "pubg":              ["#pubg", "#pubgmobile", "#battlegrounds"],
    "fall guys":         ["#fallguys", "#fallguysclips"],
    "among us":          ["#amongus", "#amogus"],
    "terraria":          ["#terraria", "#terrariagame"],
}

# Platform-specific tags
_PLATFORM_HASHTAGS: Dict[str, List[str]] = {
    "twitch":  ["#twitch", "#twitchclips", "#twitchstreamer", "#livestream"],
    "youtube": ["#youtube", "#youtuber", "#youtubegaming"],
}

# Base viral tags — always included
_BASE_HASHTAGS = ["#gaming", "#gamer", "#fyp", "#foryoupage", "#clips", "#gamingclips"]

# Title-keyword → emotion tags
_EMOTION_TAGS: Dict[str, str] = {
    "clutch":   "#clutch",
    "rage":     "#ragequit",
    "funny":    "#funny",
    "fail":     "#fail",
    "insane":   "#insane",
    "crazy":    "#crazy",
    "epic":     "#epic",
    "hack":     "#hacker",
    "noob":     "#noob",
    "pro":      "#pro",
    "win":      "#winner",
    "gg":       "#gg",
}


def generate_smart_hashtags(
    clip: Dict[str, Any],
    user_prefs: Optional[Dict] = None,
    max_tags: int = 15,
) -> List[str]:
    """
    Generate a curated, ranked list of hashtags for a clip.

    Priority order:
        1. Game-specific hashtags (up to 3)
        2. Platform hashtags (up to 2)
        3. Emotion-based hashtags from title keywords (up to 3)
        4. Base viral hashtags (fyp, gaming, etc.) (up to 5)
        5. Viral-threshold bonus tags for high view_count clips (up to 2)

    Total capped at ``max_tags`` (default 15).

    Args:
        clip:      Clip dict with 'game', 'source', 'title', 'view_count'.
        user_prefs: User preferences (for target_games fallback).
        max_tags:  Maximum number of hashtags to return.

    Returns:
        List of hashtag strings (each starting with #), deduplicated.
    """
    tags: List[str] = []
    seen: set = set()

    def _add(tag: str) -> None:
        t = tag.strip().lower()
        if t and t not in seen and len(tags) < max_tags:
            seen.add(t)
            tags.append(tag)

    # ── 1. Game-specific tags ─────────────────────────────────────────────────
    # Resolve game name: clip dict first, then user_prefs fallback
    game_raw = clip.get("game") or ""
    if not game_raw and user_prefs:
        target_games = user_prefs.get("target_games") or []
        game_raw = target_games[0] if target_games else ""

    game_key = game_raw.lower().strip()
    game_tag = _make_game_tag(game_raw or "gaming")

    # Look up known game hashtags; try partial match only when game_key is non-empty
    game_tags_found = _GAME_HASHTAGS.get(game_key, [])
    if not game_tags_found and game_key:
        for known_game, known_tags in _GAME_HASHTAGS.items():
            if known_game in game_key or game_key in known_game:
                game_tags_found = known_tags
                break
    # Always add generic game hashtag even if lookup fails
    if not game_tags_found:
        game_tags_found = [f"#{game_tag}"] if game_tag else []

    for t in game_tags_found[:3]:
        _add(t)

    # ── 2. Platform tags ──────────────────────────────────────────────────────
    source = (clip.get("source") or "").lower()
    for t in _PLATFORM_HASHTAGS.get(source, [])[:2]:
        _add(t)

    # ── 3. Emotion tags from title keywords ───────────────────────────────────
    title_lower = (clip.get("title") or "").lower()
    emotion_count = 0
    for keyword, emotion_tag in _EMOTION_TAGS.items():
        if keyword in title_lower and emotion_count < 3:
            _add(emotion_tag)
            emotion_count += 1

    # ── 4. Base viral tags ────────────────────────────────────────────────────
    for t in _BASE_HASHTAGS:
        _add(t)

    # ── 5. High view count bonus tags ─────────────────────────────────────────
    view_count = int(clip.get("view_count") or 0)
    if view_count >= 50_000:
        _add("#viral")
        _add("#trending")
    elif view_count >= 10_000:
        _add("#viral")

    return tags[:max_tags]


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing captions.py...")
    print()

    try:
        from preferences import load_preferences
        prefs = load_preferences()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Test clip
    clip = {
        "title": "Insane clutch play",
        "creator_name": "shroud",
        "game": "Valorant",
        "mode": "auto",
        "source": "twitch",
    }

    print(f"{'Style':<20}  Caption")
    print("-" * 80)

    for style_id in range(1, 5):
        style_name = CAPTION_STYLES[style_id]["name"]
        # Generate 2 variations per style to test rotation
        for _ in range(2):
            caption = generate_caption(clip, style_id=style_id, user_prefs=prefs)
            print(f"  [{style_id}] {style_name:<15}  {caption}")

    print()
    print("Rotation state saved to caption_state.json")
    print()

    # Test caption confirmation (interactive)
    if "--interactive" in sys.argv:
        print("\nTesting interactive caption confirmation...")
        final = confirm_or_edit_caption(
            generate_caption(clip, style_id=1, user_prefs=prefs),
            clip,
        )
        print(f"\nFinal caption: {final}")

    # Test SRT writing
    print("Testing SRT file writing...")
    test_segments = [
        {"start": 0.0,  "end": 2.5,  "text": "He had no idea..."},
        {"start": 2.5,  "end": 5.0,  "text": "this was coming!"},
        {"start": 5.0,  "end": 8.0,  "text": "What a play!"},
    ]
    srt_path = "/tmp/clipcast_test.srt"
    success = write_srt_file(test_segments, srt_path)
    if success:
        with open(srt_path) as f:
            print(f.read())
        Path(srt_path).unlink()
        print("SRT file written and cleaned up — OK")
    else:
        print("SRT file write failed")

    # Test animated ASS captions
    print("\nTesting animated ASS captions (all 4 styles)...")
    word_segs = [
        {"start": 0.0,  "end": 0.4,  "word": "HE"},
        {"start": 0.4,  "end": 0.7,  "word": "HAD"},
        {"start": 0.7,  "end": 1.0,  "word": "NO"},
        {"start": 1.0,  "end": 1.4,  "word": "IDEA"},
        {"start": 2.0,  "end": 2.3,  "word": "THIS"},
        {"start": 2.3,  "end": 2.8,  "word": "WAS"},
        {"start": 2.8,  "end": 3.2,  "word": "COMING"},
    ]
    ass_path = "/tmp/clipcast_test.ass"
    for style_id in range(1, 5):
        style_name = ANIMATED_CAPTION_STYLE_NAMES[style_id]
        ass_content = generate_animated_ass(word_segs, animation_style=style_id)
        assert ass_content, f"generate_animated_ass returned empty for style {style_id}"
        ok = write_ass_file(ass_content, ass_path)
        assert ok, f"write_ass_file failed for style {style_id}"
        line_count = ass_content.count("\n")
        print(f"  Style {style_id} ({style_name}): {line_count} lines — OK")
    Path(ass_path).unlink(missing_ok=True)
    print("ASS animated captions: OK")

    # Test smart hashtag generation
    print("\nTesting smart hashtag generation...")
    test_clips_ht = [
        {"title": "Insane clutch play", "game": "Valorant",  "source": "twitch",  "view_count": 75000, "creator_name": "shroud"},
        {"title": "Funny moment",       "game": "Fortnite",  "source": "youtube", "view_count": 5000,  "creator_name": "ninja"},
        {"title": "Rage quit",          "game": "Unknown",   "source": "twitch",  "view_count": 500,   "creator_name": "streamerbob"},
    ]
    for tc in test_clips_ht:
        ht = generate_smart_hashtags(tc, user_prefs=prefs)
        print(f"  '{tc['title']}' ({tc['game']}, {tc['view_count']:,} views):")
        print(f"    {' '.join(ht)}")
        assert len(ht) > 0, "Expected at least 1 hashtag"
        assert all(h.startswith("#") for h in ht), "All hashtags should start with #"
    print("Smart hashtag generation: OK")

    print("\nCaptions test complete.")
    print(
        "\nNote: faster-whisper transcription requires a video file and downloads\n"
        "model weights (~150MB for 'base') from Hugging Face on first use.\n"
        "Test it with:\n"
        "  python captions.py --transcribe path/to/video.mp4"
    )

    if "--transcribe" in sys.argv:
        idx = sys.argv.index("--transcribe")
        if idx + 1 < len(sys.argv):
            path = sys.argv[idx + 1]
            print(f"\nTranscribing {path} with faster-whisper...")
            text = transcribe_clip(path)
            if text:
                print(f"Transcript: {text}")
            else:
                print("Transcription failed. Check that faster-whisper is installed:\n"
                      "  pip install faster-whisper")
