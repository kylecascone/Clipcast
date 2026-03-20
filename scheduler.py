"""
scheduler.py
============
Runs the ClipCast automation engine continuously.

Two concurrent loops:
  1. Automated pipeline  — runs every 6 hours to fetch, score, compile,
                           and queue new clips from Twitch and YouTube.
  2. Queue processor     — checks every 60 seconds for posts that are due
                           and uploads them to TikTok.

Manual clips are handled via the folder watcher (fetcher_manual.py) which
runs in a separate thread.

SaaS Note:
    For multi-user SaaS, this scheduler would be replaced with a proper
    task queue system (e.g. Celery + Redis or AWS SQS) with per-user
    schedules stored in the database. The current structure anticipates
    this by accepting user_id throughout.
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import schedule

import database
from preferences import load_preferences, load_config

logger = logging.getLogger(__name__)

# How often to run the full automated pipeline (in hours)
AUTO_PIPELINE_INTERVAL_HOURS = 6

# How often to check the posting queue for due items (in seconds)
QUEUE_CHECK_INTERVAL_SECONDS = 60


# ══════════════════════════════════════════════════════════════════════════════
# Full automated pipeline
# ══════════════════════════════════════════════════════════════════════════════

def run_automated_pipeline(
    user_config: Optional[Dict] = None,
    user_prefs: Optional[Dict] = None,
    test_mode: bool = False,
    user_id: int = 1,
) -> None:
    """
    Run one complete automated pipeline cycle:
      1. Fetch clips from Twitch and YouTube.
      2. Filter out already-processed URLs.
      3. Score all new clips.
      4. Filter by minimum quality score.
      5. Compile into post packages.
      6. Process each package (download, edit, normalize).
      7. Generate captions.
      8. Save packages to the database.
      9. Add to posting queue (or skip if daily limit reached).

    Args:
        user_config: API credentials. If None, loaded from config.yaml.
        user_prefs: User preferences. If None, loaded from preferences.yaml.
        test_mode: If True, process everything but don't upload to TikTok.
        user_id: User ID for database writes.
    """
    from rich.console import Console
    console = Console()

    try:
        if user_config is None:
            user_config = load_config()
        if user_prefs is None:
            user_prefs = load_preferences()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        return

    run_start = datetime.now()
    logger.info(
        "=== Automated pipeline started at %s ===",
        run_start.strftime("%Y-%m-%d %H:%M:%S"),
    )
    console.print(
        f"\n[bold cyan]ClipCast — Automated pipeline starting[/bold cyan]  "
        f"[dim]{run_start.strftime('%H:%M:%S')}[/dim]"
    )

    # ── Step 1: Fetch clips (shared pool or direct) ───────────────────────────
    all_raw_clips: List[Dict[str, Any]] = []
    use_shared_pool = user_prefs.get("use_shared_pool", True)

    if use_shared_pool:
        # ── Shared pool path ─────────────────────────────────────────────────
        # Check if the pool needs refreshing, trigger refresh if stale.
        try:
            from pool_fetcher import check_pool_freshness, refresh_all_pools
            refresh_hours = int(user_prefs.get("pool_refresh_interval_hours", 6))

            if not check_pool_freshness(hours=refresh_hours):
                console.print("  [dim]Shared pool stale — refreshing…[/dim]")
                try:
                    refresh_result = refresh_all_pools(
                        user_config=user_config,
                        user_prefs=user_prefs,
                    )
                    console.print(
                        f"  Pool   : [green]refreshed[/green]  "
                        f"Twitch +{refresh_result.get('twitch_added', 0)}  "
                        f"YouTube +{refresh_result.get('youtube_added', 0)}  "
                        f"Viral +{refresh_result.get('viral_discovery_added', 0)}  "
                        f"({refresh_result.get('total_pool', 0)} total)"
                    )
                except Exception as e:
                    logger.warning("Pool refresh failed (will use stale pool): %s", e)
                    console.print(f"  Pool   : [yellow]refresh failed — using stale pool[/yellow]")
            else:
                console.print("  Pool   : [dim]fresh (no refresh needed)[/dim]")

        except ImportError as e:
            logger.warning("pool_fetcher not available: %s — falling back to direct fetch", e)
            use_shared_pool = False

    if use_shared_pool:
        # Draw clips from the shared pool
        try:
            import shared_pool as _shared_pool
            reservation_hours = int(user_prefs.get("clip_reservation_hours", 48))
            pool_clips = _shared_pool.get_clips_for_user(
                user_prefs=user_prefs,
                user_id=user_id,
                limit=100,
            )

            # Apply 20% score boost for clips from the user's preferred creators
            target_streamers = {s.lower() for s in user_prefs.get("target_streamers", [])}
            target_channels  = set(user_prefs.get("target_youtube_channels", []))
            for clip in pool_clips:
                creator = (clip.get("creator_name") or "").lower()
                if creator in target_streamers or clip.get("channel_id") in target_channels:
                    clip["score"] = min(100.0, float(clip.get("score", 0)) * 1.20)
                    clip["_preferred_creator"] = True

            all_raw_clips = pool_clips
            console.print(
                f"  Pool   : [green]{len(pool_clips)} clip(s) from shared pool[/green]"
            )

            # Track pool clip metadata for reservation after compilation.
            # Maps url → {shared_clip_id, template, caption_style} so we can
            # pass the suggested presentation combo to mark_clip_reserved().
            _pool_clip_map = {
                c.get("url", ""): {
                    "shared_clip_id":    c.get("shared_clip_id"),
                    "template":          c.get("suggested_template"),
                    "caption_style":     c.get("suggested_caption_style"),
                }
                for c in pool_clips
                if c.get("shared_clip_id")
            }

            # Apply suggested_template to each clip so compile/editor can use it
            for clip in pool_clips:
                if clip.get("suggested_template") and "template" not in clip:
                    clip["template"] = clip["suggested_template"]
                if clip.get("suggested_caption_style") and "caption_style" not in clip:
                    clip["caption_style"] = clip["suggested_caption_style"]

        except Exception as e:
            logger.error("Shared pool draw failed: %s", e)
            console.print(f"  Pool   : [red]draw failed — {e}[/red]")
            database.log_error(message=f"Shared pool draw failed: {e}", step="fetch", user_id=user_id)
            use_shared_pool = False  # fall through to direct fetch

    if not use_shared_pool:
        # ── Direct fetch path (fallback or preference) ──────────────────────
        _pool_clip_map: Dict[str, Any] = {}

        twitch_enabled = user_prefs.get("twitch_enabled", True)
        if twitch_enabled:
            try:
                from fetcher_twitch import fetch_clips as fetch_twitch
                twitch_clips = fetch_twitch(user_config=user_config, user_prefs=user_prefs)
                console.print(f"  Twitch : [green]{len(twitch_clips)} clip(s) fetched[/green]")
                all_raw_clips.extend(twitch_clips)
            except Exception as e:
                logger.error("Twitch fetch failed: %s", e)
                console.print(f"  Twitch : [red]fetch failed — {e}[/red]")
                database.log_error(message=f"Twitch fetch failed: {e}", step="fetch", user_id=user_id)

        youtube_enabled = user_prefs.get("youtube_enabled", True)
        if youtube_enabled:
            try:
                from fetcher_youtube import fetch_clips as fetch_youtube
                youtube_clips = fetch_youtube(user_config=user_config, user_prefs=user_prefs)
                console.print(f"  YouTube: [green]{len(youtube_clips)} clip(s) fetched[/green]")
                all_raw_clips.extend(youtube_clips)
            except Exception as e:
                logger.error("YouTube fetch failed: %s", e)
                console.print(f"  YouTube: [red]fetch failed — {e}[/red]")
                database.log_error(message=f"YouTube fetch failed: {e}", step="fetch", user_id=user_id)

    if not all_raw_clips:
        logger.info("No clips fetched this cycle.")
        console.print("  [yellow]No clips fetched. Ending this cycle.[/yellow]")
        return

    # ── Step 2: Filter already-processed URLs ──────────────────────────────────
    new_clips = [
        c for c in all_raw_clips
        if not database.clip_url_exists(c.get("url", ""))
    ]
    skipped_count = len(all_raw_clips) - len(new_clips)
    logger.info(
        "%d total clips fetched. %d already processed. %d new.",
        len(all_raw_clips), skipped_count, len(new_clips),
    )
    console.print(
        f"  Filter : [green]{len(new_clips)} new[/green]  "
        f"[dim]{skipped_count} already processed[/dim]"
    )

    if not new_clips:
        console.print("  [yellow]All clips already processed. Nothing to do.[/yellow]")
        return

    # ── Step 3: Save raw clips to database with 'queued' status ───────────────
    for clip in new_clips:
        try:
            clip_id = database.insert_clip({
                "user_id":       user_id,
                "source":        clip["source"],
                "title":         clip["title"],
                "creator_name":  clip.get("creator_name"),
                "url":           clip.get("url"),
                "duration":      clip.get("duration"),
                "mode":          "auto",
                "status":        "queued",
            })
        except Exception as e:
            logger.warning("Skipping duplicate clip '%s': %s", clip.get("title", "")[:50], e)
            continue
        clip["clip_id"] = clip_id  # Attach DB ID for downstream use

    # ── Step 4: Score clips ────────────────────────────────────────────────────
    from scorer import score_clips, filter_by_min_score
    scored_clips = score_clips(new_clips, user_prefs=user_prefs, user_id=user_id)

    min_score = user_prefs.get("minimum_clip_quality_score", 40)
    passing, failing = filter_by_min_score(scored_clips, min_score)

    # Mark skipped clips in database
    for clip in failing:
        if clip.get("clip_id"):
            database.update_clip_status(clip["clip_id"], "skipped")

    console.print(
        f"  Scored : [green]{len(passing)} passed[/green]  "
        f"[dim]{len(failing)} below threshold (score < {min_score})[/dim]"
    )

    if not passing:
        console.print("  [yellow]No clips passed quality threshold.[/yellow]")
        return

    # Update scores in database
    for clip in passing:
        if clip.get("clip_id"):
            database.update_clip_field(clip["clip_id"], "score", clip["score"])
            database.update_clip_status(clip["clip_id"], "scored")

    # ── Step 5: Compile packages ───────────────────────────────────────────────
    from compiler import compile_packages
    packages = compile_packages(passing, user_prefs=user_prefs, user_id=user_id)

    console.print(f"  Compile: [green]{len(packages)} package(s)[/green]")

    # ── Reserve pool clips after compilation ──────────────────────────────────
    # Two-tier: viral clips record the template+caption_style combo so the
    # system can track which presentations have been used globally.
    if use_shared_pool:
        try:
            import shared_pool as _shared_pool
            reservation_hours = int(user_prefs.get("clip_reservation_hours", 48))
            reserved_count = 0
            for pkg in packages:
                for clip in pkg.get("clips", []):
                    url = clip.get("url", "")
                    pool_meta = _pool_clip_map.get(url, {})
                    shared_id = pool_meta.get("shared_clip_id") if isinstance(pool_meta, dict) else pool_meta
                    if shared_id:
                        _shared_pool.mark_clip_reserved(
                            shared_clip_id=shared_id,
                            user_id=user_id,
                            hours=reservation_hours,
                            template=pool_meta.get("template") if isinstance(pool_meta, dict) else None,
                            caption_style=pool_meta.get("caption_style") if isinstance(pool_meta, dict) else None,
                        )
                        reserved_count += 1
            if reserved_count:
                logger.debug("Reserved %d pool clip(s) for user_id=%d.", reserved_count, user_id)
        except Exception as e:
            logger.warning("Pool clip reservation failed (non-fatal): %s", e)

    # ── Test mode: cap at 3 packages (top 3 by score) ────────────────────────
    if test_mode and len(packages) > 3:
        packages = packages[:3]
        console.print(
            "  [yellow]Test mode: processing 3 packages only[/yellow]"
        )

    # ── Step 6–8: Edit, caption, and queue each package ────────────────────────
    from posting_queue import enforce_daily_limit, add_package_to_queue
    from captions import generate_caption
    from editor import process_package

    # Check whether the user has configured any posting slots.
    # If schedule.yaml is empty or missing, process and store clips in the DB
    # but DO NOT add them to the posting queue — the user hasn't said when to post.
    _sched = load_schedule()
    _DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    _schedule_has_slots = any(_sched.get(day) for day in _DAYS)
    if not _schedule_has_slots and not test_mode:
        logger.info(
            "schedule.yaml has no slots — packages will be processed and stored "
            "but NOT queued. Configure a schedule in the webapp to enable posting."
        )
        console.print(
            "  [dim]No schedule slots configured — clips will be processed "
            "and stored but not queued for posting.[/dim]\n"
            "  [dim]Set a posting schedule in the webapp to enable auto-posting.[/dim]"
        )

    for pkg in packages:
        if enforce_daily_limit(user_prefs, user_id=user_id):
            logger.info("Daily post limit reached. Stopping queue additions for today.")
            console.print("  [yellow]Daily post limit reached. Remaining packages saved for tomorrow.[/yellow]")
            break

        # ── Generate caption ───────────────────────────────────────────────────
        lead_clip = pkg["clips"][0] if pkg["clips"] else {}
        caption_text = generate_caption(
            clip=lead_clip,
            style_id=pkg["caption_style"],
            user_prefs=user_prefs,
            user_id=user_id,
        )
        pkg["caption_text"] = caption_text

        # ── Save package to DB ─────────────────────────────────────────────────
        package_id = database.insert_package({
            "user_id":       user_id,
            "clip_ids":      pkg["clip_ids"],
            "template":      pkg["template"],
            "caption_style": pkg["caption_style"],
            "caption_text":  caption_text,
            "mode":          "auto",
            "status":        "pending",
        })
        pkg["package_id"] = package_id

        # Update clip statuses to 'compiled'
        for cid in pkg["clip_ids"]:
            database.update_clip_status(cid, "compiled")

        # ── Edit video ────────────────────────────────────────────────────────
        console.print(
            f"  Editing package {package_id} "
            f"({len(pkg['clips'])} clip(s), template={pkg['template']})..."
        )
        output_path = process_package(pkg, test_mode=test_mode)

        if not output_path:
            logger.error("Editing failed for package %d.", package_id)
            database.update_package_field(package_id, "status", "failed")
            database.log_error(
                message=f"Editing failed for package {package_id}",
                step="edit",
                package_id=package_id,
                user_id=user_id,
            )
            continue

        # Update package with compiled path
        database.update_package_field(package_id, "compiled_path", output_path)
        database.update_package_field(package_id, "status", "processed")
        pkg["compiled_path"] = output_path

        # Update clip statuses to 'processed'
        for cid in pkg["clip_ids"]:
            database.update_clip_status(cid, "processed")

        # ── Preview routing — hold for manual approval if required ─────────────
        if user_prefs.get("clip_preview_required", False) and not test_mode:
            database.update_package_field(package_id, "preview_pending", 1)
            console.print(
                f"  [yellow]Preview pending[/yellow] package {package_id} "
                f"— run [bold]python main.py --preview[/bold] to approve"
            )
            continue

        # ── Add to posting queue ───────────────────────────────────────────────
        if not test_mode and _schedule_has_slots:
            queue_id, slot = add_package_to_queue(
                package_id=package_id,
                user_prefs=user_prefs,
                mode="auto",
                user_id=user_id,
            )
            console.print(
                f"  [green]Queued[/green] package {package_id} "
                f"→ post at [bold]{slot.strftime('%Y-%m-%d %H:%M')}[/bold]"
            )
        elif not test_mode:
            # No schedule slots — clip is processed and ready but held back.
            console.print(
                f"  [dim]Package {package_id} processed and stored "
                f"(not queued — no schedule slots configured)[/dim]"
            )
        else:
            _test_title = (
                (pkg["clips"][0].get("viral_title") or pkg["clips"][0].get("title", ""))
                if pkg.get("clips") else pkg.get("caption_text", "")
            )
            _test_platforms = user_prefs.get("target_platforms", ["tiktok"])
            console.print(
                f"  [yellow][TEST MODE][/yellow] Package {package_id} processed. "
                f"Output: {output_path}"
            )
            console.print(
                f"  [dim]Would upload to:[/dim] "
                + ", ".join(
                    f"[bold]{p}[/bold]"
                    + (" → " + f"https://youtube.com/shorts/<id>" if p == "youtube_shorts" else "")
                    for p in _test_platforms
                )
            )
            if _test_title:
                console.print(f"  [dim]Title:[/dim] {_test_title[:80]}")

    run_end = datetime.now()
    duration = (run_end - run_start).total_seconds()
    logger.info("=== Automated pipeline complete in %.1fs ===", duration)
    console.print(
        f"\n[bold green]Pipeline complete[/bold green]  "
        f"[dim]{duration:.1f}s[/dim]\n"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Manual clip processing
# ══════════════════════════════════════════════════════════════════════════════

def process_manual_clip(
    clip_data: Dict[str, Any],
    user_config: Optional[Dict] = None,
    user_prefs: Optional[Dict] = None,
    test_mode: bool = False,
    user_id: int = 1,
) -> None:
    """
    Process a single manual clip through the full pipeline.

    Called when:
      - A file is dropped into clips/manual/ (via folder watcher).
      - The user runs `python main.py --manual <file_or_url>`.

    Args:
        clip_data: Clip dict from fetcher_manual.
        user_config: API credentials.
        user_prefs: User preferences.
        test_mode: If True, skip TikTok upload.
        user_id: User ID.
    """
    from rich.console import Console
    from captions import generate_caption, confirm_or_edit_caption
    from compiler import compile_packages
    from editor import process_package
    from posting_queue import add_package_to_queue, add_package_immediately

    console = Console()

    if user_config is None:
        user_config = load_config()
    if user_prefs is None:
        user_prefs = load_preferences()

    console.print(
        f"\n[bold magenta]Manual clip processing:[/bold magenta] "
        f"{clip_data.get('title', 'Untitled')[:60]}"
    )

    # ── Save to database ───────────────────────────────────────────────────────
    clip_id = database.insert_clip({**clip_data, "user_id": user_id})
    clip_data["clip_id"] = clip_id

    # ── Compile as solo package ────────────────────────────────────────────────
    packages = compile_packages([clip_data], user_prefs=user_prefs, user_id=user_id)
    if not packages:
        logger.error("compile_packages returned empty for manual clip.")
        return

    pkg = packages[0]

    # ── Generate and optionally confirm caption ────────────────────────────────
    caption = generate_caption(
        clip=clip_data,
        style_id=pkg["caption_style"],
        user_prefs=user_prefs,
        user_id=user_id,
    )

    posting_behavior = user_prefs.get("manual_mode_posting_behavior", "scheduled")
    if posting_behavior == "immediate":
        caption = confirm_or_edit_caption(caption, clip_data)

    pkg["caption_text"] = caption

    # ── Save package ───────────────────────────────────────────────────────────
    package_id = database.insert_package({
        "user_id":       user_id,
        "clip_ids":      pkg["clip_ids"],
        "template":      pkg["template"],
        "caption_style": pkg["caption_style"],
        "caption_text":  caption,
        "mode":          "manual",
        "status":        "pending",
    })
    pkg["package_id"] = package_id

    # ── Edit video ────────────────────────────────────────────────────────────
    console.print("  Editing...")
    output_path = process_package(pkg, test_mode=test_mode)

    if not output_path:
        logger.error("Editing failed for manual clip.")
        database.update_package_field(package_id, "status", "failed")
        database.log_error(
            message=f"Editing failed for manual clip (package {package_id})",
            step="edit",
            package_id=package_id,
            user_id=user_id,
        )
        return

    database.update_package_field(package_id, "compiled_path", output_path)
    database.update_package_field(package_id, "status", "processed")
    pkg["compiled_path"] = output_path
    database.update_clip_status(clip_id, "processed")

    # ── Queue or post ──────────────────────────────────────────────────────────
    if test_mode:
        console.print(
            f"  [yellow][TEST MODE][/yellow] Manual clip processed → {output_path}"
        )
        return

    if posting_behavior == "immediate":
        queue_id, slot = add_package_immediately(package_id, user_id=user_id)
        console.print(
            f"  [green]Immediate post queued[/green] at {slot.strftime('%H:%M:%S')}"
        )
    else:
        queue_id, slot = add_package_to_queue(
            package_id=package_id,
            user_prefs=user_prefs,
            mode="manual",
            user_id=user_id,
        )
        console.print(
            f"  [green]Scheduled[/green] for {slot.strftime('%Y-%m-%d %H:%M')}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Queue processor
# ══════════════════════════════════════════════════════════════════════════════

def process_due_queue_items(
    user_config: Optional[Dict] = None,
    user_prefs: Optional[Dict] = None,
    test_mode: bool = False,
    user_id: int = 1,
) -> None:
    """
    Check the posting queue for due items and upload them to all configured platforms.

    Called on a 60-second interval by the scheduler main loop.
    """
    from posting_queue import get_due_items
    from uploader import upload_package

    if user_config is None:
        user_config = load_config()
    if user_prefs is None:
        try:
            user_prefs = load_preferences()
        except (FileNotFoundError, ValueError):
            user_prefs = {}

    due_items = get_due_items(user_id=user_id)
    if not due_items:
        return

    logger.info("%d queue item(s) due for posting.", len(due_items))

    for item in due_items:
        queue_id   = item["queue_id"]
        package_id = item["package_id"]

        # Mark as 'processing' to prevent duplicate attempts
        database.update_queue_status(queue_id, "processing")

        pkg = database.get_package(package_id)
        if not pkg:
            logger.error("Package %d not found in database.", package_id)
            database.update_queue_status(queue_id, "failed")
            database.log_error(
                message=f"Package {package_id} not found in database",
                step="upload",
                user_id=user_id,
            )
            continue

        logger.info(
            "Uploading package %d (queue_id=%d)...",
            package_id, queue_id,
        )

        # upload_package returns Dict[platform, post_id_or_None]
        post_ids = upload_package(
            pkg,
            user_config=user_config,
            user_prefs=user_prefs,
            test_mode=test_mode,
        )

        any_success = bool(post_ids and any(post_ids.values()))

        if any_success:
            database.update_queue_status(
                queue_id, "posted", posted_at=datetime.now()
            )
            database.update_package_field(package_id, "status", "posted")

            # Store per-platform post IDs
            if post_ids.get("tiktok"):
                database.update_package_field(
                    package_id, "tiktok_post_id", post_ids["tiktok"]
                )
            if post_ids.get("youtube_shorts"):
                database.update_package_field(
                    package_id, "yt_shorts_post_id", post_ids["youtube_shorts"]
                )
            if post_ids.get("instagram_reels"):
                database.update_package_field(
                    package_id, "instagram_post_id", post_ids["instagram_reels"]
                )

            # Update all clips in this package to 'posted'
            for clip_id in pkg.get("clip_ids", []):
                database.update_clip_status(clip_id, "posted")

            logger.info(
                "Package %d posted. IDs: %s",
                package_id,
                {k: v for k, v in post_ids.items() if v},
            )
        else:
            database.update_queue_status(queue_id, "failed")
            database.update_package_field(package_id, "status", "failed")
            logger.error("Upload failed for package %d.", package_id)
            database.log_error(
                message=f"All platform uploads failed for package {package_id}",
                step="upload",
                package_id=package_id,
                user_id=user_id,
            )


# ══════════════════════════════════════════════════════════════════════════════
# YAML schedule helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_schedule() -> Dict[str, Any]:
    """Load weekly schedule from schedule.yaml, return {} if missing."""
    schedule_path = Path(__file__).parent / "schedule.yaml"
    if schedule_path.exists():
        try:
            import yaml
            with open(schedule_path) as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("load_schedule: could not read schedule.yaml: %s", exc)
    return {}


def get_slots_for_now(sched: Dict[str, Any]) -> list:
    """Return schedule slots whose time matches the current HH:MM."""
    from datetime import datetime
    now = datetime.now()
    day_name = now.strftime("%A")          # Monday, Tuesday, …
    current_time = now.strftime("%H:%M")
    return [s for s in sched.get(day_name, []) if s.get("time") == current_time]


def filter_pool_by_slot(slot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Return up to 10 pool clips matching a schedule slot's creator / content_type /
    min_score / platform criteria, ordered by score DESC.
    """
    creator      = slot.get("creator", "") or ""
    content_type = slot.get("content_type", "Any") or "Any"
    min_score    = float(slot.get("min_score", 0) or 0)

    query = """
        SELECT * FROM shared_clips
        WHERE is_blocked = 0
          AND (expires_at IS NULL OR expires_at > datetime('now'))
          AND score >= ?
    """
    params: List[Any] = [min_score]

    if creator and creator not in ("", "Any Creator"):
        query += " AND creator_name = ?"
        params.append(creator)

    if content_type and content_type != "Any":
        query += " AND (category LIKE ? OR theme LIKE ?)"
        params.extend([f"%{content_type}%", f"%{content_type}%"])

    query += " ORDER BY score DESC LIMIT 10"

    try:
        conn = database.get_connection()
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("filter_pool_by_slot: DB error: %s", exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Main schedule loop
# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler(
    user_config: Optional[Dict] = None,
    user_prefs: Optional[Dict] = None,
    test_mode: bool = False,
    user_id: int = 1,
) -> None:
    """
    Start the full automated scheduler.

    - Runs the automated pipeline immediately on start, then every 6 hours.
    - Checks the queue every 60 seconds.
    - Watches clips/manual/ for new files.
    - Runs until KeyboardInterrupt (Ctrl+C).

    Args:
        user_config: API credentials.
        user_prefs: User preferences.
        test_mode: If True, process everything but don't post.
        user_id: User ID.
    """
    from rich.console import Console
    from fetcher_manual import start_folder_watcher, stop_folder_watcher

    console = Console()

    if user_config is None:
        user_config = load_config()
    if user_prefs is None:
        user_prefs = load_preferences()

    database.initialize_database()

    console.print(
        "\n[bold cyan]ClipCast Studio — Scheduler Starting[/bold cyan]\n"
        f"  Auto pipeline every {AUTO_PIPELINE_INTERVAL_HOURS} hours\n"
        f"  Queue check every {QUEUE_CHECK_INTERVAL_SECONDS} seconds\n"
        f"  Watching: clips/manual/\n"
        f"  Test mode: {'ON — no real posts' if test_mode else 'OFF — live posting'}\n"
        "  Press Ctrl+C to stop.\n"
    )

    # ── Schedule automated pipeline ────────────────────────────────────────────
    def _run_pipeline():
        run_automated_pipeline(
            user_config=user_config,
            user_prefs=user_prefs,
            test_mode=test_mode,
            user_id=user_id,
        )

    def _check_queue():
        process_due_queue_items(
            user_config=user_config,
            user_prefs=user_prefs,
            test_mode=test_mode,
            user_id=user_id,
        )

    def _check_yaml_schedule():
        """Fire pipeline runs for any YAML schedule slots due this minute."""
        sched = load_schedule()
        if not sched:
            return
        due_slots = get_slots_for_now(sched)
        for slot in due_slots:
            clips = filter_pool_by_slot(slot)
            if not clips:
                logger.info("YAML schedule slot at %s: no matching clips found.", slot.get("time"))
                continue
            logger.info(
                "YAML schedule slot at %s: triggering pipeline for %d clip(s) "
                "(creator=%r, type=%r, platform=%r)",
                slot.get("time"), len(clips),
                slot.get("creator", "any"),
                slot.get("content_type", "Any"),
                slot.get("platform", "both"),
            )
            # Inject slot platform preference into prefs for this run
            slot_prefs = dict(user_prefs)
            plat = slot.get("platform", "both")
            if plat == "tiktok":
                slot_prefs["post_to_tiktok"] = True
                slot_prefs["post_to_youtube"] = False
            elif plat == "youtube":
                slot_prefs["post_to_tiktok"] = False
                slot_prefs["post_to_youtube"] = True
            threading.Thread(
                target=run_automated_pipeline,
                kwargs=dict(
                    user_config=user_config,
                    user_prefs=slot_prefs,
                    test_mode=test_mode,
                    user_id=user_id,
                ),
                daemon=True,
            ).start()

    # Run immediately on start, then every 6 hours
    schedule.every(AUTO_PIPELINE_INTERVAL_HOURS).hours.do(_run_pipeline)

    # Check queue every minute
    schedule.every(QUEUE_CHECK_INTERVAL_SECONDS).seconds.do(_check_queue)

    # Check YAML schedule every minute
    schedule.every(60).seconds.do(_check_yaml_schedule)

    # ── Start manual folder watcher in background thread ──────────────────────
    def _on_manual_clip(clip_data: Dict[str, Any]) -> None:
        process_manual_clip(
            clip_data=clip_data,
            user_config=user_config,
            user_prefs=user_prefs,
            test_mode=test_mode,
            user_id=user_id,
        )

    observer = start_folder_watcher(
        callback=_on_manual_clip,
        user_prefs=user_prefs,
        user_id=user_id,
    )

    # Run the first pipeline cycle immediately (in a thread so it doesn't block)
    first_run = threading.Thread(target=_run_pipeline, daemon=True)
    first_run.start()

    # ── Main loop ──────────────────────────────────────────────────────────────
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped by user.[/yellow]")
    finally:
        stop_folder_watcher(observer)
        logger.info("Scheduler stopped.")


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing scheduler.py — dry run...")
    print("This will run one automated pipeline cycle in TEST MODE.")
    print("No videos will be posted. Processed output goes to clips/processed/.")
    print()

    try:
        config = load_config()
        prefs  = load_preferences()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    database.initialize_database()
    run_automated_pipeline(
        user_config=config,
        user_prefs=prefs,
        test_mode=True,
    )
