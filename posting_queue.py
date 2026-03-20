"""
queue.py
========
Manages the posting queue — scheduling, display, and slot calculation.

Responsibilities:
  - Calculate the next available posting slot based on preferences.
  - Add compiled packages to the queue at the right time.
  - Display the current queue status in a formatted table.
  - Enforce daily post limits from preferences.
  - Handle immediate posting for manual clips (bypasses schedule).

SaaS Note:
    All public functions accept user_id. In multi-user mode, scheduling
    is per-user with independent posting times and daily limits.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table
from rich.text import Text

import database

logger = logging.getLogger(__name__)
console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# Slot calculation
# ══════════════════════════════════════════════════════════════════════════════

def get_next_available_slot(
    user_prefs: Dict[str, Any],
    user_id: int = 1,
) -> datetime:
    """
    Calculate the next available posting time slot.

    Logic:
      1. Get today's configured posting times from preferences.
      2. Find the first time that:
         a. Is in the future (at least 5 minutes from now).
         b. Does not already have a pending or posted queue item.
      3. If all of today's slots are taken, return the first slot tomorrow.

    Args:
        user_prefs: User preferences dict.
        user_id: User ID.

    Returns:
        datetime object for the next available slot.
    """
    posting_times: List[str] = user_prefs.get("posting_times", ["09:00"])
    post_frequency: int = user_prefs.get("post_frequency", 1)
    now = datetime.now()

    # Parse posting times as (hour, minute) tuples
    parsed_times: List[Tuple[int, int]] = []
    for t in posting_times:
        try:
            h, m = map(int, t.split(":"))
            parsed_times.append((h, m))
        except ValueError:
            logger.warning("Invalid posting time '%s' — skipping.", t)

    if not parsed_times:
        # Fallback: 1 hour from now
        return now + timedelta(hours=1)

    # Get already-scheduled times from the database
    pending_items = database.get_pending_queue(user_id=user_id)
    scheduled_set = {item["scheduled_time"][:16] for item in pending_items}

    # Check today's slots first, then tomorrow's, then the day after, etc.
    for day_offset in range(7):  # Look up to 7 days ahead
        check_date = (now + timedelta(days=day_offset)).date()

        for h, m in sorted(parsed_times):
            candidate = datetime(
                check_date.year, check_date.month, check_date.day, h, m
            )
            # Must be at least 5 minutes in the future
            if candidate < now + timedelta(minutes=5):
                continue

            # Must not already be scheduled
            candidate_str = candidate.strftime("%Y-%m-%dT%H:%M")
            if candidate_str in scheduled_set:
                continue

            return candidate

    # Fallback: 1 hour from now (should never reach this)
    return now + timedelta(hours=1)


def get_slots_for_today(
    user_prefs: Dict[str, Any],
    user_id: int = 1,
) -> List[Tuple[datetime, bool]]:
    """
    Return today's posting slots with availability flags.

    Args:
        user_prefs: User preferences dict.
        user_id: User ID.

    Returns:
        List of (slot_datetime, is_taken) tuples for today.
    """
    posting_times: List[str] = user_prefs.get("posting_times", ["09:00"])
    today = datetime.now().date()
    now = datetime.now()

    pending_items = database.get_pending_queue(user_id=user_id)
    scheduled_set = {item["scheduled_time"][:16] for item in pending_items}

    slots = []
    for t in posting_times:
        try:
            h, m = map(int, t.split(":"))
            slot_dt = datetime(today.year, today.month, today.day, h, m)
            slot_str = slot_dt.strftime("%Y-%m-%dT%H:%M")
            is_taken = slot_str in scheduled_set
            slots.append((slot_dt, is_taken))
        except ValueError:
            pass

    return slots


# ══════════════════════════════════════════════════════════════════════════════
# Adding to queue
# ══════════════════════════════════════════════════════════════════════════════

def add_package_to_queue(
    package_id: int,
    user_prefs: Dict[str, Any],
    mode: str = "auto",
    scheduled_time: Optional[datetime] = None,
    user_id: int = 1,
) -> Tuple[int, datetime]:
    """
    Add a compiled package to the posting queue.

    For manual immediate mode, schedules 1 minute from now.
    For scheduled mode (both auto and manual), finds the next available slot.

    Args:
        package_id: ID of the package to queue.
        user_prefs: User preferences dict.
        mode: "auto" or "manual".
        scheduled_time: Override the scheduled time (optional).
        user_id: User ID.

    Returns:
        Tuple of (queue_id, scheduled_datetime).
    """
    if scheduled_time:
        slot = scheduled_time
    elif (
        mode == "manual" and
        user_prefs.get("manual_mode_posting_behavior") == "immediate"
    ):
        # Post in 1 minute (gives time for any final confirmation)
        slot = datetime.now() + timedelta(minutes=1)
    else:
        slot = get_next_available_slot(user_prefs, user_id=user_id)

    queue_id = database.enqueue_package(
        package_id=package_id,
        scheduled_time=slot,
        user_id=user_id,
    )

    logger.info(
        "Package %d queued for %s (queue_id=%d, mode=%s)",
        package_id, slot.strftime("%Y-%m-%d %H:%M"), queue_id, mode,
    )
    return queue_id, slot


def add_package_immediately(
    package_id: int,
    user_id: int = 1,
) -> Tuple[int, datetime]:
    """
    Add a package to the queue to post in 1 minute (immediate mode).

    Returns:
        Tuple of (queue_id, scheduled_datetime).
    """
    slot = datetime.now() + timedelta(minutes=1)
    queue_id = database.enqueue_package(
        package_id=package_id,
        scheduled_time=slot,
        user_id=user_id,
    )
    logger.info("Package %d queued for immediate posting at %s", package_id, slot)
    return queue_id, slot


# ══════════════════════════════════════════════════════════════════════════════
# Queue display
# ══════════════════════════════════════════════════════════════════════════════

def display_queue(user_id: int = 1) -> None:
    """
    Print a formatted table of the current posting queue to the terminal.

    Shows:
      - Package ID
      - Mode (auto / manual)
      - Number of clips
      - Scheduled time
      - Time until post
      - Status
    """
    pending = database.get_pending_queue(user_id=user_id)
    now = datetime.now()

    if not pending:
        console.print("\n[yellow]Queue is empty.[/yellow] No posts are scheduled.\n")
        return

    table = Table(
        title=f"[bold cyan]Posting Queue[/bold cyan] ({len(pending)} item(s))",
        show_lines=True,
        border_style="cyan",
    )
    table.add_column("#",          style="dim",    width=4)
    table.add_column("Queue ID",   style="cyan",   width=10)
    table.add_column("Mode",       style="magenta",width=8)
    table.add_column("Template",   style="blue",   width=9)
    table.add_column("Clips",      style="white",  width=6)
    table.add_column("Scheduled",  style="green",  width=18)
    table.add_column("In",         style="yellow", width=12)
    table.add_column("Caption",    style="white",  width=40)

    for i, item in enumerate(pending, start=1):
        scheduled_str = item.get("scheduled_time", "")
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_str)
            delta = scheduled_dt - now
            if delta.total_seconds() < 0:
                time_until = "[red]Overdue[/red]"
            elif delta.total_seconds() < 3600:
                mins = int(delta.total_seconds() / 60)
                time_until = f"{mins}m"
            else:
                hours = delta.total_seconds() / 3600
                time_until = f"{hours:.1f}h"
            display_time = scheduled_dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            display_time = scheduled_str[:16] if scheduled_str else "?"
            time_until = "?"

        clip_ids   = item.get("clip_ids", [])
        caption    = (item.get("caption_text") or "Not generated yet")[:38]
        mode       = item.get("mode", "auto")
        template   = str(item.get("template", "?"))
        mode_str   = f"[cyan]{mode}[/cyan]" if mode == "auto" else f"[magenta]{mode}[/magenta]"

        table.add_row(
            str(i),
            str(item.get("queue_id")),
            mode_str,
            template,
            str(len(clip_ids)),
            display_time,
            time_until,
            caption,
        )

    console.print()
    console.print(table)
    console.print()


def display_status(user_id: int = 1) -> None:
    """
    Print a compact status summary to the terminal.

    Shows: last post time, next scheduled post, today's post count,
    and whether the scheduler is active.
    """
    last_post  = database.get_last_post_time(user_id=user_id)
    next_post  = database.get_next_scheduled_post(user_id=user_id)
    today_count = database.get_today_post_count(user_id=user_id)
    pending    = database.get_pending_queue(user_id=user_id)

    console.print("\n[bold cyan]ClipCast Studio — Status[/bold cyan]\n")
    console.print(f"  Last post        : {last_post or 'Never'}")
    console.print(f"  Next scheduled   : {next_post or 'Nothing scheduled'}")
    console.print(f"  Posts today      : {today_count}")
    console.print(f"  Queue depth      : {len(pending)} item(s) pending")
    console.print()


def get_due_items(user_id: int = 1) -> List[Dict[str, Any]]:
    """
    Return all queue items that are due to be posted (scheduled_time <= now).

    This is called by the scheduler to find what needs to be uploaded.

    Returns:
        List of pending queue item dicts that are overdue or due now.
    """
    pending = database.get_pending_queue(user_id=user_id)
    now = datetime.now()
    due = []

    for item in pending:
        scheduled_str = item.get("scheduled_time", "")
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_str)
            if scheduled_dt <= now:
                due.append(item)
        except (ValueError, TypeError):
            pass

    return due


def cancel_queue_item(queue_id: int, user_id: int = 1) -> bool:
    """
    Cancel a pending queue item.

    Args:
        queue_id: Queue item to cancel.
        user_id: User ID.

    Returns:
        True if the item was cancelled, False if it was not found or already processed.
    """
    pending = database.get_pending_queue(user_id=user_id)
    matching = [item for item in pending if item.get("queue_id") == queue_id]

    if not matching:
        logger.warning("Queue item %d not found in pending queue.", queue_id)
        return False

    database.update_queue_status(queue_id, "cancelled")
    logger.info("Queue item %d cancelled.", queue_id)
    return True


def enforce_daily_limit(
    user_prefs: Dict[str, Any],
    user_id: int = 1,
) -> bool:
    """
    Check whether the daily post limit has been reached.

    Args:
        user_prefs: User preferences dict.
        user_id: User ID.

    Returns:
        True if we are at or above the daily limit (should not post more today).
    """
    post_frequency = user_prefs.get("post_frequency", 2)
    allow_short    = user_prefs.get("allow_short_clips", False)
    max_today      = post_frequency + (1 if allow_short else 0)

    today_count = database.get_today_post_count(user_id=user_id)
    at_limit    = today_count >= max_today

    if at_limit:
        logger.info(
            "Daily post limit reached: %d/%d posts made today.",
            today_count, max_today,
        )
    return at_limit


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing queue.py...")
    print()

    try:
        from preferences import load_preferences
        prefs = load_preferences()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Initialize a fresh test database
    from database import initialize_database, insert_package, enqueue_package
    test_db_path = Path("/tmp/clipcast_queue_test.db")
    if test_db_path.exists():
        test_db_path.unlink()

    # Monkey-patch database module for testing
    import database as db_module
    _orig_get_pending    = db_module.get_pending_queue
    _orig_get_last       = db_module.get_last_post_time
    _orig_get_next       = db_module.get_next_scheduled_post
    _orig_get_today      = db_module.get_today_post_count
    _orig_enqueue        = db_module.enqueue_package

    db_module.get_pending_queue      = lambda **kw: []
    db_module.get_last_post_time     = lambda **kw: None
    db_module.get_next_scheduled_post= lambda **kw: None
    db_module.get_today_post_count   = lambda **kw: 0

    # Test slot calculation
    print("Next available posting slot:")
    slot = get_next_available_slot(prefs)
    print(f"  → {slot.strftime('%Y-%m-%d %H:%M')}")
    print()

    # Test today's slots
    today_slots = get_slots_for_today(prefs)
    print(f"Today's slots ({len(today_slots)} configured):")
    for slot_dt, is_taken in today_slots:
        status = "[taken]" if is_taken else "[available]"
        print(f"  {slot_dt.strftime('%H:%M')}  {status}")
    print()

    # Test daily limit check
    at_limit = enforce_daily_limit(prefs)
    print(f"At daily limit: {at_limit}  (0 posts today, limit={prefs.get('post_frequency')})")

    # Test display (empty queue)
    print("\nDisplay empty queue:")
    display_queue()

    # Restore
    db_module.get_pending_queue       = _orig_get_pending
    db_module.get_last_post_time      = _orig_get_last
    db_module.get_next_scheduled_post = _orig_get_next
    db_module.get_today_post_count    = _orig_get_today
    db_module.enqueue_package         = _orig_enqueue

    print("Queue test complete.")
