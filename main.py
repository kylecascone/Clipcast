"""
main.py
=======
ClipCast Studio — CLI entry point.

Usage:
  python main.py --setup              Run the preferences setup wizard
  python main.py --test               Run pipeline, save videos locally, no posting
  python main.py --run                Run pipeline once and post to configured platforms
  python main.py --schedule           Start full automated scheduler + folder watcher
  python main.py --manual <path|url>  Process a specific file or URL immediately
  python main.py --queue              Show the current posting queue
  python main.py --status             Show system status (last post, next post, count)
  python main.py --analytics          Fetch and display post performance analytics
  python main.py --preview            Approve or reject videos pending manual review
  python main.py --errors             Show recent pipeline errors
  python main.py --blocked            View and manage the creator opt-out list
  python main.py --audit              Export 30-day audit log to CSV in legal/
  python main.py --pool               Show shared content pool statistics
  python main.py --refresh            Manually trigger a shared pool refresh
  python main.py --edit [path|url]    Launch interactive custom clip editor
  python main.py --learn              Analyse post performance and auto-tune settings
"""

# ── Pillow ≥10 compatibility patch — must run before moviepy is imported ───────
# PIL removed Image.ANTIALIAS in Pillow 10.0.0; moviepy still references it.
# Patching here (first import in the process) guarantees the attribute exists
# before any downstream module (moviepy, editor, etc.) can trigger the error.
try:
    import PIL.Image
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS  # type: ignore[attr-defined]
except (ImportError, AttributeError):
    pass
# ───────────────────────────────────────────────────────────────────────────────

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# Logging setup
# ══════════════════════════════════════════════════════════════════════════════

def configure_logging(verbose: bool = False) -> None:
    """Configure logging for the application."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("clipcast.log", encoding="utf-8"),
        ],
    )
    # Silence noisy third-party loggers at INFO level
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("schedule").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("moviepy").setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════════
# CLI commands
# ══════════════════════════════════════════════════════════════════════════════

def cmd_setup() -> None:
    """Run the interactive preferences setup wizard."""
    from consent import check_consent
    from preferences import setup_wizard
    check_consent()
    setup_wizard()


def _require_consent() -> None:
    """Check consent silently before any live command. Exits gracefully if declined."""
    try:
        from consent import check_consent
        check_consent()
    except SystemExit:
        raise
    except Exception:
        pass  # Never let consent check crash a command


def cmd_test() -> None:
    """Run the full pipeline in test mode — process clips but don't post."""
    _require_consent()
    console.print(
        Panel(
            "[bold yellow]Test Mode[/bold yellow]\n\n"
            "Running the full pipeline: fetch → score → compile → edit.\n"
            "Processed videos will be saved to [bold]clips/processed/[/bold].\n"
            "[yellow]No posts will be uploaded to TikTok.[/yellow]",
            border_style="yellow",
        )
    )
    _run_once(test_mode=True)


def cmd_run() -> None:
    """Run the pipeline once and post results to configured platforms."""
    _require_consent()
    console.print(
        Panel(
            "[bold green]Live Run[/bold green]\n\n"
            "Running the full pipeline and posting to TikTok.\n"
            "Make sure your config.yaml has valid API credentials.",
            border_style="green",
        )
    )
    _run_once(test_mode=False)


def cmd_schedule() -> None:
    """Start the full automated scheduler."""
    _require_consent()
    from scheduler import start_scheduler
    from preferences import load_config, load_preferences
    from database import initialize_database

    try:
        config = load_config()
        prefs  = load_preferences()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)

    initialize_database()
    start_scheduler(user_config=config, user_prefs=prefs, test_mode=False)


def cmd_manual(source: str) -> None:
    """Process a specific file path or URL using manual mode settings."""
    _require_consent()
    from preferences import load_config, load_preferences
    from database import initialize_database
    from scheduler import process_manual_clip

    try:
        config = load_config()
        prefs  = load_preferences()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)

    initialize_database()

    source = source.strip()

    # Determine if it's a URL or a file path
    if source.startswith("http://") or source.startswith("https://"):
        from fetcher_manual import build_clip_from_url, classify_url
        clip_type = classify_url(source)
        if not clip_type:
            console.print(
                "[red]Unrecognized URL.[/red] Supported formats:\n"
                "  • Twitch clip:  https://clips.twitch.tv/...\n"
                "  • YouTube:      https://www.youtube.com/watch?v=...\n"
                "  • YouTube short: https://youtu.be/..."
            )
            sys.exit(1)

        try:
            clip_data = build_clip_from_url(source, user_prefs=prefs)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    else:
        # Treat as a file path
        file_path = Path(source)
        if not file_path.exists():
            console.print(f"[red]File not found:[/red] {source}")
            sys.exit(1)

        from fetcher_manual import build_clip_from_file
        try:
            clip_data = build_clip_from_file(file_path, user_prefs=prefs)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    # Check posting behavior and notify user
    behavior = prefs.get("manual_mode_posting_behavior", "scheduled")
    console.print(
        f"\n[cyan]Manual mode:[/cyan] {clip_data.get('source', 'unknown')} clip  "
        f"| Behavior: [bold]{behavior}[/bold]"
    )

    process_manual_clip(
        clip_data=clip_data,
        user_config=config,
        user_prefs=prefs,
        test_mode=False,
    )


def cmd_queue() -> None:
    """Display the current posting queue."""
    from database import initialize_database
    from posting_queue import display_queue

    initialize_database()
    display_queue()


def cmd_status() -> None:
    """Display system status: last post, next post, today's count."""
    from database import initialize_database
    from posting_queue import display_status, display_queue

    initialize_database()
    display_status()
    display_queue()


def cmd_analytics() -> None:
    """Fetch TikTok performance data for posted videos and display analytics table."""
    from database import initialize_database
    from analytics import fetch_and_store_analytics, display_analytics_table

    initialize_database()

    try:
        from preferences import load_config
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)

    console.print("[cyan]Fetching analytics from TikTok...[/cyan]")
    fetch_and_store_analytics(user_config=config)
    display_analytics_table()


def cmd_preview() -> None:
    """Review and approve or reject videos pending manual approval."""
    from database import (
        initialize_database,
        get_pending_approval_packages,
        update_package_field,
    )
    from posting_queue import add_package_to_queue

    initialize_database()

    try:
        from preferences import load_preferences
        prefs = load_preferences()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)

    from rich.prompt import Prompt

    pending = get_pending_approval_packages()
    if not pending:
        console.print("[yellow]No packages pending approval.[/yellow]")
        return

    console.print(f"\n[bold]{len(pending)} package(s) pending approval:[/bold]\n")

    for pkg in pending:
        package_id = pkg["package_id"]
        caption    = (pkg.get("caption_text") or "")[:70]
        path       = pkg.get("compiled_path", "N/A")

        console.print(f"[bold cyan]Package {package_id}[/bold cyan]")
        console.print(f"  Caption : {caption}")
        console.print(f"  File    : {path}")

        action = Prompt.ask(
            "  Action",
            choices=["approve", "skip", "reject"],
            default="approve",
        )

        if action == "approve":
            update_package_field(package_id, "preview_pending", 0)
            queue_id, slot = add_package_to_queue(
                package_id=package_id,
                user_prefs=prefs,
                mode="auto",
            )
            console.print(
                f"  [green]Approved[/green] — scheduled for "
                f"[bold]{slot.strftime('%Y-%m-%d %H:%M')}[/bold]\n"
            )
        elif action == "reject":
            update_package_field(package_id, "preview_pending", 0)
            update_package_field(package_id, "status", "failed")
            console.print(
                f"  [red]Rejected[/red] — package {package_id} discarded.\n"
            )
        else:
            console.print(f"  [dim]Skipped.[/dim]\n")


def cmd_blocked() -> None:
    """Display and manage the creator opt-out list."""
    from blocked_creators import load_blocked, add_blocked, remove_blocked
    from rich.table import Table
    from rich.prompt import Prompt, Confirm

    blocked = load_blocked()
    twitch_list  = blocked.get("twitch", [])
    youtube_list = blocked.get("youtube", [])

    console.print()
    if not twitch_list and not youtube_list:
        console.print("[dim]No creators on the opt-out list yet.[/dim]")
    else:
        table = Table(
            title="Creators Who Have Requested Opt-Out",
            show_lines=True,
        )
        table.add_column("Platform", style="cyan", width=12)
        table.add_column("Creator",  width=40)
        for name in twitch_list:
            table.add_row("Twitch", name)
        for name in youtube_list:
            table.add_row("YouTube", name)
        console.print(table)

    console.print()
    action = Prompt.ask(
        "Action",
        choices=["add", "remove", "done"],
        default="done",
    )

    if action == "add":
        platform = Prompt.ask("Platform", choices=["twitch", "youtube"])
        name = Prompt.ask(f"Creator name to add to opt-out list").strip()
        if name:
            if add_blocked(name, platform):
                console.print(f"[green]✓ {name} ({platform}) added to opt-out list.[/green]")
            else:
                console.print(f"[yellow]{name} ({platform}) is already on the list.[/yellow]")

    elif action == "remove":
        platform = Prompt.ask("Platform", choices=["twitch", "youtube"])
        name = Prompt.ask(f"Creator name to remove from opt-out list").strip()
        if name:
            if remove_blocked(name, platform):
                console.print(f"[green]✓ {name} ({platform}) removed from opt-out list.[/green]")
            else:
                console.print(f"[yellow]{name} ({platform}) was not on the list.[/yellow]")


def cmd_audit_export() -> None:
    """Export the last 30 days of audit logs to a CSV file in legal/."""
    from database import initialize_database
    import audit

    initialize_database()

    console.print("[cyan]Exporting audit log...[/cyan]")
    out_path = audit.export_to_csv(days=30)

    if out_path:
        console.print(f"[green]✓ Audit log exported to:[/green] {out_path}")
        from database import get_audit_logs
        count = len(get_audit_logs(days=30))
        console.print(f"  {count} entries covering the last 30 days.")
    else:
        console.print("[yellow]No audit log entries found for the last 30 days.[/yellow]")


def cmd_pool() -> None:
    """Display shared pool quality report with full breakdown."""
    from database import initialize_database
    import shared_pool
    from rich.table import Table
    from rich.rule import Rule

    initialize_database()
    shared_pool.initialize_shared_pool_tables()

    stats = shared_pool.get_pool_stats()
    total = stats.get("total_clips", 0)
    runs  = stats.get("last_runs", [])

    console.print(f"\n[bold cyan]ClipCast Pool — Quality Report[/bold cyan]")
    console.print(Rule())

    _source_labels = {
        "reddit_trending":          "Reddit trending",
        "youtube_shorts_trending":  "YouTube Shorts",
        "youtube_gaming_trending":  "YouTube Gaming",
        "streamable_trending":      "Streamable",
        "twitter_trending":         "Twitter / X",
        "twitch_api":               "Twitch API",
        "direct_api":               "Twitch/YouTube/Kick API",
    }

    try:
        import database as _database
        conn = _database.get_connection()

        # ── Quality tier breakdown ─────────────────────────────────────────────
        ai_row   = conn.execute(
            "SELECT COUNT(*) FROM shared_clips "
            "WHERE ai_analyzed=1 AND expires_at > datetime('now')"
        ).fetchone()
        auto_row = conn.execute(
            "SELECT COUNT(*) FROM shared_clips "
            "WHERE ai_analyzed=0 AND ai_quality_score=75.0 "
            "AND final_score > 0 AND expires_at > datetime('now')"
        ).fetchone()
        total_analyzed   = int(ai_row[0]) if ai_row else 0
        total_auto       = int(auto_row[0]) if auto_row else 0
        total_unfiltered = total - total_analyzed - total_auto

        console.print(
            f"  [bold]Total in pool:  [/bold][bold white]{total}[/bold white] clips\n"
            f"  Auto-approved (proven viral): [green]{total_auto}[/green]\n"
            f"  AI-scored (quality checked) : [green]{total_analyzed}[/green]\n"
            f"  Unfiltered (API direct)     : [dim]{total_unfiltered}[/dim]"
        )

        # ── By discovery source with ASCII bars ───────────────────────────────
        console.print(f"\n[bold]Discovery source breakdown:[/bold]")
        source_rows = conn.execute("""
            SELECT
                COALESCE(discovery_source, 'direct_api') AS dsrc,
                COUNT(*) AS cnt,
                ROUND(AVG(COALESCE(final_score, score, 0)), 1) AS avg_fs
            FROM shared_clips
            WHERE expires_at > datetime('now')
            GROUP BY dsrc
            ORDER BY cnt DESC
        """).fetchall()

        max_cnt = max((r["cnt"] for r in source_rows), default=1)
        for row in source_rows:
            label   = _source_labels.get(row["dsrc"], row["dsrc"])
            cnt     = row["cnt"]
            bar_len = int(cnt / max_cnt * 20)
            bar     = "█" * bar_len + "░" * (20 - bar_len)
            console.print(
                f"  [cyan]{label:<26}[/cyan] "
                f"[white]{bar}[/white] "
                f"[bold]{cnt:>4}[/bold] clips  avg=[yellow]{row['avg_fs']:.1f}[/yellow]"
            )

        # ── Theme distribution ─────────────────────────────────────────────────
        theme_rows = conn.execute("""
            SELECT COALESCE(theme, 'UNKNOWN') AS theme, COUNT(*) AS cnt
            FROM shared_clips
            WHERE expires_at > datetime('now') AND theme IS NOT NULL
            GROUP BY theme
            ORDER BY cnt DESC
            LIMIT 8
        """).fetchall()

        if theme_rows:
            console.print(f"\n[bold]Theme distribution:[/bold]")
            max_t = max(r["cnt"] for r in theme_rows)
            for row in theme_rows:
                bar_len = int(row["cnt"] / max_t * 15)
                bar     = "▪" * bar_len
                console.print(
                    f"  [dim]{row['theme']:<16}[/dim] "
                    f"[cyan]{bar:<15}[/cyan] {row['cnt']}"
                )

        # ── Days-of-content estimate ───────────────────────────────────────────
        try:
            from preferences import load_preferences
            prefs         = load_preferences()
            post_freq     = int(prefs.get("post_frequency", 1))
            max_cpkg      = int(prefs.get("max_clips_per_compilation", 1))
            clips_per_day = post_freq * max_cpkg
            days_est      = round(total / clips_per_day, 1) if clips_per_day > 0 else 0
            console.print(
                f"\n  [dim]Post frequency: {post_freq}/day × {max_cpkg} clips/pkg "
                f"= {clips_per_day} clips/day[/dim]\n"
                f"  [bold]Estimated content runway: [green]{days_est} days[/green] "
                f"({total} clips ÷ {clips_per_day}/day)[/bold]"
            )
        except Exception:
            pass

        # ── Top 5 clips by final_score with AI titles ─────────────────────────
        top_clips = conn.execute("""
            SELECT
                COALESCE(viral_title, title) AS display_title,
                creator_name,
                COALESCE(discovery_source, 'direct_api') AS dsrc,
                COALESCE(final_score, score, 0) AS fs,
                duration_sec,
                view_count,
                theme
            FROM shared_clips
            WHERE expires_at > datetime('now')
            ORDER BY fs DESC, fetched_at DESC
            LIMIT 5
        """).fetchall()
        conn.close()

        if top_clips:
            console.print(f"\n[bold]Top 5 clips right now:[/bold]")
            for i, row in enumerate(top_clips, 1):
                label = _source_labels.get(row["dsrc"], row["dsrc"])
                dur   = f"{int(row['duration_sec'] or 0)}s" if row["duration_sec"] else "?s"
                views = f"{int(row['view_count'] or 0):,}" if row["view_count"] else "?"
                theme = row["theme"] or ""
                console.print(
                    f"  [bold]{i}.[/bold] [[bold green]{row['fs']:.1f}[/bold green]] "
                    f"[white]{(row['display_title'] or '')[:55]}[/white]\n"
                    f"     [dim]{row['creator_name'] or '?'} — {label} — "
                    f"{dur} — {views} views — {theme}[/dim]"
                )

    except Exception as exc:
        console.print(f"[dim]Could not load quality stats: {exc}[/dim]")

    # ── Last pool runs ────────────────────────────────────────────────────────
    if runs:
        console.print()
        table = Table(title="Last Pool Runs", show_lines=True)
        table.add_column("run_id", style="dim", width=7)
        table.add_column("source", style="cyan", width=18)
        table.add_column("status", width=12)
        table.add_column("added", width=7)
        table.add_column("started", style="dim", width=18)
        for run in runs:
            table.add_row(
                str(run.get("run_id", "")),
                run.get("source", ""),
                run.get("status", ""),
                str(run.get("clips_added", 0)),
                (run.get("started_at") or "")[:16],
            )
        console.print(table)
    else:
        console.print(
            "[dim]No pool runs recorded yet. Run --refresh to populate the pool.[/dim]"
        )


def cmd_refresh() -> None:
    """Manually trigger a shared pool refresh."""
    _require_consent()
    from database import initialize_database
    import shared_pool as _sp
    from pool_fetcher import refresh_all_pools

    initialize_database()
    _sp.initialize_shared_pool_tables()

    try:
        from preferences import load_config, load_preferences
        config = load_config()
        prefs  = load_preferences()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)

    console.print("[cyan]Refreshing shared content pool (force mode)…[/cyan]")
    result = refresh_all_pools(user_config=config, user_prefs=prefs, force=True)

    if result.get("skipped"):
        console.print("[yellow]Pool is already fresh — no refresh needed.[/yellow]")
        console.print("[dim]Use --pool to see current pool stats.[/dim]")
    else:
        console.print(
            f"\n[green]Pool refresh complete:[/green]\n"
            f"  Twitch added          : {result.get('twitch_added', 0)}\n"
            f"  YouTube added         : {result.get('youtube_added', 0)}\n"
            f"  Kick added            : {result.get('kick_added', 0)}\n"
            f"  Viral discovery added : {result.get('viral_discovery_added', 0)}\n"
            f"    reddit              : {result.get('reddit_added', 0)}\n"
            f"    YouTube Shorts      : {result.get('youtube_shorts_added', result.get('viral_discovery_added', 0) - result.get('reddit_added', 0))}\n"
            f"    Medal.tv            : {result.get('medal_added', 0)}\n"
            f"    Streamable          : {result.get('streamable_added', 0)}\n"
            f"    Twitter             : {result.get('twitter_added', 0)}\n"
            f"  Total added           : {result.get('total_added', 0)}\n"
            f"  Expired               : {result.get('expired', 0)}\n"
            f"  Duration              : {result.get('duration_sec', 0.0)}s\n"
        )


def cmd_edit(clip_path: Optional[str] = None) -> None:
    """Launch the interactive custom clip editor."""
    _require_consent()
    from database import initialize_database
    from clip_editor import run_editor_cli

    initialize_database()
    run_editor_cli(clip_path=clip_path)


def cmd_learn(days: int = 30, dry_run: bool = False) -> None:
    """
    Analyse the last N days of post performance and update preferences.yaml
    to reflect what template, caption style, and posting hours are working.
    """
    _require_consent()
    from database import initialize_database
    from performance_learner import run_learning_cycle, display_learning_results

    initialize_database()
    console.print(
        f"\n[bold cyan]Performance Learner[/bold cyan]  "
        f"[dim]Analysing last {days} days of posts…[/dim]"
    )
    result = run_learning_cycle(user_id=1, days=days, dry_run=dry_run)
    display_learning_results(result)


def cmd_errors() -> None:
    """Display recent pipeline errors from the error log."""
    from database import initialize_database, get_recent_errors
    from rich.table import Table

    initialize_database()

    errors = get_recent_errors(limit=50)
    if not errors:
        console.print("[green]No errors in the log. Everything is running clean.[/green]")
        return

    table = Table(title="ClipCast Error Log", show_lines=True)
    table.add_column("ID",      style="dim",  width=5)
    table.add_column("Step",    style="cyan", width=10)
    table.add_column("Message",               width=50)
    table.add_column("Pkg",     style="dim",  width=5)
    table.add_column("Occurred", style="dim", width=18)

    for err in errors:
        table.add_row(
            str(err.get("error_id", "")),
            err.get("step", ""),
            (err.get("message") or "")[:50],
            str(err.get("package_id") or ""),
            (err.get("occurred_at") or "")[:16],
        )

    console.print(table)


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _run_once(test_mode: bool) -> None:
    """Shared logic for --run and --test commands."""
    from preferences import load_config, load_preferences
    from database import initialize_database
    from scheduler import run_automated_pipeline

    try:
        config = load_config()
        prefs  = load_preferences()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        console.print("Run [bold]python main.py --setup[/bold] to configure ClipCast.")
        sys.exit(1)

    initialize_database()
    run_automated_pipeline(
        user_config=config,
        user_prefs=prefs,
        test_mode=test_mode,
    )


def _check_first_run() -> bool:
    """
    Return True if preferences.yaml doesn't exist yet (first time running).
    Prompts the user to run --setup before anything else.
    """
    prefs_file = Path(__file__).parent / "preferences.yaml"
    if not prefs_file.exists():
        console.print(
            Panel(
                "[bold yellow]Welcome to ClipCast Studio![/bold yellow]\n\n"
                "It looks like this is your first time running ClipCast.\n"
                "Let's configure your preferences first.\n\n"
                "Run:  [bold]python main.py --setup[/bold]",
                border_style="yellow",
            )
        )
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="clipcast",
        description=(
            "ClipCast Studio — Automated viral clip editing and TikTok posting.\n"
            "Source: Twitch, YouTube, or drop your own files.\n"
            "Two modes: automated (scheduled) and manual (on demand)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py --setup\n"
            "  python main.py --test\n"
            "  python main.py --run\n"
            "  python main.py --schedule\n"
            "  python main.py --manual clips/manual/myclip.mp4\n"
            "  python main.py --manual https://clips.twitch.tv/AbcDefGhi\n"
            "  python main.py --manual https://www.youtube.com/watch?v=abc123\n"
            "  python main.py --queue\n"
            "  python main.py --status\n"
            "  python main.py --analytics\n"
            "  python main.py --preview\n"
            "  python main.py --errors\n"
            "  python main.py --blocked\n"
            "  python main.py --audit\n"
            "  python main.py --pool\n"
            "  python main.py --refresh\n"
            "  python main.py --edit /path/to/clip.mp4\n"
            "  python main.py --edit https://clips.twitch.tv/...\n"
        ),
    )

    group = parser.add_mutually_exclusive_group(required=True)

    group.add_argument(
        "--setup",
        action="store_true",
        help=(
            "Run the interactive preferences setup wizard. "
            "Do this first before running anything else."
        ),
    )

    group.add_argument(
        "--test",
        action="store_true",
        help=(
            "Run the full automated pipeline once: fetch, score, compile, and edit. "
            "Saves finished videos to clips/processed/ but does NOT post to TikTok. "
            "Great for verifying everything works before going live."
        ),
    )

    group.add_argument(
        "--run",
        action="store_true",
        help=(
            "Run the full automated pipeline once and post results to TikTok. "
            "Respects your posting schedule and daily limits."
        ),
    )

    group.add_argument(
        "--schedule",
        action="store_true",
        help=(
            "Start the full automated scheduler. Runs the pipeline every 6 hours, "
            "processes the posting queue every 60 seconds, and watches clips/manual/ "
            "for files you drop in. Runs until you press Ctrl+C."
        ),
    )

    group.add_argument(
        "--manual",
        metavar="FILE_OR_URL",
        help=(
            "Process a specific file or URL using your manual mode settings. "
            "Accepts a local file path (mp4, mov, avi, mkv) or a Twitch/YouTube URL. "
            "Posts immediately or schedules based on your manual_mode_posting_behavior "
            "preference."
        ),
    )

    group.add_argument(
        "--queue",
        action="store_true",
        help="Show the current posting queue with estimated post times.",
    )

    group.add_argument(
        "--status",
        action="store_true",
        help=(
            "Show system status: last post time, next scheduled post, "
            "today's post count, and queue depth."
        ),
    )

    group.add_argument(
        "--analytics",
        action="store_true",
        help=(
            "Fetch TikTok performance data for posted videos and display an "
            "analytics table with views, likes, comments, and shares."
        ),
    )

    group.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Review videos pending manual approval and approve or reject each one. "
            "Requires clip_preview_required=true in preferences.yaml."
        ),
    )

    group.add_argument(
        "--errors",
        action="store_true",
        help="Show recent pipeline errors from the error log.",
    )

    group.add_argument(
        "--blocked",
        action="store_true",
        help=(
            "View and manage the creator opt-out list. Creators on this list "
            "have their content automatically skipped during automated processing."
        ),
    )

    group.add_argument(
        "--audit",
        action="store_true",
        help=(
            "Export the last 30 days of audit logs to a CSV file in legal/. "
            "Includes every clip fetched, processed, and posted."
        ),
    )

    group.add_argument(
        "--pool",
        action="store_true",
        help=(
            "Show shared content pool statistics: total clips, by platform, "
            "average score, active reservations, and the last 5 pool runs."
        ),
    )

    group.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Manually trigger a full shared pool refresh right now. "
            "Fetches fresh clips from Twitch and YouTube and stores them in "
            "the shared pool. Respects YouTube quota limits."
        ),
    )

    group.add_argument(
        "--edit",
        metavar="FILE_OR_URL",
        nargs="?",
        const="",
        help=(
            "Launch the interactive custom clip editor. Optionally provide a "
            "file path or URL to open directly. Operations: trim, crop, "
            "caption, speed, music, overlay, fade, template, export, queue."
        ),
    )

    group.add_argument(
        "--learn",
        action="store_true",
        help=(
            "Analyse the last 30 days of post performance and auto-tune "
            "preferences.yaml: template, caption style, and best posting hours "
            "are updated to match what is actually getting views. Safe to run "
            "at any time — requires at least 3 posts per setting to update it."
        ),
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging output.",
    )

    return parser


def main() -> None:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)

    # Print header
    console.print(
        "\n[bold cyan]ClipCast Studio[/bold cyan]  "
        "[dim]Automated Viral Clip Pipeline[/dim]\n"
    )

    # Route to the correct command
    if args.setup:
        cmd_setup()

    elif args.test:
        if _check_first_run():
            sys.exit(0)
        cmd_test()

    elif args.run:
        if _check_first_run():
            sys.exit(0)
        cmd_run()

    elif args.schedule:
        if _check_first_run():
            sys.exit(0)
        cmd_schedule()

    elif args.manual:
        if _check_first_run():
            sys.exit(0)
        cmd_manual(args.manual)

    elif args.queue:
        cmd_queue()

    elif args.status:
        cmd_status()

    elif args.analytics:
        cmd_analytics()

    elif args.preview:
        cmd_preview()

    elif args.errors:
        cmd_errors()

    elif args.blocked:
        cmd_blocked()

    elif args.audit:
        cmd_audit_export()

    elif args.pool:
        cmd_pool()

    elif args.refresh:
        if _check_first_run():
            sys.exit(0)
        cmd_refresh()

    elif args.edit is not None:
        if _check_first_run():
            sys.exit(0)
        # args.edit is "" when --edit is used with no argument (nargs="?", const="")
        cmd_edit(clip_path=args.edit if args.edit else None)

    elif args.learn:
        if _check_first_run():
            sys.exit(0)
        cmd_learn()


if __name__ == "__main__":
    main()
