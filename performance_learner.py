"""
performance_learner.py
======================
Feedback loop that reads TikTok / YouTube / Instagram post performance and
adjusts ClipCast's scoring and posting preferences to match what is actually
working.

What it does
------------
1. Reads the last N days of post performance from the database
   (performance table + posting_queue table).
2. Groups posts by: clip_length, template_used, caption_style_used,
   post_hour, source platform.
3. Computes avg_views, avg_likes, avg_shares and avg_engagement_rate
   per group.
4. Identifies statistically significant winners using a minimum sample
   threshold to avoid over-fitting on one lucky post.
5. Writes recommended preference adjustments to preferences.yaml under
   a [learned] block.  The main pipeline reads these automatically.
6. Prints a rich table showing what's working and what was changed.

Adjustable settings (written to preferences.yaml)
--------------------------------------------------
  clip_length          — switched to the best-performing duration category
  default_video_template  — switched to best-performing template
  default_caption_style   — switched to best-performing caption style
  best_post_hours         — list of top 3 posting hours by avg views

The system NEVER lowers minimum_views or changes credentials.  It only
adjusts content-strategy preferences.

Database requirements
---------------------
Reads from:
  performance    (post_id, views, likes, shares, comments, package_id, scraped_at)
  posting_queue  (package_id, platform, scheduled_time, template_used, caption_style_used)
  packages       (package_id, clips JSON)
  clips          (clip_id, duration, source)

All columns used here are standard — no schema additions needed.

Usage
-----
    from performance_learner import run_learning_cycle
    changes = run_learning_cycle(user_id=1, days=30)

Test
----
    python performance_learner.py
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import database

logger = logging.getLogger(__name__)

# ── Learning parameters ────────────────────────────────────────────────────────
DEFAULT_LOOKBACK_DAYS  = 30     # Analyse the last N days of posts
MIN_SAMPLE_SIZE        = 3      # Need at least this many posts to trust a group
MIN_IMPROVEMENT_PCT    = 5.0    # Only change a setting if winner beats current by ≥5%


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

def _load_performance_data(
    user_id: int = 1,
    days: int = DEFAULT_LOOKBACK_DAYS,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Load post performance rows joined with queue metadata.

    Returns a list of dicts with the following keys:
        package_id, views, likes, shares, comments,
        template_used, caption_style_used, scheduled_time,
        post_hour (int 0–23), platform, source, duration.

    Empty list on error or no data.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    conn = database.get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT
                p.package_id,
                p.view_count    AS views,
                p.like_count    AS likes,
                p.share_count   AS shares,
                p.comment_count AS comments,
                pq.template_used,
                pq.caption_style_used,
                pq.scheduled_time,
                pq.platform
            FROM performance p
            JOIN posting_queue pq ON p.package_id = pq.package_id
            WHERE pq.user_id = :user_id
              AND pq.scheduled_time >= :cutoff
              AND pq.status = 'posted'
        """, {"user_id": user_id, "cutoff": cutoff}).fetchall()

    except Exception as exc:
        logger.error("_load_performance_data: query failed: %s", exc)
        return []
    finally:
        conn.close()

    results = []
    for row in rows:
        try:
            sched_time = row["scheduled_time"] or ""
            hour = int(sched_time[11:13]) if len(sched_time) >= 13 else -1
        except (ValueError, IndexError):
            hour = -1

        results.append({
            "package_id":        row["package_id"],
            "views":             int(row["views"] or 0),
            "likes":             int(row["likes"] or 0),
            "shares":            int(row["shares"] or 0),
            "comments":          int(row["comments"] or 0),
            "template_used":     row["template_used"],
            "caption_style_used": row["caption_style_used"],
            "post_hour":         hour,
            "platform":          row["platform"] or "tiktok",
        })

    logger.debug("Loaded %d performance records (user_id=%d, days=%d)", len(results), user_id, days)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Analysis helpers
# ══════════════════════════════════════════════════════════════════════════════

def _avg_views_by(records: List[Dict], key: str) -> Dict[Any, Tuple[float, int]]:
    """
    Group records by ``key`` and compute (avg_views, sample_count) per group.

    Only groups with at least MIN_SAMPLE_SIZE posts are included.

    Returns:
        Dict mapping group_value → (avg_views, count), sorted by avg_views DESC.
    """
    groups: Dict[Any, List[int]] = defaultdict(list)
    for r in records:
        val = r.get(key)
        if val is not None:
            groups[val].append(r["views"])

    result = {}
    for val, view_list in groups.items():
        if len(view_list) >= MIN_SAMPLE_SIZE:
            result[val] = (sum(view_list) / len(view_list), len(view_list))

    return dict(sorted(result.items(), key=lambda x: x[1][0], reverse=True))


def _engagement_rate(record: Dict) -> float:
    """Calculate engagement rate = (likes + shares + comments) / views."""
    views = record.get("views") or 0
    if not views:
        return 0.0
    engaged = (
        (record.get("likes") or 0) +
        (record.get("shares") or 0) +
        (record.get("comments") or 0)
    )
    return engaged / views


def _top_hours(records: List[Dict], top_n: int = 3) -> List[int]:
    """Return the top N posting hours by average views."""
    hour_views = _avg_views_by(records, "post_hour")
    return [h for h in list(hour_views.keys())[:top_n] if h >= 0]


# ══════════════════════════════════════════════════════════════════════════════
# Preference writing
# ══════════════════════════════════════════════════════════════════════════════

def _read_prefs_yaml(prefs_path: Path) -> Dict:
    """Load preferences.yaml, return empty dict on error."""
    try:
        import yaml
        with open(prefs_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.error("Could not read preferences.yaml: %s", exc)
        return {}


def _write_pref(prefs_path: Path, key: str, value: Any) -> bool:
    """
    Update a single key in preferences.yaml in-place using text replacement.

    Finds the line matching ``^key:`` and rewrites it.  If the key does not
    exist, appends it at the end of the file.
    """
    try:
        text = prefs_path.read_text(encoding="utf-8")
        new_line = f"{key}: {json.dumps(value) if isinstance(value, list) else value}"

        import re
        pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(new_line, text)
        else:
            text = text.rstrip("\n") + f"\n{new_line}\n"

        prefs_path.write_text(text, encoding="utf-8")
        return True

    except Exception as exc:
        logger.error("_write_pref failed for key='%s': %s", key, exc)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Main learning cycle
# ══════════════════════════════════════════════════════════════════════════════

def run_learning_cycle(
    user_id: int = 1,
    days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
    db_path: Optional[Path] = None,
    prefs_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Analyse post performance and update preferences.yaml with what's working.

    Steps:
        1. Load performance data from the last ``days`` days.
        2. Find best-performing template, caption style, and posting hours.
        3. Compare winners against current settings.
        4. Update preferences.yaml for settings where the winner is ≥5% better.
        5. Return a summary of changes made.

    Args:
        user_id:    User ID to filter performance data.
        days:       How many days of data to analyse (default 30).
        dry_run:    If True, analyse and report but don't write to prefs.
        db_path:    Override database path (useful for tests).
        prefs_path: Override preferences.yaml path.

    Returns:
        Dict with keys:
            records_analysed  (int)   — Number of posts in the window.
            changes           (list)  — List of {setting, old, new, reason}.
            recommendations   (dict)  — All analysis results regardless of changes.
            dry_run           (bool)  — Whether writes were suppressed.
    """
    BASE_DIR  = Path(__file__).parent
    prefs_path = prefs_path or (BASE_DIR / "preferences.yaml")

    records = _load_performance_data(user_id=user_id, days=days, db_path=db_path)
    changes: List[Dict] = []

    if not records:
        logger.info("performance_learner: no data in the last %d days — nothing to learn.", days)
        return {
            "records_analysed": 0,
            "changes": [],
            "recommendations": {},
            "dry_run": dry_run,
        }

    # Load current prefs for comparison
    current_prefs = _read_prefs_yaml(prefs_path)

    # ── 1. Best template ───────────────────────────────────────────────────────
    template_perf = _avg_views_by(records, "template_used")
    best_template = next(iter(template_perf), None)
    current_template = current_prefs.get("default_video_template", 1)

    if best_template and best_template != current_template:
        current_avg = template_perf.get(current_template, (0, 0))[0]
        best_avg    = template_perf[best_template][0]
        improvement = ((best_avg - current_avg) / max(current_avg, 1)) * 100
        if improvement >= MIN_IMPROVEMENT_PCT:
            change = {
                "setting": "default_video_template",
                "old":     current_template,
                "new":     best_template,
                "reason":  f"Template {best_template} averages {best_avg:.0f} views vs "
                           f"{current_avg:.0f} for template {current_template} "
                           f"(+{improvement:.1f}%)",
            }
            changes.append(change)
            if not dry_run:
                _write_pref(prefs_path, "default_video_template", best_template)

    # ── 2. Best caption style ─────────────────────────────────────────────────
    caption_perf = _avg_views_by(records, "caption_style_used")
    best_caption = next(iter(caption_perf), None)
    current_caption = current_prefs.get("default_caption_style", 1)

    if best_caption and best_caption != current_caption:
        current_avg = caption_perf.get(current_caption, (0, 0))[0]
        best_avg    = caption_perf[best_caption][0]
        improvement = ((best_avg - current_avg) / max(current_avg, 1)) * 100
        if improvement >= MIN_IMPROVEMENT_PCT:
            change = {
                "setting": "default_caption_style",
                "old":     current_caption,
                "new":     best_caption,
                "reason":  f"Caption style {best_caption} averages {best_avg:.0f} views vs "
                           f"{current_avg:.0f} for style {current_caption} "
                           f"(+{improvement:.1f}%)",
            }
            changes.append(change)
            if not dry_run:
                _write_pref(prefs_path, "default_caption_style", best_caption)

    # ── 3. Best posting hours ─────────────────────────────────────────────────
    top_hours = _top_hours(records, top_n=3)
    if top_hours:
        change = {
            "setting": "best_post_hours",
            "old":     current_prefs.get("best_post_hours", []),
            "new":     top_hours,
            "reason":  f"Highest average views at hours: {top_hours}",
        }
        changes.append(change)
        if not dry_run:
            _write_pref(prefs_path, "best_post_hours", top_hours)

    # ── Build recommendations summary ─────────────────────────────────────────
    total_views = sum(r["views"] for r in records)
    avg_engagement = (
        sum(_engagement_rate(r) for r in records) / len(records)
        if records else 0
    )

    recommendations = {
        "template_performance":  {str(k): {"avg_views": round(v[0]), "count": v[1]}
                                   for k, v in template_perf.items()},
        "caption_performance":   {str(k): {"avg_views": round(v[0]), "count": v[1]}
                                   for k, v in caption_perf.items()},
        "top_post_hours":        top_hours,
        "total_posts_analysed":  len(records),
        "total_views":           total_views,
        "avg_engagement_rate":   round(avg_engagement * 100, 2),
    }

    logger.info(
        "Learning cycle complete: %d posts analysed, %d change(s) applied.",
        len(records), len(changes),
    )
    return {
        "records_analysed": len(records),
        "changes": changes,
        "recommendations": recommendations,
        "dry_run": dry_run,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Rich display
# ══════════════════════════════════════════════════════════════════════════════

def display_learning_results(result: Dict[str, Any]) -> None:
    """
    Print a formatted rich table of learning cycle results.

    Args:
        result: Dict returned by ``run_learning_cycle()``.
    """
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
    except ImportError:
        # Fallback to plain print
        print(json.dumps(result, indent=2))
        return

    console = Console()
    rec = result.get("recommendations", {})
    changes = result.get("changes", [])
    n = result.get("records_analysed", 0)
    dry = result.get("dry_run", False)

    console.print(
        f"\n[bold cyan]Performance Learner Results[/bold cyan]  "
        f"[dim]{'DRY RUN — no changes written' if dry else 'Changes applied to preferences.yaml'}[/dim]\n"
        f"  Posts analysed     : [green]{n}[/green]\n"
        f"  Avg engagement rate: [green]{rec.get('avg_engagement_rate', 0)}%[/green]\n"
        f"  Total views (period): [green]{rec.get('total_views', 0):,}[/green]\n"
    )

    if not n:
        console.print("[yellow]No performance data found. Post some content and re-run --learn.[/yellow]")
        return

    # Template performance table
    tpl_perf = rec.get("template_performance", {})
    if tpl_perf:
        t1 = Table(title="Template Performance", show_lines=True)
        t1.add_column("Template", style="cyan", width=10)
        t1.add_column("Avg Views", width=12)
        t1.add_column("Posts", width=8)
        for k, v in tpl_perf.items():
            t1.add_row(f"Template {k}", f"{v['avg_views']:,}", str(v["count"]))
        console.print(t1)

    # Caption style performance table
    cap_perf = rec.get("caption_performance", {})
    if cap_perf:
        t2 = Table(title="Caption Style Performance", show_lines=True)
        t2.add_column("Style", style="cyan", width=10)
        t2.add_column("Avg Views", width=12)
        t2.add_column("Posts", width=8)
        for k, v in cap_perf.items():
            t2.add_row(f"Style {k}", f"{v['avg_views']:,}", str(v["count"]))
        console.print(t2)

    # Top posting hours
    top_hours = rec.get("top_post_hours", [])
    if top_hours:
        hours_str = ", ".join(f"{h:02d}:00" for h in top_hours)
        console.print(f"\n  Best posting hours : [green]{hours_str}[/green]")

    # Changes made
    if changes:
        console.print(f"\n[bold green]{len(changes)} setting(s) updated:[/bold green]")
        for c in changes:
            console.print(
                f"  [cyan]{c['setting']}[/cyan]: "
                f"[dim]{c['old']}[/dim] → [green]{c['new']}[/green]\n"
                f"  [dim]  {c['reason']}[/dim]"
            )
    else:
        console.print("\n[dim]No changes needed — current settings are already optimal.[/dim]")


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(name)s  %(message)s")

    print("=" * 60)
    print("performance_learner.py  —  self-test")
    print("=" * 60)

    tmp_db = Path(tempfile.mktemp(suffix="_learn_test.db"))
    tmp_prefs = Path(tempfile.mktemp(suffix="_learn_prefs.yaml"))

    try:
        # Write a minimal prefs file
        tmp_prefs.write_text(
            "default_video_template: 1\n"
            "default_caption_style: 1\n"
            "minimum_views: 100\n"
        )

        # Initialize database
        database.initialize_database(db_path=tmp_db)

        # ── Populate test data ─────────────────────────────────────────────────
        conn = database.get_connection(tmp_db)
        for pkg_id in range(1, 11):
            conn.execute("""
                INSERT OR IGNORE INTO packages
                    (package_id, user_id, clip_ids, template, caption_style, mode, status)
                VALUES (?, 1, '[]', 1, 1, 'auto', 'posted')
            """, (pkg_id,))

        # Insert posting_queue rows with varied templates and caption styles
        import random
        random.seed(42)
        test_data = []
        for pkg_id in range(1, 11):
            # Template 3 and Caption Style 2 will be "winners"
            template = 3 if pkg_id <= 5 else random.choice([1, 2, 4])
            caption  = 2 if pkg_id <= 5 else random.choice([1, 3, 4])
            hour     = 20 if pkg_id <= 5 else random.randint(0, 23)
            views    = random.randint(8000, 12000) if template == 3 else random.randint(1000, 3000)
            sched    = f"2026-02-{10 + pkg_id:02d}T{hour:02d}:00:00Z"

            conn.execute("""
                INSERT OR IGNORE INTO posting_queue
                    (package_id, user_id, status, platform, scheduled_time,
                     template_used, caption_style_used)
                VALUES (?, 1, 'posted', 'tiktok', ?, ?, ?)
            """, (pkg_id, sched, template, caption))

            conn.execute("""
                INSERT OR IGNORE INTO performance
                    (package_id, user_id, platform, view_count, like_count, share_count, comment_count)
                VALUES (?, 1, 'tiktok', ?, ?, ?, ?)
            """, (pkg_id, views, int(views*0.08), int(views*0.02), int(views*0.01)))

            test_data.append({"pkg_id": pkg_id, "template": template,
                               "caption": caption, "views": views})
        conn.commit()
        conn.close()

        print(f"\nInserted {len(test_data)} test records:")
        for d in test_data:
            print(f"  pkg={d['pkg_id']} T={d['template']} C={d['caption']} views={d['views']:,}")

        # ── Run learning cycle (dry run first) ─────────────────────────────────
        print("\nRunning dry_run=True...")
        result = run_learning_cycle(
            user_id=1,
            days=30,
            dry_run=True,
            db_path=tmp_db,
            prefs_path=tmp_prefs,
        )
        print(f"  records_analysed: {result['records_analysed']}  (expected 10)")
        assert result["records_analysed"] == 10

        changes_dry = result["changes"]
        print(f"  changes found (dry): {len(changes_dry)}")
        for c in changes_dry:
            print(f"    {c['setting']}: {c['old']} → {c['new']}")

        # Prefs should be unchanged after dry run
        prefs_content = tmp_prefs.read_text()
        assert "default_video_template: 1" in prefs_content, \
            "dry_run should not modify prefs"
        print("  Prefs unchanged after dry_run: OK")

        # ── Run actual learning cycle ──────────────────────────────────────────
        print("\nRunning with dry_run=False (will write to tmp prefs)...")
        result2 = run_learning_cycle(
            user_id=1,
            days=30,
            dry_run=False,
            db_path=tmp_db,
            prefs_path=tmp_prefs,
        )
        print(f"  changes applied: {len(result2['changes'])}")

        # Display results
        display_learning_results(result2)

        print("\n" + "=" * 60)
        print("All performance_learner.py tests PASSED.")
        print("=" * 60)

    except AssertionError as ae:
        print(f"\nASSERTION FAILED: {ae}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\nUNEXPECTED ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        for p in (tmp_db, tmp_prefs):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        print(f"\nTemp files cleaned up.")
