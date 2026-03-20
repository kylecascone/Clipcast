"""
music_detector.py
=================
Detects whether a clip likely contains background music using FFmpeg audio
frequency analysis.

Heuristic:
  Music typically has significant sustained low-frequency energy (bass lines,
  kick drums) relative to its overall audio level. Voice-only and game audio
  alone tend to have much weaker bass relative to their midrange content.

  1. Measure overall RMS level with FFmpeg volumedetect.
  2. Measure bass-band RMS level (low-pass ≤ 300 Hz) the same way.
  3. If the bass RMS is within 8 dB of the overall RMS, flag as likely music.

This is a fast heuristic — it catches most music-heavy clips but is not
infallible. False positives (explosions, low-register game effects) are
possible. The flag is advisory only; processing continues regardless.

DMCA Note:
    Music in clips is the #1 source of takedowns on TikTok, YouTube, and
    Instagram. Heed has_music=1 warnings seriously before posting.

SaaS Note:
    All functions are stateless. Safe to call concurrently for multiple users.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from rich.console import Console

logger  = logging.getLogger(__name__)
_console = Console()

# Bass energy must be within this many dB of overall energy to flag as music.
# Lower = more sensitive (more flags). Higher = less sensitive (fewer flags).
_BASS_THRESHOLD_DB = 8.0


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def detect_music(video_path: str | Path) -> bool:
    """
    Analyze a video's audio to determine if it likely contains background music.

    Args:
        video_path: Path to the downloaded video file.

    Returns:
        True if music is likely present, False otherwise.
        Returns False (safe assumption) if analysis cannot complete.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        logger.error("music_detector: file not found: %s", video_path)
        return False

    overall_rms = _measure_rms(video_path, lowpass_hz=None)
    bass_rms    = _measure_rms(video_path, lowpass_hz=300)

    if overall_rms is None or bass_rms is None:
        logger.debug(
            "music_detector: could not measure audio levels for '%s'",
            video_path.name,
        )
        return False

    # Both values are dBFS (negative; closer to 0 = louder).
    # diff > 0 means bass is quieter than overall (normal for voice/game audio).
    # diff < threshold means bass is nearly as loud as overall → likely music.
    diff = overall_rms - bass_rms
    has_music = diff < _BASS_THRESHOLD_DB

    logger.debug(
        "music_detector '%s': overall=%.1f dBFS  bass=%.1f dBFS  "
        "diff=%.1f dB  threshold=%.1f dB  → %s",
        video_path.name, overall_rms, bass_rms, diff, _BASS_THRESHOLD_DB,
        "MUSIC" if has_music else "no music",
    )

    if has_music:
        logger.warning(
            "[DMCA WARNING] '%s' likely contains background music "
            "(bass %.1f dBFS vs overall %.1f dBFS, diff %.1f dB < %.1f dB threshold). "
            "Clip flagged has_music=1.",
            video_path.name, bass_rms, overall_rms, diff, _BASS_THRESHOLD_DB,
        )
        # Friendly, non-alarming terminal notice — informs without blocking
        _console.print(
            f"  [yellow]Note:[/yellow] [dim]{video_path.name}[/dim] may contain background music. "
            "TikTok may mute or remove it."
        )

    return has_music


def flag_clip_in_database(clip_id: int, has_music: bool) -> None:
    """
    Update the has_music column for a clip in the database.

    Args:
        clip_id:   Database clip_id to update.
        has_music: True if music was detected.
    """
    try:
        import database
        database.update_clip_field(clip_id, "has_music", int(has_music))
        logger.debug("Flagged clip_id=%d has_music=%s", clip_id, has_music)
    except Exception as e:
        logger.error(
            "music_detector: failed to update has_music for clip_id=%d: %s",
            clip_id, e,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _measure_rms(
    video_path: Path,
    lowpass_hz: Optional[int],
) -> Optional[float]:
    """
    Run FFmpeg volumedetect (optionally after a low-pass filter) and return
    the mean_volume in dBFS.

    Args:
        video_path: Path to the video file.
        lowpass_hz: If set, apply a low-pass filter at this Hz before measuring.

    Returns:
        Mean volume in dBFS (e.g. -23.4), or None if parsing fails.
    """
    if lowpass_hz:
        af = f"lowpass=f={lowpass_hz},volumedetect"
    else:
        af = "volumedetect"

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-nostats", "-i", str(video_path),
                "-af", af,
                "-vn", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("music_detector: FFmpeg failed: %s", e)
        return None

    # volumedetect prints e.g. "mean_volume: -23.4 dB" to stderr
    m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", result.stderr)
    if m:
        return float(m.group(1))

    return None


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python music_detector.py <path/to/video.mp4>")
        sys.exit(1)

    path = sys.argv[1]
    print(f"Analyzing: {path}\n")

    result = detect_music(path)
    if result:
        print("Result: MUSIC DETECTED — DMCA risk. Review before posting.")
    else:
        print("Result: No music detected (or analysis inconclusive).")
