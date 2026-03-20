"""
compiler.py
===========
Groups scored clips into post packages targeting the preferred video length.

Rules (enforced strictly):
  - Lead each package with the highest-scored clip.
  - Never mix manual and automated clips in the same package.
  - Manual clips always get their own dedicated package.
  - Never exceed max_clips_per_compilation from preferences.
  - Target the preferred length range (min_sec to max_sec).
  - For medium_short, try to reach 60 seconds (the monetization threshold).

Returns post packages with template and caption style attached, ready to
be handed to editor.py for video production.
"""

import logging
from typing import Any, Dict, List, Tuple, Optional

from preferences import get_clip_length_range

# ── IRL vs Gaming theme sets ───────────────────────────────────────────────────
_IRL_THEMES    = {"PRANK", "CONFRONTATION", "TRAVEL", "SOCIAL", "DATE", "VIRAL_MOMENT"}
_GAMING_THEMES = {"FUNNY", "RAGE", "SHOCKED", "CLUTCH", "WHOLESOME", "DRAMA",
                  "FAIL", "REACTION"}

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def compile_packages(
    clips: List[Dict[str, Any]],
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
) -> List[Dict[str, Any]]:
    """
    Group scored clips into post packages.

    Separates manual clips from automated clips first, then builds packages
    independently for each group.

    Args:
        clips: List of scored clip dicts (output from scorer.py).
                Must already be sorted best-first (scorer.score_clips does this).
        user_prefs: User preferences. If None, loaded from preferences.yaml.
        user_id: Reserved for SaaS.

    Returns:
        List of package dicts, each containing:
            clip_ids      (list[int]) — clip_id values from the database
            clips         (list[dict])— full clip dicts (for editor.py convenience)
            total_duration (float)   — sum of clip durations
            template      (int)      — template ID to apply
            caption_style (int)      — caption style ID to apply
            mode          (str)      — "auto" or "manual"
            user_id       (int)      — user ID
    """
    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    clip_length_pref  = user_prefs.get("clip_length", "medium")
    min_sec, max_sec  = get_clip_length_range(clip_length_pref)
    max_per_package   = user_prefs.get("max_clips_per_compilation", 2)
    auto_template     = user_prefs.get("default_video_template", 1)
    manual_template   = user_prefs.get("manual_mode_default_template", 1)
    caption_style     = user_prefs.get("default_caption_style", 1)

    # Separate manual and automated clips
    manual_clips = [c for c in clips if c.get("mode") == "manual"]
    auto_clips   = [c for c in clips if c.get("mode") != "manual"]

    # Both groups are assumed to already be sorted by score descending
    packages: List[Dict[str, Any]] = []

    # ── Manual packages (one clip per package — never combined with auto) ──────
    for clip in manual_clips:
        pkg = _make_package(
            selected_clips=[clip],
            template=manual_template,
            caption_style=caption_style,
            mode="manual",
            user_id=user_id,
        )
        packages.append(pkg)
        logger.info(
            "Manual package created: '%s' (%.1fs)",
            clip.get("title", "")[:50], pkg["total_duration"],
        )

    # ── Automated packages ─────────────────────────────────────────────────────
    if auto_clips:
        # Split IRL and gaming clips into separate groups before packaging.
        # Never mix IRL themes (PRANK, TRAVEL, SOCIAL…) with gaming themes.
        irl_clips    = [c for c in auto_clips if c.get("theme") in _IRL_THEMES]
        gaming_clips = [c for c in auto_clips if c.get("theme") not in _IRL_THEMES]

        for clip_group in [gaming_clips, irl_clips]:
            if not clip_group:
                continue
            group_packages = _build_auto_packages(
                clips=clip_group,
                min_sec=min_sec,
                max_sec=max_sec,
                max_per_package=max_per_package,
                template=auto_template,
                caption_style=caption_style,
                clip_length_pref=clip_length_pref,
                user_id=user_id,
            )
            packages.extend(group_packages)

    logger.info(
        "Compiler produced %d package(s): %d manual, %d automated.",
        len(packages), len(manual_clips), len(packages) - len(manual_clips),
    )
    return packages


def describe_packages(packages: List[Dict[str, Any]]) -> str:
    """
    Return a human-readable summary of compiled packages (for logging/display).
    """
    lines = []
    for i, pkg in enumerate(packages, start=1):
        clips_str = ", ".join(
            f"'{c.get('title', '')[:30]}'" for c in pkg.get("clips", [])
        )
        lines.append(
            f"  Package {i}: [{pkg['mode']}] {pkg['total_duration']:.1f}s | "
            f"template={pkg['template']} | {len(pkg['clips'])} clip(s): {clips_str}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _build_auto_packages(
    clips: List[Dict[str, Any]],
    min_sec: float,
    max_sec: float,
    max_per_package: int,
    template: int,
    caption_style: int,
    clip_length_pref: str,
    user_id: int,
) -> List[Dict[str, Any]]:
    """
    Build automated post packages from a sorted list of clips.

    Greedy algorithm: start a package with the best available clip,
    then fill it with the next best clips until:
      - Adding the next clip would exceed max_sec, OR
      - We have reached max_per_package clips.

    If a single clip already meets or exceeds min_sec, it forms its own package.
    If a single clip is under min_sec, it is paired with another short clip.
    """
    # For medium_short, the target is to reach the monetization floor (60s)
    target_floor = 60.0 if clip_length_pref == "medium_short" else min_sec

    packages: List[Dict[str, Any]] = []
    remaining = list(clips)  # Copy so we can pop from it

    while remaining:
        selected: List[Dict[str, Any]] = []
        running_duration = 0.0

        # Always lead with the highest-scored remaining clip
        lead = remaining.pop(0)
        selected.append(lead)
        running_duration += lead.get("duration") or 0.0

        # Try to fill the package
        to_remove = []
        for candidate in remaining:
            if len(selected) >= max_per_package:
                break

            candidate_duration = candidate.get("duration") or 0.0

            # Don't add a clip if it would exceed max_sec
            if running_duration + candidate_duration > max_sec:
                continue

            selected.append(candidate)
            running_duration += candidate_duration
            to_remove.append(candidate)

            # Stop adding if we're comfortably within range
            if running_duration >= target_floor:
                break

        # Remove used clips from remaining
        for used in to_remove:
            remaining.remove(used)

        pkg = _make_package(
            selected_clips=selected,
            template=template,
            caption_style=caption_style,
            mode="auto",
            user_id=user_id,
        )
        packages.append(pkg)

        logger.info(
            "Auto package created: %d clip(s), %.1fs total "
            "(target: %.0f–%.0f s)",
            len(selected), running_duration, min_sec, max_sec,
        )

    return packages


def _make_package(
    selected_clips: List[Dict[str, Any]],
    template: int,
    caption_style: int,
    mode: str,
    user_id: int,
) -> Dict[str, Any]:
    """
    Assemble a package dict from a list of selected clips.

    Args:
        selected_clips: List of clip dicts (already sorted best-first).
        template: Template ID.
        caption_style: Caption style ID.
        mode: "auto" or "manual".
        user_id: User ID.

    Returns:
        Package dict ready for database.insert_package() and editor.py.
    """
    clip_ids = [c["clip_id"] for c in selected_clips if c.get("clip_id")]
    total_duration = sum(c.get("duration") or 0.0 for c in selected_clips)

    return {
        "clip_ids":       clip_ids,
        "clips":          selected_clips,   # Full dicts for editor convenience
        "total_duration": round(total_duration, 2),
        "template":       template,
        "caption_style":  caption_style,
        "mode":           mode,
        "user_id":        user_id,
        "status":         "pending",
    }


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing compiler.py...")
    print()

    try:
        from preferences import load_preferences
        prefs = load_preferences()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Simulated scored clips (what scorer.py would return)
    test_clips = [
        {
            "clip_id": 1, "title": "Insane clutch — target streamer",
            "creator_name": "shroud", "source": "twitch",
            "duration": 45.0, "score": 88.0, "mode": "auto",
        },
        {
            "clip_id": 2, "title": "Epic fail compilation",
            "creator_name": "pokimane", "source": "twitch",
            "duration": 30.0, "score": 72.0, "mode": "auto",
        },
        {
            "clip_id": 3, "title": "Speedrun world record attempt",
            "creator_name": "xqc", "source": "twitch",
            "duration": 55.0, "score": 65.0, "mode": "auto",
        },
        {
            "clip_id": 4, "title": "My own clip — manual drop",
            "creator_name": None, "source": "manual",
            "duration": 60.0, "score": 100.0, "mode": "manual",
        },
    ]

    packages = compile_packages(test_clips, user_prefs=prefs)

    print(f"Produced {len(packages)} package(s):\n")
    print(describe_packages(packages))

    for i, pkg in enumerate(packages, start=1):
        print(f"\nPackage {i} details:")
        print(f"  mode:           {pkg['mode']}")
        print(f"  template:       {pkg['template']}")
        print(f"  caption_style:  {pkg['caption_style']}")
        print(f"  total_duration: {pkg['total_duration']}s")
        print(f"  clip_ids:       {pkg['clip_ids']}")
        print(f"  clips:          {[c['title'][:30] for c in pkg['clips']]}")

    print("\nCompiler test complete.")
