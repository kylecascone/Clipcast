"""
webapp/app.py
=============
ClipCast Studio — Flask web interface.
Run from the clipcast-studio root:  python webapp/app.py
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Make parent modules importable ────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.after_request
def add_ngrok_header(response):
    response.headers['ngrok-skip-browser-warning'] = '1'
    return response

app.config["SECRET_KEY"] = "clipcast-studio-secret"
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Track running jobs so we don't double-start
_pipeline_lock = threading.Lock()
_pipeline_running = False


# ══════════════════════════════════════════════════════════════════════════════
# SocketIO log handler — streams pipeline output to browser in real time
# ══════════════════════════════════════════════════════════════════════════════

class SocketIOHandler(logging.Handler):
    """Forwards log records to connected SocketIO clients."""

    def emit(self, record):  # noqa: D102
        try:
            msg = self.format(record)
            level = record.levelname.lower()
            socketio.emit("log", {"level": level, "msg": msg, "ts": time.strftime("%H:%M:%S")})
        except Exception:
            pass


_socketio_handler = SocketIOHandler()
_socketio_handler.setFormatter(logging.Formatter("%(name)s  %(message)s"))
logging.getLogger().addHandler(_socketio_handler)


# ══════════════════════════════════════════════════════════════════════════════
# Helper: run a ClipCast CLI command in a background thread
# ══════════════════════════════════════════════════════════════════════════════

def _run_cmd(args: list[str], job_name: str):
    global _pipeline_running
    with _pipeline_lock:
        if _pipeline_running:
            socketio.emit("job_status", {"job": job_name, "status": "already_running"})
            return
        _pipeline_running = True

    socketio.emit("job_status", {"job": job_name, "status": "started"})
    try:
        cmd = [sys.executable, str(ROOT / "main.py")] + args
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(ROOT),
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                socketio.emit("log", {"level": "info", "msg": line, "ts": time.strftime("%H:%M:%S")})
        proc.wait()
        status = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        status = "error"
        socketio.emit("log", {"level": "error", "msg": str(exc), "ts": time.strftime("%H:%M:%S")})
    finally:
        with _pipeline_lock:
            _pipeline_running = False
        socketio.emit("job_status", {"job": job_name, "status": status})


# ══════════════════════════════════════════════════════════════════════════════
# Pages
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/queue")
def queue_page():
    return render_template("queue.html")


@app.route("/schedule")
def schedule():
    return render_template("schedule.html")


@app.route("/api/schedule", methods=["GET"])
def api_schedule_get():
    try:
        import yaml
        schedule_path = ROOT / "schedule.yaml"
        if schedule_path.exists():
            with open(schedule_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/schedule", methods=["POST"])
def api_schedule_post():
    try:
        import yaml
        data = request.get_json(force=True)
        schedule_path = ROOT / "schedule.yaml"
        with open(schedule_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/creators")
def api_creators():
    """Return trending creators for schedule dropdowns."""
    try:
        from trending_discovery import get_top_trending_creators
        trending = get_top_trending_creators(limit=25, max_age_hours=24)
        if trending:
            return jsonify(trending)
    except Exception:
        pass
    # Fallback: distinct creator names from shared_clips
    try:
        from database import get_connection
        conn = get_connection()
        rows = conn.execute(
            "SELECT DISTINCT creator_name FROM shared_clips WHERE creator_name IS NOT NULL "
            "AND creator_name != '' ORDER BY creator_name LIMIT 100"
        ).fetchall()
        conn.close()
        return jsonify([{"creator_name": r[0], "platform": "twitch", "viewer_count": 0, "viral_signal_boost": 0} for r in rows])
    except Exception as exc:
        return jsonify([])


@app.route("/api/trending-creators")
def api_trending_creators():
    """Return all trending creators with full stats for the Settings page."""
    try:
        from trending_discovery import get_top_trending_creators
        creators = get_top_trending_creators(limit=50, max_age_hours=48)
        return jsonify(creators)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/analytics")
def analytics():
    return render_template("analytics.html")


@app.route("/editor")
def editor():
    return render_template("editor.html")


@app.route("/settings")
def settings():
    return render_template("settings.html")


@app.route("/pool")
def pool():
    return render_template("pool.html")


@app.route("/blocked")
def blocked():
    return render_template("blocked.html")


@app.route("/accounts")
def accounts():
    return render_template("accounts.html")


@app.route("/api/accounts/status")
def api_accounts_status():
    try:
        import yaml
        accounts_path = ROOT / "accounts.yaml"
        if accounts_path.exists():
            with open(accounts_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/accounts/save", methods=["POST"])
def api_accounts_save():
    try:
        import yaml
        data = request.get_json(force=True)
        accounts_path = ROOT / "accounts.yaml"
        with open(accounts_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/accounts/youtube/test", methods=["POST"])
def api_youtube_test():
    try:
        from youtube_uploader import test_youtube_connection
        success = test_youtube_connection()
        return jsonify({"connected": success})
    except Exception as exc:
        return jsonify({"connected": False, "error": str(exc)}), 500


@app.route("/api/accounts/youtube/auth", methods=["POST"])
def api_youtube_auth():
    try:
        from youtube_uploader import get_youtube_client
        youtube = get_youtube_client()
        response = youtube.channels().list(part="snippet", mine=True).execute()
        channels = response.get("items", [])
        channel_name = channels[0]["snippet"]["title"] if channels else "Unknown"
        return jsonify({"ok": True, "channel": channel_name})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/how-to")
def how_to():
    return render_template("how_to.html")


# ══════════════════════════════════════════════════════════════════════════════
# API — Stats
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    try:
        from database import get_connection
        conn = get_connection()
        cur = conn.cursor()

        # Queue count
        cur.execute("SELECT COUNT(*) FROM posting_queue WHERE status='pending'")
        queue_count = cur.fetchone()[0]

        # Posts today
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cur.execute(
            "SELECT COUNT(*) FROM posting_queue WHERE status='posted' AND posted_at LIKE ?",
            (f"{today}%",),
        )
        posts_today = cur.fetchone()[0]

        # Pool size (fresh = not expired)
        try:
            cur.execute(
                "SELECT COUNT(*) FROM shared_clips WHERE is_blocked=0 "
                "AND (expires_at IS NULL OR expires_at > datetime('now'))"
            )
            pool_size = cur.fetchone()[0]
        except Exception:
            pool_size = 0

        # Videos processed
        processed_dir = ROOT / "clips" / "processed"
        processed_count = len(list(processed_dir.glob("*.mp4"))) if processed_dir.exists() else 0

        conn.close()
        return jsonify({
            "queue_pending": queue_count,
            "posts_today": posts_today,
            "pool_size": pool_size,
            "videos_processed": processed_count,
            "pipeline_running": _pipeline_running,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — Queue
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/queue")
def api_queue():
    try:
        from database import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT q.queue_id as id,
                      COALESCE(sc.title, p.caption_text, 'Untitled') as clip_title,
                      sc.thumbnail_path,
                      sc.creator_name,
                      sc.duration_sec as duration,
                      q.platform,
                      q.status,
                      q.scheduled_time as scheduled_for,
                      q.posted_at,
                      q.package_id
               FROM posting_queue q
               LEFT JOIN packages p ON p.package_id = q.package_id
               LEFT JOIN shared_clips sc ON (
                   sc.shared_clip_id = CAST(TRIM(p.clip_ids, '[]" ') AS INTEGER)
               )
               ORDER BY q.scheduled_time ASC LIMIT 100"""
        )
        rows = cur.fetchall()
        conn.close()
        items = [dict(zip([d[0] for d in cur.description], row)) for row in rows]
        for item in items:
            t = item.get("clip_title") or ""
            if len(t) > 70:
                item["clip_title"] = t[:70] + "…"
        return jsonify(items)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/queue/<int:item_id>/cancel", methods=["POST"])
def api_queue_cancel(item_id):
    try:
        from database import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE posting_queue SET status='cancelled' WHERE queue_id=? AND status='pending'",
            (item_id,),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — Pool
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pool")
def api_pool():
    try:
        from database import get_connection
        conn = get_connection()
        cur = conn.cursor()
        source = request.args.get("source", "")
        limit  = min(int(request.args.get("limit", 50)), 200)
        query  = """SELECT shared_clip_id as id, title, creator_name, source,
                           view_count, duration_sec as duration, score,
                           url as clip_url, is_blocked,
                           expires_at, fetched_at
                    FROM shared_clips WHERE is_blocked=0 AND 1=1"""
        params: list = []
        if source:
            query += " AND source=?"
            params.append(source)
        query += " ORDER BY view_count DESC LIMIT ?"
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()
        items = [dict(zip([d[0] for d in cur.description], row)) for row in rows]
        return jsonify(items)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — Analytics
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/analytics")
def api_analytics():
    try:
        from database import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT p.platform,
                      p.view_count  AS views,
                      p.like_count  AS likes,
                      p.share_count AS shares,
                      p.comment_count AS comments,
                      p.fetched_at,
                      pkg.caption_text AS clip_title
               FROM performance p
               LEFT JOIN packages pkg ON pkg.package_id = p.package_id
               ORDER BY p.fetched_at DESC LIMIT 50"""
        )
        rows = cur.fetchall()
        conn.close()
        items = [dict(zip([d[0] for d in cur.description], row)) for row in rows]
        return jsonify(items)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — Settings (preferences.yaml)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    try:
        from preferences import load_preferences
        prefs = load_preferences()
        return jsonify(prefs)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    try:
        import yaml
        data = request.get_json(force=True)
        prefs_path = ROOT / "preferences.yaml"

        # Load current, update with provided keys, write back
        with open(prefs_path) as f:
            current = yaml.safe_load(f) or {}

        # Only allow safe writable keys (no secrets)
        WRITABLE_KEYS = {
            "clip_length", "post_frequency", "posting_times",
            "max_clips_per_compilation", "minimum_clip_quality_score",
            "minimum_views", "allow_clips_from_non_target_streamers",
            "global_pool_size", "allow_youtube_trending",
            "youtube_global_pool_enabled", "youtube_lookback_days",
            "default_video_template", "default_caption_style",
            "animated_captions_enabled", "animated_caption_style",
            "animated_caption_font_size", "smart_hashtags_enabled",
            "smart_hashtags_max", "thumbnail_enabled",
            "learning_enabled", "learning_lookback_days",
            "clip_preview_required", "youtube_enabled", "twitch_enabled",
            "target_streamers", "target_platforms",
        }
        for key, val in data.items():
            if key in WRITABLE_KEYS:
                current[key] = val

        with open(prefs_path, "w") as f:
            yaml.dump(current, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — Blocked creators
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/blocked")
def api_blocked():
    try:
        from blocked_creators import load_blocked
        data = load_blocked()
        # Flatten into list of {name, platform} dicts
        result = []
        for name in data.get("twitch", []):
            result.append({"name": name, "platform": "twitch"})
        for name in data.get("youtube", []):
            result.append({"name": name, "platform": "youtube"})
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/blocked/<name>", methods=["DELETE"])
def api_blocked_remove(name):
    try:
        from blocked_creators import remove_blocked
        platform = request.args.get("platform", "twitch")
        remove_blocked(name, platform)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/blocked", methods=["POST"])
def api_blocked_add():
    try:
        from blocked_creators import add_blocked
        data = request.get_json(force=True)
        name     = data.get("name", "").strip()
        platform = data.get("platform", "twitch")
        if not name:
            return jsonify({"error": "Name is required"}), 400
        add_blocked(name, platform)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — Pipeline actions
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/run", methods=["POST"])
def api_run():
    if _pipeline_running:
        return jsonify({"error": "Pipeline already running"}), 409
    mode = request.json.get("mode", "test") if request.is_json else "test"
    flag = "--run" if mode == "run" else "--test"
    t = threading.Thread(target=_run_cmd, args=([flag], f"pipeline_{mode}"), daemon=True)
    t.start()
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/refresh-pool", methods=["POST"])
def api_refresh_pool():
    if _pipeline_running:
        return jsonify({"error": "Pipeline already running"}), 409
    t = threading.Thread(target=_run_cmd, args=(["--refresh"], "pool_refresh"), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/manual", methods=["POST"])
def api_manual():
    if _pipeline_running:
        return jsonify({"error": "Pipeline already running"}), 409
    data = request.get_json(force=True)
    url_or_path = data.get("url", "").strip()
    if not url_or_path:
        return jsonify({"error": "No URL or path provided"}), 400
    t = threading.Thread(
        target=_run_cmd,
        args=(["--manual", url_or_path], "manual"),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


@app.route("/api/processed-videos")
def api_processed_videos():
    processed_dir = ROOT / "clips" / "processed"
    if not processed_dir.exists():
        return jsonify([])
    videos = []
    for f in sorted(processed_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)[:20]:
        videos.append({
            "name": f.name,
            "size_mb": round(f.stat().st_size / 1_048_576, 1),
            "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify(videos)


# ══════════════════════════════════════════════════════════════════════════════
# SocketIO events
# ══════════════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def on_connect():
    emit("log", {"level": "info", "msg": "Connected to ClipCast Studio", "ts": time.strftime("%H:%M:%S")})


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  ClipCast Studio Web Interface")
    port = int(os.environ.get("PORT", 5001))
    print(f"  http://127.0.0.1:{port}")
    print("=" * 60)

    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
        import threading
        from main import _bootstrap_railway, cmd_schedule
        _bootstrap_railway()
        scheduler_thread = threading.Thread(target=cmd_schedule, daemon=True)
        scheduler_thread.start()
        print("Railway: scheduler started in background thread", flush=True)

    socketio.run(app, host="0.0.0.0", port=port, debug=False)
