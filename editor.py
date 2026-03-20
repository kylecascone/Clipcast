"""
editor.py
=========
Orchestrates the full video editing pipeline for a package:

  1. Download raw clips (yt-dlp for URLs, copy for local files).
  2. Crop each clip to 9:16 vertical (center crop).
  3. Apply the package's template (overlays, credit cards, transitions).
  4. Burn in Whisper-generated captions.
  5. Normalize audio across all clips.
  6. Concatenate and export final MP4 to clips/processed/.

All settings are passed in as parameters — nothing is hardcoded.
The output filename is timestamped to prevent collisions.

Dependencies:
  - yt-dlp    : for downloading Twitch clips and YouTube videos
  - moviepy   : for video compositing, text overlays, and transitions
  - FFmpeg    : called via subprocess for audio normalization and caption burning
  - openai-whisper : for subtitle generation (via captions.py)

Install:
  pip install -r requirements.txt
  brew install ffmpeg   (macOS) or sudo apt install ffmpeg (Linux)
"""

# ── ImageMagick discovery — must run before any MoviePy import ────────────────
# ImageMagick v7 (Homebrew on macOS) uses the `magick` binary instead of
# `convert`. We probe common install paths first, then fall back to PATH lookup.
import os
import subprocess


def _find_imagemagick() -> str:
    for path in [
        "/usr/local/bin/magick",
        "/opt/homebrew/bin/magick",
        "/usr/local/bin/convert",
        "/opt/homebrew/bin/convert",
    ]:
        if os.path.exists(path):
            return path
    result = subprocess.run(["which", "magick"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    result = subprocess.run(["which", "convert"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return "magick"


_IM_BINARY = _find_imagemagick()
os.environ["IMAGEMAGICK_BINARY"] = _IM_BINARY
# ──────────────────────────────────────────────────────────────────────────────

import logging
import re
import shutil
import tempfile
import textwrap
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Pillow ≥10 compatibility ───────────────────────────────────────────────────
# PIL.Image.ANTIALIAS was removed in Pillow 10.0.0. The patch is applied once
# at process startup in main.py (before any moviepy imports). Nothing needed here.

# ── Tell MoviePy which binary was found ───────────────────────────────────────
from moviepy.config import change_settings  # noqa: E402
change_settings({"IMAGEMAGICK_BINARY": _IM_BINARY})

logger = logging.getLogger(__name__)

# ── Font resolver ──────────────────────────────────────────────────────────────
# Returns an absolute path to a usable bold/sans font on macOS and Linux.
# Called at module level so all font constants resolve once at import time.

def find_font() -> Optional[str]:
    """Return the first existing font path for the current OS, or None."""
    import platform as _platform
    if _platform.system() == "Darwin":
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Impact.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
        ]
    else:
        # Linux / Railway (Nix dejavu_fonts package or distro defaults)
        candidates = [
            "/run/current-system/sw/share/X11/fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    return next((p for p in candidates if Path(p).exists()), None)


_FONT_FALLBACK = find_font()

# ImageMagick v7 cannot resolve font names via fontconfig on macOS;
# map logical names to absolute paths using find_font() as the fallback.
_FONT_MAP: Dict[str, str] = {
    "Arial":      _FONT_FALLBACK or "Arial",
    "Arial Bold": _FONT_FALLBACK or "Arial",
    "Impact":     _FONT_FALLBACK or "Impact",
    "Helvetica":  _FONT_FALLBACK or "Helvetica",
}


def _resolve_font(name: Optional[str]) -> Optional[str]:
    """
    Return an absolute font file path for *name*.

    On macOS, ImageMagick v7 cannot resolve font names through fontconfig,
    so we map the most common names used in templates to their Supplemental
    font paths. If *name* is already an absolute path that exists it is
    returned as-is. Falls back to _FONT_FALLBACK if nothing matches.
    """
    if not name:
        return _FONT_FALLBACK
    # Already an absolute path?
    if os.path.isabs(name) and os.path.exists(name):
        return name
    resolved = _FONT_MAP.get(name)
    if resolved and Path(resolved).exists():
        return resolved
    return _FONT_FALLBACK or name


# ── Profanity filter (imported once at module level) ──────────────────────────
from captions import censor_word  # noqa: E402

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
RAW_DIR       = BASE_DIR / "clips" / "raw"
PROCESSED_DIR = BASE_DIR / "clips" / "processed"
COMPILED_DIR  = BASE_DIR / "clips" / "compiled"

# ── Video output constants ─────────────────────────────────────────────────────
TARGET_WIDTH  = 1080
TARGET_HEIGHT = 1920
TARGET_FPS    = 30

# ── Audio normalization target (EBU R128 broadcast standard) ──────────────────
AUDIO_TARGET_LUFS      = -14.0   # TikTok's recommended integrated loudness
AUDIO_TARGET_LRA       = 11.0    # Loudness range
AUDIO_TARGET_TRUE_PEAK = -1.0    # Max true peak (dBTP)


# ── Hardware encoder detection ────────────────────────────────────────────────
# h264_videotoolbox (Apple hardware H.264) is 10-50x faster than libx264 on Mac.
# Detected once at process start via a tiny null-source test encode.
_HW_ENCODE_AVAILABLE: Optional[bool] = None


def _get_encoder_flags() -> List[str]:
    """
    Return FFmpeg video encoder flags.
    Uses h264_videotoolbox (hardware) if available on macOS, else libx264 veryfast.
    """
    global _HW_ENCODE_AVAILABLE
    if _HW_ENCODE_AVAILABLE is None:
        try:
            r = subprocess.run(
                [
                    "ffmpeg", "-f", "lavfi", "-i", "nullsrc=s=128x128:r=1",
                    "-t", "0.1", "-c:v", "h264_videotoolbox",
                    "-f", "null", "-",
                ],
                capture_output=True, timeout=10,
            )
            _HW_ENCODE_AVAILABLE = r.returncode == 0
        except Exception:
            _HW_ENCODE_AVAILABLE = False
        logger.debug("Hardware encoder (videotoolbox): %s", _HW_ENCODE_AVAILABLE)

    if _HW_ENCODE_AVAILABLE:
        return ["-c:v", "h264_videotoolbox", "-b:v", "4000k"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def process_package(
    package: Dict[str, Any],
    test_mode: bool = False,
    user_prefs: Optional[Dict] = None,
) -> Optional[str]:
    """
    Run the full editing pipeline for a package and return the output path.

    Args:
        package:    Package dict from compiler.py (must include 'clips' list).
        test_mode:  If True, process locally but do not post to TikTok.
        user_prefs: User preferences dict. If None, loaded from preferences.yaml.
                    Used for moments detection, branding, and intro/outro.

    Returns:
        Absolute path to the final processed MP4 file, or None on failure.
    """
    if user_prefs is None:
        try:
            from preferences import load_preferences
            user_prefs = load_preferences()
        except Exception:
            user_prefs = {}

    clips = package.get("clips", [])
    template_id   = package.get("template", 1)
    caption_style = package.get("caption_style", 1)
    mode = package.get("mode", "auto")

    if not clips:
        logger.error("process_package called with empty clips list.")
        return None

    logger.info(
        "Starting edit: %d clip(s), template=%d, mode=%s",
        len(clips), template_id, mode,
    )

    # Ensure output directories exist
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from templates import get_template
        template_spec = get_template(template_id)

        # Step 1: Download / locate raw clips
        local_paths = _acquire_raw_clips(clips)
        if not local_paths:
            logger.error("No clips could be downloaded/located.")
            return None

        # Step 2: Probe each clip's actual duration and update clip metadata
        for i, (clip, lpath) in enumerate(zip(clips, local_paths)):
            if lpath:
                duration = _probe_duration(lpath)
                if duration:
                    clip["duration"] = duration
                    clip["local_path"] = str(lpath)

        # Step 2b: Enforce duration limits (reject <15 s, cap >90 s)
        local_paths = _enforce_duration_limits(clips, local_paths)

        # Step 2c: Music detection — flag clips that likely contain music
        _run_music_detection(clips, local_paths)

        # Step 3: Process each clip (crop to 9:16, overlays, credit card)
        processed_segments = _process_segments(
            clips=clips,
            local_paths=local_paths,
            template_spec=template_spec,
        )

        if not processed_segments:
            logger.error("All clips failed during segment processing.")
            return None

        # Step 4: Concatenate segments with transitions
        combined_path = _concatenate_segments(
            segments=processed_segments,
            template_spec=template_spec,
        )
        if not combined_path:
            return None

        # Step 5: Normalize audio
        normalized_path = _normalize_audio(combined_path)
        if not normalized_path:
            normalized_path = combined_path  # Fall back to un-normalized

        # Step 6: Burn word-by-word captions if enabled
        caption_text = package.get("caption_text")   # TikTok description
        final_path = _burn_subtitles(
            clips=clips,
            local_paths=local_paths,
            video_path=normalized_path,
            template_spec=template_spec,
            user_prefs=user_prefs,
            layout_type="gaming",
        )
        if not final_path:
            final_path = normalized_path  # Fall back if subtitle burn fails

        # Step 6.5: Hook text card — white rounded rectangle centered above video frame
        hook_text = (clips[0].get("viral_title") or clips[0].get("title", "")) if clips else ""
        # Compute fg_y by probing the first source clip (same math as build_layout_fullframe)
        hook_fg_y = 400  # fallback: assume typical 16:9 clip
        src_for_hook = next((p for p in local_paths if p and p.exists()), None)
        if src_for_hook:
            try:
                _hprobe = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0",
                     str(src_for_hook)],
                    text=True, timeout=10,
                ).strip().split(",")
                _src_w, _src_h = int(_hprobe[0]), int(_hprobe[1])
                _fg_h = min(int(OUTPUT_W * _src_h / _src_w), OUTPUT_H)
                hook_fg_y = (OUTPUT_H - _fg_h) // 2
            except Exception:
                pass
        # If the source is already vertical (e.g. a YouTube Short that is
        # 1080×1920) the math above yields fg_y=0.  Fall back to the standard
        # 16:9-clip-in-1920-canvas value so the card lands in the blur zone.
        if hook_fg_y < 50:
            hook_fg_y = 420
        # Always apply hook text — never skip it
        if is_weak_title(hook_text):
            raw_title = (clips[0].get("title", "") if clips else "")
            logger.debug("Hook text '%s' is weak — trying raw title '%s'", hook_text[:40], raw_title[:40])
            hook_text = raw_title if raw_title else hook_text
        if not hook_text:
            hook_text = "You need to see this 👀"

        logger.info("Hook text (fg_y=%d): %s", hook_fg_y, hook_text[:60])
        print(f"  Hook text: {hook_text[:60]}")
        try:
            hook_out = final_path.parent / f"{final_path.stem}_hooked.mp4"
            success = add_hook_text_to_video(str(final_path), hook_text, str(hook_out), fg_y=hook_fg_y)
            if success and hook_out.exists():
                import os as _os
                _os.replace(str(hook_out), str(final_path))
                print(f"  Hook applied: {hook_text[:60]}")
            else:
                logger.warning("Hook text overlay failed — continuing without it.")
                print(f"  Hook failed for: {hook_text[:60]}")
        except Exception as _hook_exc:
            logger.warning("Hook text exception: %s", _hook_exc)
            print(f"  Hook error: {_hook_exc}")

        # Step 6b: Apply branding overlay (watermark + channel name)
        branded = _apply_branding_step(final_path, user_prefs)
        if branded:
            final_path = Path(branded)

        # Step 6c: Prepend intro / append outro if configured
        with_bookends = _add_intro_outro(final_path, user_prefs)
        if with_bookends:
            final_path = Path(with_bookends)

        # Step 7: Move to final output location
        output_path = _export_final(final_path, mode=mode)

        # Step 8: Generate thumbnail + hashtags
        creator_name = clips[0].get("creator_name", "") if clips else ""
        thumbnail_path = output_path.replace(".mp4", "_thumbnail.jpg")
        if generate_thumbnail(output_path, thumbnail_path):
            logger.info("Thumbnail saved → %s", thumbnail_path)

        # Generate platform-optimised hashtags
        clip_data_for_tags = clips[0] if clips else {}
        try:
            from hashtags import generate_hashtags
            tags = generate_hashtags(clip_data_for_tags)
            logger.info("Hashtags (TikTok): %s", tags["tiktok"][:80])
        except Exception as exc:
            logger.warning("Hashtag generation failed: %s", exc)
            tags = {"tiktok": "", "youtube": "", "instagram": ""}

        # Persist thumbnail + hashtags on the clip's DB record
        try:
            from database import get_connection
            clip_id = clip_data_for_tags.get("id") or clip_data_for_tags.get("shared_clip_id")
            if clip_id:
                with get_connection() as _conn:
                    _conn.execute(
                        """UPDATE shared_clips
                           SET thumbnail_path = ?, hashtags_tiktok = ?,
                               hashtags_youtube = ?, hashtags_instagram = ?
                           WHERE shared_clip_id = ?""",
                        (thumbnail_path, tags["tiktok"], tags["youtube"], tags["instagram"], clip_id),
                    )
        except Exception:
            pass  # non-fatal — data saved to disk / memory regardless

        logger.info("Package editing complete → %s", output_path)
        return output_path

    except Exception as e:
        logger.exception("Unexpected error during package processing: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Step 1: Download / locate raw clips
# ══════════════════════════════════════════════════════════════════════════════

def _acquire_raw_clips(clips: List[Dict]) -> List[Optional[Path]]:
    """
    Download or locate each clip. Returns a list of local Path objects
    (or None for any clip that could not be acquired).
    """
    results: List[Optional[Path]] = []

    for clip in clips:
        local_path = clip.get("local_path")
        url = clip.get("url")

        if local_path and Path(local_path).exists():
            logger.debug("Using existing local file: %s", local_path)
            results.append(Path(local_path))
            continue

        if url:
            downloaded = _download_clip(url, clip.get("title", "clip"))
            results.append(downloaded)
            continue

        logger.warning(
            "Clip '%s' has neither a local_path nor a URL. Skipping.",
            clip.get("title", "unknown"),
        )
        results.append(None)

    return results


def _twitch_direct_download(url: str, out_path: Path) -> Optional[Path]:
    """
    Fallback Twitch clip downloader using VideoAccessToken_Clip GraphQL.
    Used when yt-dlp's Twitch extractor is broken (e.g. PersistedQueryNotFound).

    Returns Path to downloaded .mp4, or None on failure.
    """
    import re as _re
    import json as _json
    import urllib.request as _urlreq

    m = _re.search(r'/clip/([^/?#]+)', url)
    if not m:
        return None
    slug = m.group(1)

    gql_url  = "https://gql.twitch.tv/gql"
    headers  = {
        "Client-ID":   "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Content-Type": "application/json",
    }
    payload = _json.dumps([{
        "operationName": "VideoAccessToken_Clip",
        "variables": {"slug": slug},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11",
            }
        },
    }]).encode()

    try:
        req  = _urlreq.Request(gql_url, data=payload, headers=headers, method="POST")
        resp = _urlreq.urlopen(req, timeout=15)
        body = _json.loads(resp.read())
        clip_data = body[0]["data"]["clip"]
        if not clip_data:
            logger.warning("Twitch direct: clip not found for slug '%s'", slug)
            return None

        token_obj = clip_data["playbackAccessToken"]
        sig   = token_obj["signature"]
        token = token_obj["value"]

        # Pick best quality (highest resolution)
        qualities = clip_data.get("videoQualities", [])
        if not qualities:
            logger.warning("Twitch direct: no videoQualities for slug '%s'", slug)
            return None
        best = max(qualities, key=lambda q: int(q.get("quality", "0")))
        cdn_url = best["sourceURL"] + f"?sig={sig}&token={_urlreq.quote(token)}"

        logger.info("Twitch direct fallback: downloading %s @ %sp", slug, best.get("quality"))
        req2 = _urlreq.Request(cdn_url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlreq.urlopen(req2, timeout=60) as resp2:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as fout:
                fout.write(resp2.read())

        if out_path.exists() and out_path.stat().st_size > 10_000:
            logger.info("Twitch direct fallback: saved → %s", out_path.name)
            return out_path

        logger.warning("Twitch direct fallback: output file too small, likely failed")
        return None

    except Exception as exc:
        logger.warning("Twitch direct fallback failed for '%s': %s", url, exc)
        return None


def _download_clip(url: str, title: str) -> Optional[Path]:
    """
    Download a clip from a URL using yt-dlp, with a direct Twitch CDN fallback.

    Args:
        url: Twitch clip URL or YouTube video URL.
        title: Used to generate the output filename.

    Returns:
        Path to the downloaded file, or None on failure.
    """
    # Sanitize title for use as a filename
    safe_title = re.sub(r'[^\w\-_\. ]', '', title)[:50].strip()
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_stem   = f"{timestamp}_{safe_title}"
    out_path   = RAW_DIR / f"{out_stem}.%(ext)s"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", str(out_path),
        "--no-warnings",
        # Bot-detection bypass: present as Android app client, which skips
        # YouTube's sign-in challenge that affects server/datacenter IPs
        "--extractor-args", "youtube:player_client=android,web",
        "--add-header", "User-Agent:Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36",
        url,
    ]

    logger.info("Downloading: %s", url[:80])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("yt-dlp failed for '%s' — trying direct fallback", url[:80])
            # Twitch-only fallback: VideoAccessToken_Clip GraphQL
            if "twitch.tv" in url and "/clip/" in url:
                fallback_path = RAW_DIR / f"{out_stem}.mp4"
                result2 = _twitch_direct_download(url, fallback_path)
                if result2:
                    return result2
            logger.error("yt-dlp failed for '%s':\n%s", url, result.stderr[:500])
            return None

        # Find the actual downloaded file (yt-dlp fills in %(ext)s)
        for f in RAW_DIR.glob(f"{out_stem}.*"):
            if f.suffix.lower() in (".mp4", ".mkv", ".webm"):
                logger.info("Downloaded → %s", f.name)
                return f

        logger.error("yt-dlp succeeded but no output file found for '%s'.", url)
        return None

    except FileNotFoundError:
        logger.error(
            "yt-dlp is not installed. Install with:\n"
            "  pip install yt-dlp\nor\n"
            "  brew install yt-dlp"
        )
        return None
    except subprocess.TimeoutExpired:
        logger.error("Download timed out for '%s'.", url)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Step 2: Probe duration
# ══════════════════════════════════════════════════════════════════════════════

def _probe_duration(video_path: Path) -> Optional[float]:
    """Use FFprobe to get the exact duration of a video file in seconds."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Step 3: Process segments (crop + overlays)
# ══════════════════════════════════════════════════════════════════════════════

def _process_segments(
    clips: List[Dict],
    local_paths: List[Optional[Path]],
    template_spec: Dict,
) -> List[Path]:
    """
    Crop each clip to 9:16 and apply template overlays (credit card, etc.).

    Uses moviepy for compositing. Skips clips whose local_path is None.

    Returns:
        List of paths to processed segment files (in clips/raw/).
    """
    try:
        from moviepy.editor import (
            VideoFileClip, CompositeVideoClip, TextClip, ColorClip,
            concatenate_videoclips, ImageClip,
        )
    except ImportError:
        logger.error(
            "moviepy is not installed. Install with:\n"
            "  pip install moviepy"
        )
        return []

    processed: List[Path] = []
    total = len([p for p in local_paths if p is not None])

    for i, (clip_meta, lpath) in enumerate(zip(clips, local_paths)):
        if lpath is None:
            logger.warning("Skipping clip '%s' — no local file.", clip_meta.get("title"))
            continue

        out_path = RAW_DIR / f"segment_{i:02d}_{lpath.stem}.mp4"

        if out_path.exists():
            # Validate cached segment — if it's corrupted (e.g. killed mid-write),
            # delete it and reprocess rather than propagating a broken file.
            if _probe_duration(out_path) is not None:
                logger.debug("Segment already processed: %s", out_path.name)
                processed.append(out_path)
                continue
            else:
                logger.warning(
                    "Cached segment '%s' is invalid — deleting and reprocessing.",
                    out_path.name,
                )
                out_path.unlink(missing_ok=True)

        logger.info("Processing segment %d/%d: %s", i + 1, total, lpath.name)

        _PROCESS_TIMEOUT = 600  # seconds — multi-pass layout (facecam + blurred bg + overlays)
        result_holder: List[Optional[Path]] = [None]
        exc_holder:    List[Optional[Exception]] = [None]

        def _run_processing(
            _lpath=lpath, _clip_meta=clip_meta, _out=out_path,
        ):
            try:
                result_holder[0] = _apply_layout(_lpath, _clip_meta, _out)
            except Exception as e:
                exc_holder[0] = e

        t = threading.Thread(target=_run_processing, daemon=True)
        t.start()
        t.join(timeout=_PROCESS_TIMEOUT)

        if t.is_alive():
            logger.error(
                "Processing timeout (%ds) for segment '%s' — skipping.",
                _PROCESS_TIMEOUT, lpath.name,
            )
            continue

        if exc_holder[0]:
            logger.error("Failed to process segment '%s': %s", lpath.name, exc_holder[0])
            continue

        if result_holder[0]:
            processed.append(result_holder[0])

    return processed


def _escape_ffmpeg_text(text: str) -> str:
    """Escape a string for use as a value in an FFmpeg drawtext filter."""
    return (
        text
        .replace("\\", "\\\\")
        .replace("'",  "\\'")
        .replace(":",  "\\:")
        .replace("[",  "\\[")
        .replace("]",  "\\]")
    )


# ── Non-gaming categories (for detect_clip_type) ──────────────────────────────
_NON_GAMING_CATEGORIES = {
    'just chatting', 'just_chatting', 'irl', 'react',
    'food & drink', 'travel', 'fitness', 'art', 'music',
    'talk shows', 'pools, hot tubs, and beaches',
    'pools hot tubs and beaches', 'pools_hot_tubs_and_beaches',
    'beauty & body art', 'beauty and body art',
    'talk shows & podcasts', 'talk shows and podcasts',
    'software and game development',
    'crypto', 'gambling', 'sports', 'news & politics', 'news and politics',
    'science & technology', 'science and technology',
    'howto & style', 'howto and style',
    'people & blogs', 'people and blogs',
    'comedy', 'entertainment', 'education',
    # YouTube categoryIds for non-gaming content
    '22', '23', '24', '25', '26', '27', '28', '29',
}


def detect_clip_type(clip_meta: Dict) -> str:
    """
    Determine if a clip is gaming or talking-head content.
    Returns 'gaming' or 'talking'.
    """
    game = (clip_meta.get('game') or clip_meta.get('category') or '').lower()
    if game in _NON_GAMING_CATEGORIES:
        logger.info("detect_clip_type: '%s' → talking", game)
        return 'talking'
    logger.debug("detect_clip_type: '%s' → gaming", game)
    return 'gaming'




# ══════════════════════════════════════════════════════════════════════════════
# Layout constants
# ══════════════════════════════════════════════════════════════════════════════

OUTPUT_W   = 1080
OUTPUT_H   = 1920
BRANDING_H = 80

_FONT_BOLD = find_font()


# ══════════════════════════════════════════════════════════════════════════════
# Layout helpers — PIL-rendered assets + PNG overlay
# ══════════════════════════════════════════════════════════════════════════════


def make_branding_bar(platform: str, creator_name: str) -> str:
    """
    Render a solid-black branding bar: platform name (colored) on the left,
    creator URL on the right.  Returns png_path.
    """
    from PIL import Image, ImageDraw, ImageFont

    img  = Image.new("RGBA", (OUTPUT_W, BRANDING_H), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    font_lg = ImageFont.truetype(_FONT_BOLD, 40)
    font_sm = ImageFont.truetype(_FONT_BOLD, 28)
    color_map = {"twitch": (145, 70, 255), "youtube": (255, 0, 0), "kick": (83, 252, 24)}
    color = color_map.get(platform.lower(), (255, 255, 255))

    draw.text((30, BRANDING_H // 2 - 20), platform.upper(), fill=color, font=font_lg)
    if creator_name:
        url_text = f"{platform.lower()}.com/{creator_name}"
        bbox = draw.textbbox((0, 0), url_text, font=font_sm)
        tw   = bbox[2] - bbox[0]
        draw.text((OUTPUT_W - tw - 30, BRANDING_H // 2 - 14), url_text, fill=(255, 255, 255, 255), font=font_sm)

    out_path = f"/tmp/branding_{platform}_{abs(hash(creator_name))}.png"
    img.save(out_path)
    return out_path


def _overlay_png_on_video(video_in: str, png: str, x: int, y: int, video_out: str) -> bool:
    """Composite a PNG image onto a video at pixel position (x, y)."""
    cmd = [
        "ffmpeg", "-y", "-threads", "2",
        "-i", video_in, "-i", png,
        "-filter_complex", f"[0:v][1:v]overlay={x}:{y}",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
        video_out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.returncode == 0


# ══════════════════════════════════════════════════════════════════════════════
# Hook text overlay — white rounded-rectangle card at top of video
# ══════════════════════════════════════════════════════════════════════════════

def is_weak_title(title: str) -> bool:
    """
    Return True if the title is a generic label rather than a compelling hook.
    Triggers fallback to raw source title or skips the hook card entirely.
    """
    import re as _re
    if not title:
        return True
    t = title.lower().strip()
    # Strip trailing emoji for length check
    t_no_emoji = _re.sub(r'[\U00010000-\U0010ffff]', '', t).strip()
    if len(t_no_emoji) < 15:
        return True
    weak_patterns = [
        r'^\w+\s+\w+$',        # exactly two words: 'pikachu cake', 'gaming moment'
        r'^the \w+$',           # 'the moment', 'the clip'
        r'^\w+ clip$',          # 'funny clip'
        r'^\w+ moment$',        # 'gaming moment'
        r'^clip$',
        r'^moment$',
    ]
    for pattern in weak_patterns:
        if _re.match(pattern, t_no_emoji):
            return True
    return False

def make_hook_text_overlay(
    text: str,
    video_width: int = 1080,
    max_height: int = 400,
) -> tuple:
    """
    Render a clean TikTok-style white rounded-rectangle hook card.
    Arial Bold first (cleanest readability), no stroke needed on white bg.
    Card is centered horizontally on a full-width transparent canvas.
    Returns (hook_png_path, card_w, card_h, total_h).
    """
    import textwrap
    from PIL import Image, ImageDraw, ImageFont
    from captions import censor_caption_text

    text = censor_caption_text(text)

    # Arial Bold gives the cleanest readable TikTok style
    font_path = find_font()
    if not font_path:
        raise RuntimeError("No suitable font found for hook overlay")

    # Start at 62, shrink to fit the available blur-zone height
    font_size = 62
    font = ImageFont.truetype(font_path, font_size)
    lines = []
    card_w = card_h = total_h = padding_x = padding_y = line_height = 0

    while font_size >= 32:
        font = ImageFont.truetype(font_path, font_size)
        wrapped = textwrap.fill(text, width=22)
        lines = wrapped.split("\n")[:3]

        padding_x = 40
        padding_y = 30
        line_height = int(font_size * 1.25)

        tmp_img = Image.new("RGBA", (1, 1))
        tmp_draw = ImageDraw.Draw(tmp_img)
        max_line_w = 0
        for line in lines:
            bbox = tmp_draw.textbbox((0, 0), line, font=font)
            max_line_w = max(max_line_w, bbox[2] - bbox[0])

        card_w = min(max_line_w + padding_x * 2, 920)
        card_h = len(lines) * line_height + padding_y * 2
        total_h = card_h + 60

        if total_h <= max_height:
            break
        font_size -= 4

    img = Image.new("RGBA", (video_width, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Center card horizontally
    card_x = (video_width - card_w) // 2
    card_y = 20

    # Clean white rounded rectangle — no shadow, no border
    draw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=24,
        fill=(255, 255, 255, 255),
    )

    # Clean black text — no stroke needed, white bg provides full contrast
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        text_x = card_x + (card_w - line_w) // 2
        text_y = card_y + padding_y + i * line_height
        draw.text((text_x, text_y), line, fill=(0, 0, 0, 255), font=font)

    hook_path = f"/tmp/hook_{abs(hash(text))}.png"
    img.save(hook_path)
    return hook_path, card_w, card_h, total_h


def add_hook_text_to_video(
    video_path: str,
    hook_text: str,
    output_path: str,
    fg_y: int = 500,
) -> bool:
    """
    Overlay a white hook-text card centered in the blur zone ABOVE the video frame.
    fg_y = y-pixel where the actual video frame starts on the 1920-tall canvas.
    Returns True on success, False on failure.
    """
    try:
        hook_png, card_w, card_h, total_h = make_hook_text_overlay(
            hook_text,
            max_height=max(fg_y - 40, 200),
        )
    except Exception as exc:
        logger.warning("Hook text PNG render failed: %s", exc)
        return False

    # Center the hook card in the space above the video frame
    hook_y = max((fg_y - total_h) // 2, 10)

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", hook_png,
        "-filter_complex", f"[0:v][1:v]overlay=0:{hook_y}",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-threads", "2",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        os.remove(hook_png)
    except Exception:
        pass
    return result.returncode == 0


def generate_thumbnail(video_path: str, output_path: str, hook_text: str = "", creator_name: str = "") -> bool:
    """
    Extract a clean raw frame at 30% into the video and save as JPEG.
    No text, no gradient, no overlays — just the frame resized to 1080×1920.
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=10,
        )
        duration = float(probe.stdout.strip())
        timestamp = duration * 0.30
    except Exception:
        timestamp = 3.0

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0 and Path(output_path).exists()


# ══════════════════════════════════════════════════════════════════════════════
# Single layout — full frame + blurred background (all content types)
# ══════════════════════════════════════════════════════════════════════════════

def build_layout_fullframe(
    source: Path,
    output: Path,
    platform: str,
    creator: str,
) -> Optional[Path]:
    """
    Universal layout for every clip — gaming, IRL, or anything else.

    Step 1 — BG: source scaled+cropped to 1080×1920, heavy Gaussian blur (sigma=25).
    Step 2 — FG: source scaled to 1080px wide, full original frame preserved (no crop).
    Step 3 — FG centered on BG; audio pulled from source.
    Step 4 — Branding bar at very bottom.
    """
    stem     = source.stem
    tmp_bg   = Path(f"/tmp/bff_bg_{stem}.mp4")
    tmp_fg   = Path(f"/tmp/bff_fg_{stem}.mp4")
    tmp_comb = Path(f"/tmp/bff_comb_{stem}.mp4")
    temps    = [tmp_bg, tmp_fg, tmp_comb]

    try:
        # BG pass — blurred fill
        r1 = subprocess.run([
            "ffmpeg", "-y", "-threads", "2", "-i", str(source),
            "-vf", (
                f"fps=30,"
                f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUTPUT_W}:{OUTPUT_H},gblur=sigma=25"
            ),
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
            str(tmp_bg),
        ], capture_output=True, text=True, timeout=180)

        # FG pass — complete original frame, nothing cropped
        r2 = subprocess.run([
            "ffmpeg", "-y", "-threads", "2", "-i", str(source),
            "-vf", f"fps=30,scale={OUTPUT_W}:-2",
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
            str(tmp_fg),
        ], capture_output=True, text=True, timeout=180)

        if r1.returncode != 0 or r2.returncode != 0:
            logger.error("build_layout_fullframe: bg/fg pass failed — bg=%d fg=%d",
                         r1.returncode, r2.returncode)
            if r1.returncode != 0:
                logger.error("  bg stderr: %s", r1.stderr[-300:])
            if r2.returncode != 0:
                logger.error("  fg stderr: %s", r2.stderr[-300:])
            return None

        # Probe actual FG height so we can pin the exact vertical center
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=height", "-of", "csv=p=0", str(tmp_fg),
        ], capture_output=True, text=True, timeout=15)
        fg_h_str = probe.stdout.strip()
        fg_h = int(fg_h_str) if fg_h_str.isdigit() else OUTPUT_H // 2
        fg_y = (OUTPUT_H - fg_h) // 2

        # Overlay pass — FG centered on BG, audio from source
        r3 = subprocess.run([
            "ffmpeg", "-y", "-threads", "2",
            "-i", str(tmp_bg), "-i", str(tmp_fg), "-i", str(source),
            "-filter_complex", f"[0:v][1:v]overlay=(W-w)/2:{fg_y}[v]",
            "-map", "[v]", "-map", "2:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
            "-c:a", "aac", "-b:a", "128k", "-shortest",
            str(tmp_comb),
        ], capture_output=True, text=True, timeout=180)

        if r3.returncode != 0:
            logger.warning(
                "build_layout_fullframe: two-pass overlay failed (rc=%d) — trying single-pass fallback",
                r3.returncode,
            )
            logger.debug("  overlay stderr: %s", r3.stderr[-300:])
            # Single-pass fallback: scale+blur+overlay in one filter_complex, no tmp files needed
            r3 = subprocess.run([
                "ffmpeg", "-y", "-threads", "2", "-i", str(source),
                "-filter_complex", (
                    f"[0:v]fps=30,scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,"
                    f"crop={OUTPUT_W}:{OUTPUT_H},gblur=sigma=25[bg];"
                    f"[0:v]fps=30,scale={OUTPUT_W}:-2[fg];"
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
                ),
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
                "-c:a", "aac", "-b:a", "128k",
                str(tmp_comb),
            ], capture_output=True, text=True, timeout=300)
            if r3.returncode != 0:
                logger.error(
                    "build_layout_fullframe: single-pass fallback also failed:\n%s",
                    r3.stderr[-400:],
                )
                return None
            logger.info("build_layout_fullframe: single-pass fallback succeeded")

        # Branding bar at bottom
        brand_path = make_branding_bar(platform, creator)
        _overlay_png_on_video(str(tmp_comb), brand_path, 0, OUTPUT_H - BRANDING_H, str(output))

        return output if output.exists() and output.stat().st_size > 100_000 else None

    except subprocess.TimeoutExpired:
        logger.error("build_layout_fullframe: FFmpeg timeout")
        return None
    finally:
        for t in temps:
            t.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# Layout router — replaces _apply_template_to_clip
# ══════════════════════════════════════════════════════════════════════════════

def _apply_layout(
    video_path: Path,
    clip_meta: Dict,
    output_path: Path,
) -> Optional[Path]:
    """
    Apply the single full-frame blurred-background layout to every clip.
    One layout handles all content — gaming, IRL, or anything else.
    """
    src_raw  = (clip_meta.get("source") or "twitch").lower()
    platform = src_raw if src_raw in ("twitch", "youtube", "kick") else "twitch"
    creator  = clip_meta.get("creator_name") or ""

    logger.info("_apply_layout: full-frame blur → %s", video_path.name)
    result = build_layout_fullframe(video_path, output_path, platform, creator)

    if result is None:
        # Single-pass fallback if the three-step pipeline fails
        logger.warning("_apply_layout: layout failed — using single-pass fallback")
        enc = _get_encoder_flags()
        fc  = (
            "[0:v]fps=30,split[s1][s2];"
            "[s1]scale=270:480:force_original_aspect_ratio=increase,"
            "crop=270:480,gblur=sigma=5,scale=1080:1920[bg];"
            "[s2]scale=1080:-2:flags=lanczos[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[out]"
        )
        cmd = [
            "ffmpeg", "-y", "-threads", "2", "-hwaccel", "videotoolbox",
            "-i", str(video_path), "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a?", *enc,
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(output_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return output_path if r.returncode == 0 and output_path.exists() else None

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Step 4: Concatenate segments
# ══════════════════════════════════════════════════════════════════════════════

def _concatenate_segments(
    segments: List[Path],
    template_spec: Dict,
) -> Optional[Path]:
    """
    Concatenate processed segments using FFmpeg concat demuxer.
    Applies transition type from template spec.
    """
    if not segments:
        return None
    if len(segments) == 1:
        return segments[0]

    transition_type = template_spec.get("transition_type", "cut")
    transition_dur  = template_spec.get("transition_duration", 0.0)

    if transition_type in ("fade", "crossfade") and transition_dur > 0:
        return _concat_with_crossfade(segments, fade_duration=transition_dur)
    else:
        return _concat_hard_cut(segments)


def _concat_hard_cut(segments: List[Path]) -> Optional[Path]:
    """Concatenate video files with hard cuts using FFmpeg concat demuxer."""
    out_path = COMPILED_DIR / f"concat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

    # Write a concat list file
    list_file = COMPILED_DIR / "concat_list.txt"
    with open(list_file, "w") as f:
        for seg in segments:
            f.write(f"file '{seg.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-threads", "2",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    list_file.unlink(missing_ok=True)

    if result.returncode != 0:
        logger.error("FFmpeg concat failed:\n%s", result.stderr[-500:])
        return None

    return out_path


def _concat_with_crossfade(segments: List[Path], fade_duration: float) -> Optional[Path]:
    """
    Concatenate with crossfade transitions using FFmpeg xfade/acrossfade.
    Fast FFmpeg-only implementation — no MoviePy frame-by-frame processing.
    """
    out_path = COMPILED_DIR / f"concat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    enc = _get_encoder_flags()

    if len(segments) == 1:
        return segments[0]

    # Need durations for xfade offset calculation
    durations: List[float] = []
    for seg in segments:
        dur = _probe_duration(seg)
        if dur is None:
            logger.warning("Could not probe duration for '%s' — falling back to hard cut", seg.name)
            return _concat_hard_cut(segments)
        durations.append(dur)

    # Build FFmpeg filter_complex for xfade chain
    # xfade offset = sum(d[0..i-1]) - fade_duration * i  (account for overlap)
    inputs = []
    for seg in segments:
        inputs += ["-i", str(seg)]

    if len(segments) == 2:
        offset = max(0.0, durations[0] - fade_duration)
        filter_v = (
            f"[0:v][1:v]xfade=transition=fade:duration={fade_duration}:offset={offset:.3f}[v]"
        )
        filter_a = (
            f"[0:a][1:a]acrossfade=d={fade_duration}[a]"
        )
        filter_complex = f"{filter_v};{filter_a}"
        map_args = ["-map", "[v]", "-map", "[a]"]
    else:
        # General N-clip chain
        v_parts = []
        a_parts = []
        cumulative = 0.0
        prev_v = "[0:v]"
        prev_a = "[0:a]"
        for i in range(1, len(segments)):
            cumulative += durations[i - 1] - fade_duration
            offset = max(0.0, cumulative)
            out_v = f"[v{i}]" if i < len(segments) - 1 else "[v]"
            out_a = f"[a{i}]" if i < len(segments) - 1 else "[a]"
            v_parts.append(
                f"{prev_v}[{i}:v]xfade=transition=fade:duration={fade_duration}:offset={offset:.3f}{out_v}"
            )
            a_parts.append(
                f"{prev_a}[{i}:a]acrossfade=d={fade_duration}{out_a}"
            )
            prev_v = out_v
            prev_a = out_a
        filter_complex = ";".join(v_parts + a_parts)
        map_args = ["-map", "[v]", "-map", "[a]"]

    cmd = [
        "ffmpeg", "-y",
        "-threads", "2",
        *inputs,
        "-filter_complex", filter_complex,
        *map_args,
        *enc,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        logger.warning("FFmpeg xfade failed — falling back to hard cut:\n%s", result.stderr[-300:])
        return _concat_hard_cut(segments)

    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# Step 5: Audio normalization
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_audio(video_path: Path) -> Optional[Path]:
    """
    Normalize video audio to TikTok-recommended levels using FFmpeg's
    loudnorm filter (EBU R128 two-pass normalization).

    Returns:
        Path to the normalized video file, or None on failure.
    """
    out_path = video_path.parent / f"{video_path.stem}_normalized.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-threads", "2",
        "-i", str(video_path),
        "-af",
        (
            f"loudnorm=I={AUDIO_TARGET_LUFS}"
            f":LRA={AUDIO_TARGET_LRA}"
            f":TP={AUDIO_TARGET_TRUE_PEAK}"
            ":print_format=summary"
        ),
        "-c:v", "copy",   # Copy video stream — no re-encode needed
        "-c:a", "aac",
        "-b:a", "192k",
        str(out_path),
    ]

    logger.info("Normalizing audio for '%s'...", video_path.name)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        logger.error("Audio normalization failed:\n%s", result.stderr[-300:])
        return None

    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# Step 6: Burn subtitles
# ══════════════════════════════════════════════════════════════════════════════

# ── drawtext availability cache (checked once per process) ────────────────────
_DRAWTEXT_AVAILABLE: Optional[bool] = None
_DRAWTEXT_FONT = find_font()


def _check_drawtext() -> bool:
    """Return True if FFmpeg was compiled with drawtext (libfreetype) support."""
    global _DRAWTEXT_AVAILABLE
    if _DRAWTEXT_AVAILABLE is None:
        try:
            r = subprocess.run(
                ["ffmpeg", "-filters"], capture_output=True, text=True, timeout=10,
            )
            _DRAWTEXT_AVAILABLE = "drawtext" in r.stdout
            if _DRAWTEXT_AVAILABLE:
                logger.debug("FFmpeg drawtext: available")
            else:
                logger.info(
                    "FFmpeg drawtext not compiled in — will use PIL+overlay fallback for captions."
                )
        except Exception:
            _DRAWTEXT_AVAILABLE = False
    return _DRAWTEXT_AVAILABLE


def _escape_drawtext(text: str) -> str:
    """Escape a word for use inside an FFmpeg drawtext text= option."""
    # Replace characters that would break the filter string
    text = text.replace("\\", "\\\\")
    text = text.replace("'",  "\u2019")   # curly apostrophe avoids single-quote parse error
    text = text.replace(":",  "\\:")
    text = text.replace("%",  "\\%")
    return text


def _burn_captions_ffmpeg(
    video_path: Path,
    word_segments: List[Dict],
    out_path: Path,
    layout_type: str = "gaming",
    fg_y: int = 0,
    fg_h: int = 0,
) -> Optional[Path]:
    """
    Burn word-by-word captions using a single FFmpeg drawtext pass.

    Builds one chained drawtext filter per word, each gated by
    enable='between(t,START,END)'. Single subprocess call — completes in
    under 30 seconds even for long clips. Audio is stream-copied (no re-encode).

    Returns out_path on success, None if drawtext unavailable or command fails.
    """
    if not _check_drawtext():
        return None

    font_path = _DRAWTEXT_FONT or find_font()
    if not font_path:
        logger.warning("No TTF font found for drawtext — falling back to MoviePy.")
        return None

    # Place captions in the blur zone just below the bottom edge of the video frame.
    if fg_h > 0:
        caption_y = fg_y + fg_h + 20
        caption_y = min(caption_y, OUTPUT_H - BRANDING_H - 80)
        y_expr = str(caption_y)
    else:
        y_expr = "h-240" if layout_type == "talking" else "h-200"

    dt_filters = []
    for seg in word_segments:
        word = censor_word(seg.get("word", "").strip()).upper()
        if not word:
            continue
        t_start = float(seg["start"])
        t_end   = float(seg["end"])
        if t_end <= t_start:
            continue
        escaped = _escape_drawtext(word)
        dt_filters.append(
            f"drawtext=fontfile={font_path}"
            f":text='{escaped}'"
            f":fontsize=78"
            f":fontcolor=white"
            f":borderw=4"
            f":bordercolor=black"
            f":x=(w-text_w)/2"
            f":y={y_expr}"
            f":enable='between(t,{t_start:.3f},{t_end:.3f})'"
        )

    if not dt_filters:
        return None

    enc = _get_encoder_flags()
    cmd = [
        "ffmpeg", "-y",
        "-threads", "2",
        "-i", str(video_path),
        "-vf", ",".join(dt_filters),
        *enc,
        "-c:a", "copy",          # copy audio stream — no re-encode for speed
        "-movflags", "+faststart",
        str(out_path),
    ]

    logger.info("FFmpeg drawtext: burning %d word captions", len(dt_filters))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("FFmpeg drawtext failed:\n%s", result.stderr[-800:])
            return None
        return out_path if out_path.exists() else None
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg drawtext timed out (300s).")
        return None
    except FileNotFoundError:
        return None


def _render_word_png(
    word: str,
    out_path: Path,
    font_path: str,
    font_size: int = 80,
    stroke_w: int = 4,
) -> Tuple[int, int]:
    """
    Render a single word as a transparent-background PNG.
    Returns (image_width, image_height).
    """
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(font_path, font_size)

    # Measure text bounding box
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), word, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad = stroke_w + 6
    img_w = text_w + pad * 2
    img_h = text_h + pad * 2

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    tx = pad - bbox[0]
    ty = pad - bbox[1]

    # Draw black stroke by offsetting
    for ox in range(-stroke_w, stroke_w + 1):
        for oy in range(-stroke_w, stroke_w + 1):
            if ox == 0 and oy == 0:
                continue
            draw.text((tx + ox, ty + oy), word, font=font, fill=(0, 0, 0, 255))

    # Draw white text on top
    draw.text((tx, ty), word, font=font, fill=(255, 255, 255, 255))
    img.save(str(out_path), "PNG")
    return img_w, img_h


def _burn_captions_png_overlay(
    video_path: Path,
    word_segments: List[Dict],
    out_path: Path,
    layout_type: str = "gaming",
    fg_y: int = 0,
    fg_h: int = 0,
) -> Optional[Path]:
    """
    Burn word-by-word captions by piping a transparent caption track to FFmpeg.

    Instead of chaining N overlay filters (which exhausts FFmpeg's frame buffers
    for large N), this generates a transparent caption-track video via rawvideo
    pipe (QuickTime RLE / argb), then overlays it once in a second FFmpeg pass.

    Does NOT require libfreetype. All text rendering is in PIL.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = _DRAWTEXT_FONT or find_font()
    if not font_path:
        return None

    # ── Probe video dimensions and frame rate ─────────────────────────────────
    try:
        probe = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "csv=p=0", str(video_path)],
            text=True, timeout=15,
        ).strip().split(",")
        vid_w, vid_h = int(probe[0]), int(probe[1])
        fps_num, fps_den = map(int, probe[2].split("/"))
        fps = fps_num / fps_den
    except Exception as exc:
        logger.warning("Caption track: could not probe video: %s", exc)
        return None

    duration = _probe_duration(video_path)
    if not duration:
        return None
    total_frames = int(duration * fps) + 1

    # ── Pre-render unique word images (RGBA, full video width, short height) ──
    cap_h = 200   # caption strip height in pixels (placed at video bottom)
    cap_w = vid_w

    try:
        font_size = 78
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        return None

    stroke_w = 4

    def _render_word_rgba(word: str) -> bytes:
        """Return raw RGBA bytes for a transparent caption frame."""
        img = Image.new("RGBA", (cap_w, cap_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Measure text width for centering
        bbox = draw.textbbox((0, 0), word, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (cap_w - tw) // 2
        y = (cap_h - th) // 2
        # Black stroke
        for dx in range(-stroke_w, stroke_w + 1):
            for dy in range(-stroke_w, stroke_w + 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), word, font=font, fill=(0, 0, 0, 255))
        # White text
        draw.text((x, y), word, font=font, fill=(255, 255, 255, 255))
        return img.tobytes()

    blank_frame = Image.new("RGBA", (cap_w, cap_h), (0, 0, 0, 0)).tobytes()

    # Build segment list sorted by time, pre-render unique words
    valid_segs = []
    word_cache: dict = {}
    for seg in word_segments:
        word = censor_word(seg.get("word", "").strip()).upper()
        t_start = float(seg.get("start", 0))
        t_end   = float(seg.get("end", 0))
        if not word or t_end <= t_start:
            continue
        if word not in word_cache:
            word_cache[word] = _render_word_rgba(word)
        valid_segs.append((t_start, t_end, word))

    if not valid_segs:
        return None

    valid_segs.sort(key=lambda s: s[0])
    logger.info("Caption track: rendering %d words via rawvideo pipe", len(valid_segs))

    # ── Pipe rawvideo to FFmpeg → encode transparent caption track (qtrle) ────
    cap_track = out_path.parent / f"cap_track_{out_path.stem}.mov"
    try:
        enc_proc = subprocess.Popen(
            ["ffmpeg", "-y",
             "-threads", "2",
             "-f", "rawvideo", "-pixel_format", "rgba",
             "-video_size", f"{cap_w}x{cap_h}",
             "-framerate", f"{fps_num}/{fps_den}",
             "-i", "pipe:0",
             "-c:v", "qtrle", "-pix_fmt", "argb",
             str(cap_track)],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        seg_idx = 0
        prev_word: Optional[str] = None
        prev_bytes: Optional[bytes] = blank_frame

        for frame_n in range(total_frames):
            t = frame_n / fps
            # Advance segment pointer
            while seg_idx < len(valid_segs) and valid_segs[seg_idx][1] < t:
                seg_idx += 1
            # Find active word
            if seg_idx < len(valid_segs):
                s_start, s_end, s_word = valid_segs[seg_idx]
                if s_start <= t <= s_end:
                    frame_bytes = word_cache[s_word]
                else:
                    frame_bytes = blank_frame
            else:
                frame_bytes = blank_frame

            enc_proc.stdin.write(frame_bytes)

        enc_proc.stdin.close()
        enc_proc.wait(timeout=60)

        if enc_proc.returncode != 0 or not cap_track.exists():
            logger.warning("Caption track encoding failed")
            return None

    except Exception as exc:
        logger.warning("Caption track pipe failed: %s", exc)
        return None

    # ── Single overlay pass: video + caption track ────────────────────────────
    # Place caption strip in the blur zone just below the video frame
    if fg_h > 0:
        caption_y = fg_y + fg_h + 20
        caption_y = min(caption_y, OUTPUT_H - BRANDING_H - 80)
    else:
        caption_y = vid_h - cap_h
    try:
        cmd = [
            "ffmpeg", "-y",
            "-threads", "2",
            "-i", str(video_path),
            "-i", str(cap_track),
            "-filter_complex",
            f"[0:v][1:v]overlay=x=(W-w)/2:y={caption_y}:format=auto[out]",
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("Caption overlay pass failed:\n%s", result.stderr[-500:])
            return None
        return out_path if out_path.exists() else None

    except Exception as exc:
        logger.error("Caption overlay error: %s", exc)
        return None
    finally:
        cap_track.unlink(missing_ok=True)


def _burn_captions_moviepy(
    video_path: Path,
    word_segments: List[Dict],
    out_path: Path,
    layout_type: str = "gaming",
    fg_y: int = 0,
    fg_h: int = 0,
) -> Optional[Path]:
    """
    Burn word-by-word captions using MoviePy TextClip (ImageMagick).
    Last-resort fallback when both drawtext and PIL are unavailable.
    """
    try:
        from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip
    except ImportError:
        logger.warning("moviepy not available — skipping captions.")
        return None

    font = _resolve_font("Arial Bold")
    video = VideoFileClip(str(video_path))
    vid_h = video.size[1]
    if fg_h > 0:
        caption_y = fg_y + fg_h + 20
        caption_y = min(caption_y, OUTPUT_H - BRANDING_H - 80)
    else:
        caption_y = int(vid_h * (0.78 if layout_type == "talking" else 0.82))

    txt_clips = []
    for seg in word_segments:
        start    = float(seg["start"])
        end      = float(seg["end"])
        duration = end - start
        if duration <= 0 or start >= video.duration:
            continue
        duration = min(duration, video.duration - start)
        word = censor_word(seg["word"].strip()).upper()
        if not word:
            continue
        try:
            txt = TextClip(
                word,
                fontsize=80,
                font=font,
                color="white",
                stroke_color="black",
                stroke_width=4,
                method="label",
            ).set_start(start).set_duration(duration)
            pos_y = caption_y - txt.h // 2
            txt_clips.append(txt.set_position(("center", pos_y)))
        except Exception as exc:
            logger.debug("TextClip failed for '%s': %s", word, exc)
            continue

    if not txt_clips:
        logger.info("No caption clips created — skipping captions.")
        video.close()
        return None

    final = CompositeVideoClip([video] + txt_clips)
    try:
        final.write_videofile(
            str(out_path), fps=TARGET_FPS, codec="libx264",
            audio_codec="aac", logger=None,
        )
    except Exception as exc:
        logger.error("MoviePy caption export failed: %s", exc)
        return None
    finally:
        final.close()
        video.close()

    return out_path if out_path.exists() else None


def _burn_subtitles(
    clips: List[Dict],
    local_paths: List[Optional[Path]],
    video_path: Path,
    template_spec: Dict,
    user_prefs: Optional[Dict] = None,
    layout_type: str = "gaming",
) -> Optional[Path]:
    """
    Burn word-by-word captions into the video.

    Fast path:  FFmpeg drawtext filter (single pass, ~10-30 s per clip).
    Fallback:   MoviePy TextClip via ImageMagick if drawtext not compiled in.

    Style: bold white text size 80, 4px black stroke, one word at a time,
    centered in the lower third. Words appear/disappear instantly, no fade.
    """
    caption_cfg = template_spec.get("caption", {})
    if not caption_cfg.get("enabled", True):
        return None

    source_path = next((p for p in local_paths if p and p.exists()), None)
    if not source_path:
        logger.warning("No local clip available for caption generation.")
        return None

    if user_prefs is None:
        user_prefs = {}

    out_path = video_path.parent / f"{video_path.stem}_captioned.mp4"

    from captions import generate_word_segments, censor_word
    word_segments = generate_word_segments(str(source_path))
    if not word_segments:
        logger.info("No word segments generated — skipping captions.")
        return None

    logger.info("Burning %d word captions onto video", len(word_segments))

    # Probe source clip dimensions to compute fg_y/fg_h so captions land
    # in the blur zone just below the actual video frame, not on stream UI.
    fg_y = 0
    fg_h = 0
    try:
        src_probe = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(source_path)],
            text=True, timeout=10,
        ).strip().split(",")
        src_w, src_h = int(src_probe[0]), int(src_probe[1])
        # Foreground was scaled to OUTPUT_W wide (scale=1080:-2), height keeps aspect ratio
        fg_h = min(int(OUTPUT_W * src_h / src_w), OUTPUT_H)
        fg_y = (OUTPUT_H - fg_h) // 2
    except Exception:
        pass  # fall back to bottom-of-frame positioning in each caption function

    # Fast path 1: FFmpeg drawtext (requires libfreetype in FFmpeg)
    result = _burn_captions_ffmpeg(video_path, word_segments, out_path, layout_type, fg_y=fg_y, fg_h=fg_h)
    if result:
        return result

    # Fast path 2: PIL-rendered PNGs composited via FFmpeg overlay (no libfreetype needed)
    result = _burn_captions_png_overlay(video_path, word_segments, out_path, layout_type, fg_y=fg_y, fg_h=fg_h)
    if result:
        return result

    # Last resort: MoviePy TextClip (slow, frame-by-frame Python compositing)
    logger.info("Falling back to MoviePy TextClip for captions.")
    return _burn_captions_moviepy(video_path, word_segments, out_path, layout_type, fg_y=fg_y, fg_h=fg_h)


# ══════════════════════════════════════════════════════════════════════════════
# Step 7: Export final
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Duration enforcement
# ══════════════════════════════════════════════════════════════════════════════

_MIN_CLIP_SEC = 15.0   # clips shorter than this are rejected entirely
_MAX_CLIP_SEC = 90.0   # clips longer than this are hard-trimmed to 90 s


def _enforce_duration_limits(
    clips: List[Dict],
    local_paths: List[Optional[Path]],
) -> List[Optional[Path]]:
    """
    Enforce clip duration rules:
      < 15 s  — reject (return None so downstream skips the clip)
      15–90 s — pass through unchanged
      > 90 s  — trim to the first 90 s via FFmpeg stream copy

    Does NOT trim to peak moments or alter content in any other way.
    """
    updated: List[Optional[Path]] = []
    for clip_meta, lpath in zip(clips, local_paths):
        if lpath is None:
            updated.append(None)
            continue

        duration = _probe_duration(lpath) or 0.0

        if duration < _MIN_CLIP_SEC:
            logger.warning(
                "Rejecting '%s' — too short (%.1f s < %.0f s minimum)",
                lpath.name, duration, _MIN_CLIP_SEC,
            )
            updated.append(None)
            continue

        if duration > _MAX_CLIP_SEC:
            trimmed = _trim_clip_to(lpath, _MAX_CLIP_SEC)
            if trimmed:
                logger.info(
                    "Trimmed '%s' from %.1f s to %.0f s cap",
                    lpath.name, duration, _MAX_CLIP_SEC,
                )
                updated.append(trimmed)
            else:
                updated.append(lpath)  # trim failed — keep original
            continue

        updated.append(lpath)
    return updated


def _trim_clip_to(source: Path, max_sec: float) -> Optional[Path]:
    """Trim a clip to the first max_sec seconds using FFmpeg stream copy."""
    out = source.parent / f"capped_{source.stem}.mp4"
    cmd = [
        "ffmpeg", "-y", "-threads", "2",
        "-i", str(source),
        "-t", f"{max_sec:.3f}",
        "-c", "copy",
        str(out),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and out.exists():
            return out
        logger.warning("_trim_clip_to failed for '%s': %s", source.name, result.stderr[-200:])
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("_trim_clip_to error: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Music detection
# ══════════════════════════════════════════════════════════════════════════════

def _run_music_detection(
    clips: List[Dict],
    local_paths: List[Optional[Path]],
) -> None:
    """Run music_detector on each clip and flag in the database if detected."""
    try:
        from music_detector import detect_music, flag_clip_in_database
    except ImportError:
        return

    for clip_meta, lpath in zip(clips, local_paths):
        if lpath is None:
            continue
        try:
            has_music = detect_music(lpath)
            clip_id = clip_meta.get("clip_id")
            if clip_id:
                flag_clip_in_database(clip_id, has_music)
        except Exception as e:
            logger.warning("Music detection error for '%s': %s", lpath, e)


# ══════════════════════════════════════════════════════════════════════════════
# Branding overlay
# ══════════════════════════════════════════════════════════════════════════════

def _apply_branding_step(
    video_path: Path,
    user_prefs: Dict,
) -> Optional[str]:
    """Apply branding overlay using branding.py. Returns new path or None."""
    try:
        from branding import apply_branding
        return apply_branding(video_path, user_prefs=user_prefs)
    except ImportError:
        return None
    except Exception as e:
        logger.warning("Branding step failed: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Intro / Outro
# ══════════════════════════════════════════════════════════════════════════════

def _add_intro_outro(video_path: Path, user_prefs: Dict) -> Optional[str]:
    """
    Prepend an intro clip and/or append an outro clip using FFmpeg concat.
    Both are cropped to 9:16 (1080×1920) if they aren't already.
    Returns the path to the new combined file, or None if not configured.
    """
    intro_src = user_prefs.get("intro_clip_path", "").strip()
    outro_src = user_prefs.get("outro_clip_path", "").strip()

    intro_path = Path(intro_src) if intro_src and Path(intro_src).exists() else None
    outro_path = Path(outro_src) if outro_src and Path(outro_src).exists() else None

    if not intro_path and not outro_path:
        return None

    segments: List[Path] = []

    if intro_path:
        scaled = _scale_to_vertical(intro_path)
        if scaled:
            segments.append(scaled)

    segments.append(video_path)

    if outro_path:
        scaled = _scale_to_vertical(outro_path)
        if scaled:
            segments.append(scaled)

    if len(segments) < 2:
        return None

    out_path = video_path.parent / f"bookend_{video_path.stem}.mp4"
    list_file = video_path.parent / "bookend_concat.txt"
    with open(list_file, "w") as f:
        for seg in segments:
            f.write(f"file '{seg.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-threads", "2",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        str(out_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        list_file.unlink(missing_ok=True)
        if result.returncode == 0:
            logger.info("Intro/outro added → %s", out_path.name)
            return str(out_path)
        logger.warning("Intro/outro concat failed:\n%s", result.stderr[-300:])
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        list_file.unlink(missing_ok=True)
        logger.warning("_add_intro_outro error: %s", e)
        return None


def _scale_to_vertical(source: Path) -> Optional[Path]:
    """Scale/crop a clip to 9:16 (1080×1920) using FFmpeg scale+crop."""
    out = source.parent / f"vert_{source.stem}.mp4"
    if out.exists():
        return out
    cmd = [
        "ffmpeg", "-y",
        "-threads", "2",
        "-i", str(source),
        "-vf",
        "scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih),"
        "crop=1080:1920",
        "-c:a", "copy",
        str(out),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            return out
        logger.warning("_scale_to_vertical failed for '%s'", source.name)
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _export_final(source_path: Path, mode: str) -> str:
    """Move the finished video to clips/processed/ with a timestamped filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_name = f"clipcast_{mode}_{timestamp}.mp4"
    final_path = PROCESSED_DIR / final_name
    shutil.move(str(source_path), str(final_path))
    logger.info("Final video exported → %s", final_path)
    return str(final_path)


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing editor.py — dependency check...")
    print()

    # Check all required tools are installed
    checks = [
        ("FFmpeg", ["ffmpeg", "-version"]),
        ("FFprobe", ["ffprobe", "-version"]),
        ("yt-dlp", ["yt-dlp", "--version"]),
    ]

    all_ok = True
    for name, cmd in checks:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            version_line = result.stdout.decode().split("\n")[0]
            print(f"  ✓ {name:10}  {version_line[:60]}")
        except FileNotFoundError:
            print(f"  ✗ {name:10}  NOT FOUND")
            all_ok = False

    # Check Python packages
    py_packages = [
        ("moviepy",        "moviepy"),
        ("PIL",            "Pillow"),
        ("faster_whisper", "faster-whisper"),
    ]
    for import_name, pip_name in py_packages:
        try:
            __import__(import_name)
            print(f"  ✓ {pip_name}")
        except ImportError:
            print(f"  ✗ {pip_name}  NOT INSTALLED")
            all_ok = False

    print()
    if all_ok:
        print("All dependencies found. Editor is ready to use.")
    else:
        print("Some dependencies are missing. See above for what needs to be installed.")
        print("\nFull install command:")
        print("  pip install -r requirements.txt")
        print("  brew install ffmpeg   (macOS)")
        print("  sudo apt install ffmpeg   (Linux)")
