"""
moments.py
==========
Finds the peak audio energy moment in a video using FFmpeg's ebur128 filter.

Returns a (start, end) timestamp pair for the most energetically dense section
of a clip, targeting the preferred clip length from preferences. The editor
can then extract just that section instead of always using the full clip.

This means a 10-minute YouTube video can be automatically trimmed to the
single most exciting 60–90-second window before template processing.

SaaS Note:
    All functions are stateless and accept explicit parameters. No global
    state — safe to call concurrently for different users / packages.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def find_peak_moment(
    video_path: str | Path,
    target_duration: float,
    min_start: float = 0.0,
) -> Tuple[float, float]:
    """
    Analyze a video's audio and return (start, end) timestamps of the most
    energetically dense section of the given duration.

    Strategy:
      1. Probe total duration via ffprobe.
      2. If the clip is already short enough, return it in full.
      3. Run FFmpeg with the ebur128 filter to get per-moment loudness samples
         (~10 samples/second).
      4. Slide a window of target_duration seconds across all samples and
         pick the window with the highest average momentary loudness.

    Args:
        video_path:      Path to the source video file.
        target_duration: Target clip length in seconds (from preferences).
        min_start:       Earliest allowable start time in seconds (default 0).

    Returns:
        (start, end) tuple in seconds.
        Falls back to (min_start, min_start + target_duration) if analysis fails.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        logger.error("moments.find_peak_moment: file not found: %s", video_path)
        return (min_start, min_start + target_duration)

    total = _probe_duration(video_path)
    if total is None:
        return (min_start, min_start + target_duration)

    # Clip already within target — return it in full
    if total <= target_duration + 2.0:
        return (0.0, total)

    samples = _extract_ebur128_samples(video_path)
    if not samples:
        logger.info(
            "moments: no ebur128 data for '%s', returning first %.0fs.",
            video_path.name, target_duration,
        )
        end = min(min_start + target_duration, total)
        return (min_start, end)

    start, end = _find_peak_window(samples, target_duration, min_start, total)
    logger.info(
        "moments: peak %.1f–%.1f s (of %.1f s total) for '%s'",
        start, end, total, video_path.name,
    )
    return (start, end)


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _probe_duration(video_path: Path) -> Optional[float]:
    """Return video duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _extract_ebur128_samples(
    video_path: Path,
) -> List[Tuple[float, float]]:
    """
    Run FFmpeg with the ebur128 filter and parse per-moment loudness values.

    FFmpeg prints lines like:
        t: 1.234  M: -18.5  S: -19.0  I: -18.0  LRA: 2.1

    'M' (momentary loudness, 400 ms window) is sampled ~10 times per second.
    Returns a list of (timestamp_sec, loudness_LUFS) tuples.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-nostats", "-i", str(video_path),
                "-af", "ebur128=metadata=1",
                "-vn", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("moments: FFmpeg ebur128 failed: %s", e)
        return []

    pattern = re.compile(r"t:\s*([\d.]+)\s+M:\s*([-\d.inf]+)")
    samples: List[Tuple[float, float]] = []

    for line in result.stderr.splitlines():
        m = pattern.search(line)
        if m:
            t = float(m.group(1))
            loudness_str = m.group(2)
            try:
                loudness = float(loudness_str)
            except ValueError:
                loudness = -70.0  # -inf or other non-numeric → treat as silence
            # Cap at -70 so silent sections don't distort the window average
            if loudness < -70.0:
                loudness = -70.0
            samples.append((t, loudness))

    logger.debug(
        "moments: %d ebur128 samples parsed from '%s'",
        len(samples), video_path.name,
    )
    return samples


def _find_peak_window(
    samples: List[Tuple[float, float]],
    target_duration: float,
    min_start: float,
    total_duration: float,
) -> Tuple[float, float]:
    """
    Slide a window of target_duration seconds over loudness samples and return
    the (start, end) pair with the highest average momentary loudness.
    """
    if not samples:
        end = min(min_start + target_duration, total_duration)
        return (min_start, end)

    best_start = min_start
    best_score = float("-inf")
    n = len(samples)

    for i in range(n):
        t_start = samples[i][0]
        if t_start < min_start:
            continue
        t_end = t_start + target_duration
        if t_end > total_duration:
            break

        window = [s[1] for s in samples if t_start <= s[0] < t_end]
        if not window:
            continue
        score = sum(window) / len(window)

        if score > best_score:
            best_score = score
            best_start = t_start

    best_end = min(best_start + target_duration, total_duration)
    return (best_start, best_end)


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python moments.py <path/to/video.mp4> [target_duration_sec]")
        sys.exit(1)

    path = sys.argv[1]
    target = float(sys.argv[2]) if len(sys.argv) > 2 else 75.0

    print(f"Analyzing: {path}")
    print(f"Target duration: {target}s")

    start, end = find_peak_moment(path, target_duration=target)
    print(f"\nPeak moment: {start:.1f}s → {end:.1f}s  (duration: {end - start:.1f}s)")
    print(
        f"\nTo extract this section with FFmpeg:\n"
        f"  ffmpeg -i '{path}' -ss {start:.1f} -to {end:.1f} -c copy peak_moment.mp4"
    )
