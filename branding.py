"""
branding.py
===========
Applies per-user branding overlays to finished videos using FFmpeg.

Reads the 'branding' section from preferences.yaml and applies:
  - Watermark image (PNG) at the specified corner with configurable opacity
  - Channel name text overlay at the specified position

Both overlays are applied via FFmpeg filter_complex so there is no quality
loss from a re-encode of the video frames beyond what libx264 normally does.
If neither branding element is enabled, the source file is returned unchanged.

Called by editor.py as the last step before intro/outro concatenation.

SaaS Note:
    branding_cfg is passed explicitly — no global config reads. In multi-user
    mode, each user's branding settings come from their preferences record.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Position helpers
# ══════════════════════════════════════════════════════════════════════════════

_WATERMARK_POSITIONS = {
    "top_left":     "x=20:y=20",
    "top_right":    "x=main_w-overlay_w-20:y=20",
    "bottom_left":  "x=20:y=main_h-overlay_h-20",
    "bottom_right": "x=main_w-overlay_w-20:y=main_h-overlay_h-20",
}

_TEXT_POSITIONS = {
    "top_left":     ("20", "30"),
    "top_right":    ("main_w-tw-20", "30"),
    "bottom_left":  ("20", "main_h-th-20"),
    "bottom_right": ("main_w-tw-20", "main_h-th-20"),
}


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def apply_branding(
    video_path: str | Path,
    branding_cfg: Optional[Dict[str, Any]] = None,
    user_prefs: Optional[Dict] = None,
) -> Optional[str]:
    """
    Apply watermark and/or channel name text to a finished video file.

    Args:
        video_path:   Path to the processed video (input).
        branding_cfg: Branding configuration dict (from preferences['branding']).
                      If None, loads from user_prefs['branding'].
        user_prefs:   Full preferences dict. Used only when branding_cfg is None.

    Returns:
        Path to the branded output file (str), or None if no branding is
        configured or FFmpeg fails. Caller falls back to the original file.
    """
    video_path = Path(video_path)

    if branding_cfg is None:
        if user_prefs is None:
            try:
                from preferences import load_preferences
                user_prefs = load_preferences()
            except Exception:
                return None
        branding_cfg = user_prefs.get("branding", {})

    if not branding_cfg:
        return None

    watermark_path = branding_cfg.get("watermark_image", "").strip()
    watermark_pos  = branding_cfg.get("watermark_position", "bottom_right")
    watermark_alpha = float(branding_cfg.get("watermark_opacity", 0.7))
    show_name      = branding_cfg.get("show_channel_name", False)
    name_text      = branding_cfg.get("channel_name_text", "").strip()
    name_pos       = branding_cfg.get("channel_name_position", "bottom_left")

    has_watermark = bool(watermark_path) and Path(watermark_path).exists()
    has_text      = show_name and bool(name_text)

    if not has_watermark and not has_text:
        return None   # Nothing to apply

    out_path = video_path.parent / f"{video_path.stem}_branded.mp4"

    if has_watermark and has_text:
        success = _apply_both(
            video_path, Path(watermark_path), watermark_pos, watermark_alpha,
            name_text, name_pos, out_path,
        )
    elif has_watermark:
        success = _apply_watermark_only(
            video_path, Path(watermark_path), watermark_pos, watermark_alpha,
            out_path,
        )
    else:
        success = _apply_text_only(video_path, name_text, name_pos, out_path)

    if success:
        logger.info("Branding applied → %s", out_path.name)
        return str(out_path)

    logger.warning("Branding failed — returning original video unchanged.")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Internal FFmpeg helpers
# ══════════════════════════════════════════════════════════════════════════════

def _run_ffmpeg(cmd: list) -> bool:
    """Run an FFmpeg command and return True on success."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.error("branding FFmpeg error:\n%s", result.stderr[-400:])
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error("branding FFmpeg call failed: %s", e)
        return False


def _apply_watermark_only(
    video_path: Path,
    watermark: Path,
    position: str,
    alpha: float,
    out_path: Path,
) -> bool:
    pos_expr = _WATERMARK_POSITIONS.get(position, _WATERMARK_POSITIONS["bottom_right"])
    # Scale watermark to 12% of video width
    filter_graph = (
        f"[1]scale=iw*0.12:-1,format=rgba,colorchannelmixer=aa={alpha:.2f}[wm];"
        f"[0][wm]overlay={pos_expr}:format=auto,format=yuv420p"
    )
    return _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(watermark),
        "-filter_complex", filter_graph,
        "-c:a", "copy",
        str(out_path),
    ])


def _apply_text_only(
    video_path: Path,
    text: str,
    position: str,
    out_path: Path,
) -> bool:
    px, py = _TEXT_POSITIONS.get(position, _TEXT_POSITIONS["bottom_left"])
    # Escape special FFmpeg drawtext characters
    safe_text = text.replace("'", "\\'").replace(":", "\\:")
    vf = (
        f"drawtext=text='{safe_text}':"
        f"x={px}:y={py}:"
        f"fontsize=36:fontcolor=white:"
        f"shadowcolor=black:shadowx=2:shadowy=2:"
        f"box=1:boxcolor=black@0.4:boxborderw=6"
    )
    return _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", vf,
        "-c:a", "copy",
        str(out_path),
    ])


def _apply_both(
    video_path: Path,
    watermark: Path,
    wm_position: str,
    wm_alpha: float,
    text: str,
    text_position: str,
    out_path: Path,
) -> bool:
    wm_pos_expr = _WATERMARK_POSITIONS.get(wm_position, _WATERMARK_POSITIONS["bottom_right"])
    tx, ty = _TEXT_POSITIONS.get(text_position, _TEXT_POSITIONS["bottom_left"])
    safe_text = text.replace("'", "\\'").replace(":", "\\:")

    filter_graph = (
        f"[1]scale=iw*0.12:-1,format=rgba,colorchannelmixer=aa={wm_alpha:.2f}[wm];"
        f"[0][wm]overlay={wm_pos_expr}:format=auto,format=yuv420p,"
        f"drawtext=text='{safe_text}':"
        f"x={tx}:y={ty}:"
        f"fontsize=36:fontcolor=white:"
        f"shadowcolor=black:shadowx=2:shadowy=2:"
        f"box=1:boxcolor=black@0.4:boxborderw=6[out]"
    )
    return _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(watermark),
        "-filter_complex", filter_graph,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:a", "copy",
        str(out_path),
    ])


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing branding.py...")
    try:
        from preferences import load_preferences
        prefs = load_preferences()
        branding = prefs.get("branding", {})
        print(f"  watermark_image:    {branding.get('watermark_image') or '(not set)'}")
        print(f"  watermark_position: {branding.get('watermark_position', 'bottom_right')}")
        print(f"  watermark_opacity:  {branding.get('watermark_opacity', 0.7)}")
        print(f"  show_channel_name:  {branding.get('show_channel_name', False)}")
        print(f"  channel_name_text:  {branding.get('channel_name_text') or '(not set)'}")
        print(f"  channel_name_pos:   {branding.get('channel_name_position', 'bottom_left')}")
        print("\nBranding config loaded. Pass a video path to test overlay:")
        print("  python branding.py <path/to/video.mp4>")

        if len(sys.argv) > 1:
            result = apply_branding(sys.argv[1], user_prefs=prefs)
            if result:
                print(f"\nBranding applied → {result}")
            else:
                print("\nNo branding applied (not configured or nothing to add).")
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
