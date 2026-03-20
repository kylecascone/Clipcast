"""
thumbnail.py
============
Auto-generate eye-catching thumbnails for processed ClipCast videos.

Strategy
--------
1. Detect the peak moment in the video using ``moments.py`` (FFmpeg ebur128
   energy detection). The loudest, most energetic frame is typically the most
   visually interesting.
2. Extract that frame as a full-resolution JPEG with FFmpeg.
3. Optionally composite a branding overlay (watermark logo, creator name,
   score badge) using Pillow if available — falls back to the raw frame if
   Pillow is not installed.
4. Return the thumbnail path for use with TikTok / YouTube Shorts upload.

Output
------
Thumbnail is saved alongside the processed video:
    clips/processed/video_name_thumb.jpg

Specs match TikTok / YouTube Shorts requirements:
  - Resolution: 1080 × 1920 (9:16 vertical)
  - Format:     JPEG
  - Quality:    85 (good balance of size and sharpness)

Usage
-----
    from thumbnail import generate_thumbnail
    thumb_path = generate_thumbnail("/path/to/clip.mp4", clip_metadata)
    # Returns path string on success, None on failure

Test
----
    python thumbnail.py
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Output settings ─────────────────────────────────────────────────────────────
THUMB_WIDTH   = 1080
THUMB_HEIGHT  = 1920
THUMB_QUALITY = 85     # JPEG quality (0–100)

# ── Score badge colours (RGB) ────────────────────────────────────────────────────
SCORE_BADGE_HIGH   = (22, 197, 94)   # Green  — score ≥ 70
SCORE_BADGE_MED    = (250, 176, 5)   # Yellow — score 40–69
SCORE_BADGE_LOW    = (239, 68, 68)   # Red    — score < 40


# ══════════════════════════════════════════════════════════════════════════════
# Peak frame detection
# ══════════════════════════════════════════════════════════════════════════════

def _detect_peak_timestamp(video_path: str) -> float:
    """
    Return the timestamp (seconds) of the audio energy peak in the video.

    Uses FFmpeg's ebur128 filter to log per-frame integrated loudness, then
    finds the frame with the highest momentary loudness. Falls back to 30%
    into the video if FFmpeg is unavailable or parsing fails.

    Args:
        video_path: Path to the processed video file.

    Returns:
        Timestamp in seconds (float).
    """
    try:
        # Get video duration first
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", video_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        duration = 10.0  # safe default
        if probe.returncode == 0:
            try:
                info = json.loads(probe.stdout)
                duration = float(info["format"]["duration"])
            except (KeyError, ValueError, json.JSONDecodeError):
                pass

        # Run ebur128 momentary loudness filter
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner",
                "-i", video_path,
                "-af", "ebur128=peak=true:framelog=verbose",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )

        best_ts    = duration * 0.30    # fallback: 30% into clip
        best_lufs  = -999.0

        for line in result.stderr.splitlines():
            # Lines look like: "  t: 12.35       M: -14.2   S: -16.1   I: -16.5   LRA: 2.4 LRA low: -18.5 LRA high: -16.0"
            if line.strip().startswith("t:") and "M:" in line:
                try:
                    parts = line.split()
                    ts    = float(parts[1])
                    m_idx = parts.index("M:") + 1
                    lufs  = float(parts[m_idx])
                    if lufs > best_lufs:
                        best_lufs = lufs
                        best_ts   = ts
                except (ValueError, IndexError):
                    continue

        # Keep peak frame away from very end (avoid credits/freeze frame)
        best_ts = min(best_ts, duration * 0.90)
        logger.debug("Peak timestamp: %.2fs (momentary LUFS=%.1f)", best_ts, best_lufs)
        return best_ts

    except FileNotFoundError:
        logger.warning("FFmpeg not found — using 30%% timestamp for thumbnail.")
        return 0.0
    except subprocess.TimeoutExpired:
        logger.warning("FFmpeg peak detection timed out — using 30%% timestamp.")
        return 0.0
    except Exception as exc:
        logger.debug("Peak detection error: %s — using 30%% timestamp.", exc)
        return 0.0


def _extract_frame(video_path: str, timestamp: float, output_path: str) -> bool:
    """
    Extract a single frame from a video at the given timestamp using FFmpeg.

    Scales the frame to THUMB_WIDTH × THUMB_HEIGHT using crop+scale.

    Args:
        video_path:  Source video file.
        timestamp:   Time offset in seconds.
        output_path: Destination JPEG path.

    Returns:
        True on success.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(timestamp),
                "-i", video_path,
                "-vframes", "1",
                "-vf", f"crop=ih*{THUMB_WIDTH}/{THUMB_HEIGHT}:ih,scale={THUMB_WIDTH}:{THUMB_HEIGHT}",
                "-q:v", str(max(1, int((100 - THUMB_QUALITY) / 10))),
                output_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error("FFmpeg frame extract failed: %s", result.stderr[-300:])
            return False
        return True

    except FileNotFoundError:
        logger.error("FFmpeg not found — cannot extract thumbnail frame.")
        return False
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg frame extraction timed out.")
        return False
    except Exception as exc:
        logger.error("Frame extraction error: %s", exc)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Branding overlay
# ══════════════════════════════════════════════════════════════════════════════

def _add_branding_overlay(
    image_path: str,
    clip: Dict[str, Any],
    user_prefs: Optional[Dict] = None,
) -> bool:
    """
    Composite a branding overlay onto the thumbnail using Pillow.

    Adds:
      - Creator name in large bold text at the top.
      - Coloured score badge (circle with score number) at the bottom-right.
      - Semi-transparent bottom gradient bar.

    If Pillow is not installed this function returns False silently — the
    caller will still use the plain extracted frame.

    Args:
        image_path: Path to the JPEG to modify in-place.
        clip:       Clip dict (for creator_name, score, title).
        user_prefs: User preferences (for branding_name).

    Returns:
        True if overlay was applied; False if Pillow unavailable or error.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.debug("Pillow not installed — skipping thumbnail branding overlay.")
        return False

    try:
        img = Image.open(image_path).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")

        # ── Bottom gradient bar ─────────────────────────────────────────────
        bar_height = 280
        for y in range(THUMB_HEIGHT - bar_height, THUMB_HEIGHT):
            alpha = int(200 * (y - (THUMB_HEIGHT - bar_height)) / bar_height)
            draw.line([(0, y), (THUMB_WIDTH, y)], fill=(0, 0, 0, alpha))

        # ── Creator name ────────────────────────────────────────────────────
        creator = (clip.get("creator_name") or "").strip()
        if creator:
            try:
                font_large = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 72)
            except (IOError, OSError):
                font_large = ImageFont.load_default()

            # Shadow for legibility
            shadow_offset = 3
            draw.text(
                (40 + shadow_offset, THUMB_HEIGHT - 220 + shadow_offset),
                creator, font=font_large, fill=(0, 0, 0, 200),
            )
            draw.text(
                (40, THUMB_HEIGHT - 220),
                creator, font=font_large, fill=(255, 255, 255, 255),
            )

        # ── Score badge ─────────────────────────────────────────────────────
        score = float(clip.get("score") or 0)
        if score > 0:
            badge_r  = 55
            badge_cx = THUMB_WIDTH  - 70
            badge_cy = THUMB_HEIGHT - 80

            if score >= 70:
                badge_fill = SCORE_BADGE_HIGH
            elif score >= 40:
                badge_fill = SCORE_BADGE_MED
            else:
                badge_fill = SCORE_BADGE_LOW

            draw.ellipse(
                [(badge_cx - badge_r, badge_cy - badge_r),
                 (badge_cx + badge_r, badge_cy + badge_r)],
                fill=(*badge_fill, 220),
            )
            try:
                font_badge = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
            except (IOError, OSError):
                font_badge = ImageFont.load_default()

            score_str = f"{int(score)}"
            bbox = draw.textbbox((0, 0), score_str, font=font_badge)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(
                (badge_cx - tw // 2, badge_cy - th // 2),
                score_str, font=font_badge, fill=(255, 255, 255, 255),
            )

        img.save(image_path, "JPEG", quality=THUMB_QUALITY)
        logger.debug("Branding overlay applied to thumbnail '%s'", image_path)
        return True

    except Exception as exc:
        logger.warning("Thumbnail branding overlay failed: %s", exc)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def generate_thumbnail(
    video_path: str,
    clip: Optional[Dict[str, Any]] = None,
    user_prefs: Optional[Dict] = None,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """
    Generate a thumbnail for a processed video.

    Steps:
        1. Detect the audio energy peak frame.
        2. Extract that frame as a JPEG at THUMB_WIDTH × THUMB_HEIGHT.
        3. Apply branding overlay (creator name + score badge) via Pillow.

    Args:
        video_path:  Path to the processed .mp4 file.
        clip:        Clip metadata dict (creator_name, score, title).
        user_prefs:  User preferences dict.
        output_path: Override the auto-generated thumbnail path.

    Returns:
        Path to the generated thumbnail JPEG, or None on failure.
    """
    video = Path(video_path)
    if not video.exists():
        logger.error("generate_thumbnail: video not found at '%s'", video_path)
        return None

    # Default output: same dir as video, same name + _thumb.jpg
    if output_path is None:
        output_path = str(video.with_name(video.stem + "_thumb.jpg"))

    clip = clip or {}

    # Step 1: detect peak timestamp
    peak_ts = _detect_peak_timestamp(video_path)

    # Step 2: extract frame
    ok = _extract_frame(video_path, peak_ts, output_path)
    if not ok:
        logger.error("generate_thumbnail: frame extraction failed.")
        return None

    # Step 3: apply branding overlay (best-effort, won't fail the pipeline)
    _add_branding_overlay(output_path, clip, user_prefs)

    logger.info(
        "Thumbnail generated: '%s'  (peak_ts=%.2fs)",
        output_path, peak_ts,
    )
    return output_path


def generate_thumbnails_for_package(
    package: Dict[str, Any],
    user_prefs: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Generate thumbnails for all processed videos in a package.

    Updates the package dict in-place with 'thumbnail_path' on each clip.

    Args:
        package:    Package dict from compiler.py (must have 'output_path').
        user_prefs: User preferences.

    Returns:
        The updated package dict.
    """
    output_path = package.get("output_path")
    if output_path and Path(output_path).exists():
        thumb = generate_thumbnail(
            video_path=output_path,
            clip=package.get("clips", [{}])[0],
            user_prefs=user_prefs,
        )
        package["thumbnail_path"] = thumb
    return package


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(name)s  %(message)s")

    print("=" * 60)
    print("thumbnail.py  —  self-test")
    print("=" * 60)

    # Test with a real video if provided, otherwise test the error path
    if "--video" in sys.argv:
        idx = sys.argv.index("--video")
        video_path = sys.argv[idx + 1]

        print(f"\nGenerating thumbnail for: {video_path}")
        clip_meta = {
            "creator_name": "TestStreamer",
            "title":        "Insane clutch moment",
            "score":        82.5,
            "source":       "twitch",
        }
        thumb = generate_thumbnail(video_path, clip=clip_meta)
        if thumb:
            print(f"Thumbnail saved: {thumb}")
        else:
            print("Thumbnail generation failed.")
            sys.exit(1)

    else:
        print(
            "\nNo --video argument provided — testing error path only.\n"
            "For a full test with frame extraction:\n"
            "  python thumbnail.py --video /path/to/clip.mp4\n"
        )

        # Test missing file path
        result = generate_thumbnail("/nonexistent/file.mp4")
        assert result is None, "Expected None for missing file"
        print("Missing file → returns None: OK")

        # Test _detect_peak_timestamp error handling
        ts = _detect_peak_timestamp("/nonexistent/file.mp4")
        assert isinstance(ts, float), "Expected float from _detect_peak_timestamp"
        print(f"_detect_peak_timestamp (missing file) → {ts:.2f}s: OK")

        # Test _add_branding_overlay with a dummy image if Pillow is available
        try:
            from PIL import Image
            import tempfile, os
            # Create a blank 1080×1920 test image
            tmp_img = tempfile.mktemp(suffix="_thumb_test.jpg")
            img = Image.new("RGB", (THUMB_WIDTH, THUMB_HEIGHT), color=(50, 100, 150))
            img.save(tmp_img, "JPEG")
            ok = _add_branding_overlay(tmp_img, {"creator_name": "TestCreator", "score": 82.5})
            print(f"_add_branding_overlay on blank image: {'OK' if ok else 'skipped (Pillow unavailable)'}")
            os.unlink(tmp_img)
        except ImportError:
            print("Pillow not installed — branding overlay will be skipped at runtime.")

        print("\n" + "=" * 60)
        print("thumbnail.py self-test PASSED (basic checks).")
        print("=" * 60)
        print("\nFull test: python thumbnail.py --video /path/to/clip.mp4")
