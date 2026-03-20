"""
blocked_creators.py
===================
Manages the list of creators who have requested opt-out from ClipCast
automated clip processing.

The list lives in legal/blocked_creators.yaml and is checked before any
clip from a given creator is included in the pipeline. Blocked-creator
encounters are logged silently to the audit trail.

Framing: These are creators who have exercised their right to opt out.
Respecting opt-out requests is the right thing to do — and it protects
ClipCast operators from DMCA disputes before they start.

Test:
    python blocked_creators.py
"""

import logging
import yaml
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

# ── File path ──────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
BLOCKED_FILE = BASE_DIR / "legal" / "blocked_creators.yaml"


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def load_blocked() -> Dict[str, List[str]]:
    """
    Load the blocked creators list.

    Returns:
        Dict with 'twitch' and 'youtube' keys, each a list of lowercase names.
        Returns empty lists if the file doesn't exist or is malformed.
    """
    if not BLOCKED_FILE.exists():
        return {"twitch": [], "youtube": []}

    try:
        with open(BLOCKED_FILE) as f:
            data = yaml.safe_load(f) or {}
        blocked = data.get("blocked") or {}
        return {
            "twitch":  [str(x).lower().lstrip("@") for x in (blocked.get("twitch") or [])],
            "youtube": [str(x).lower().lstrip("@") for x in (blocked.get("youtube") or [])],
        }
    except Exception as e:
        logger.warning("blocked_creators: could not load file: %s", e)
        return {"twitch": [], "youtube": []}


def is_blocked(name: str, platform: str) -> bool:
    """
    Check whether a creator is on the opt-out list.

    Args:
        name:     Creator name (Twitch username or YouTube handle/channel ID).
        platform: 'twitch' or 'youtube'.

    Returns:
        True if the creator has opted out and should be skipped.
    """
    if not name:
        return False
    blocked_list = load_blocked().get(platform.lower(), [])
    return name.lower().lstrip("@") in blocked_list


def add_blocked(name: str, platform: str) -> bool:
    """
    Add a creator to the opt-out list.

    Args:
        name:     Creator name to add.
        platform: 'twitch' or 'youtube'.

    Returns:
        True if added, False if already present.
    """
    BLOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    blocked = load_blocked()
    name_clean = name.lower().lstrip("@")
    platform_list = blocked.get(platform.lower(), [])

    if name_clean in platform_list:
        return False

    platform_list.append(name_clean)
    blocked[platform.lower()] = platform_list
    _save_blocked(blocked)
    logger.info("Added %s/%s to opt-out list.", platform, name_clean)
    return True


def remove_blocked(name: str, platform: str) -> bool:
    """
    Remove a creator from the opt-out list.

    Args:
        name:     Creator name to remove.
        platform: 'twitch' or 'youtube'.

    Returns:
        True if removed, False if not found.
    """
    blocked = load_blocked()
    name_clean = name.lower().lstrip("@")
    platform_list = blocked.get(platform.lower(), [])

    if name_clean not in platform_list:
        return False

    blocked[platform.lower()] = [x for x in platform_list if x != name_clean]
    _save_blocked(blocked)
    logger.info("Removed %s/%s from opt-out list.", platform, name_clean)
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _save_blocked(blocked: Dict[str, List[str]]) -> None:
    """Write the blocked dict back to legal/blocked_creators.yaml, preserving the header comment."""
    BLOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# ── Creators Who Have Requested Opt-Out ──────────────────────────────────────\n"
        "#\n"
        "# Add Twitch usernames or YouTube channel IDs / @handles here to exclude\n"
        "# their content from all automated ClipCast processing.\n"
        "#\n"
        "# Manage this list with:  python main.py --blocked\n"
        "#\n\n"
    )
    body = yaml.dump(
        {"blocked": {"twitch": blocked.get("twitch", []), "youtube": blocked.get("youtube", [])}},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    BLOCKED_FILE.write_text(header + body, encoding="utf-8")


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print("Testing blocked_creators.py...\n")

    added = add_blocked("teststreamer", "twitch")
    print(f"Added teststreamer/twitch: {added}  (expected: True)")

    result = is_blocked("teststreamer", "twitch")
    print(f"Is teststreamer blocked: {result}  (expected: True)")

    result = is_blocked("TESTSTREAMER", "twitch")
    print(f"Is TESTSTREAMER (uppercase) blocked: {result}  (expected: True)")

    result = is_blocked("anotherstreamer", "twitch")
    print(f"Is anotherstreamer blocked: {result}  (expected: False)")

    removed = remove_blocked("teststreamer", "twitch")
    print(f"Removed teststreamer: {removed}  (expected: True)")

    result = is_blocked("teststreamer", "twitch")
    print(f"Is teststreamer blocked after removal: {result}  (expected: False)")

    print(f"\nAll tests passed. Blocked file: {BLOCKED_FILE}")
