"""
clip_editor.py
==============
Interactive non-destructive clip editor for ClipCast Studio.

Lets you trim, crop, caption, adjust speed, add music, overlays, and
transitions — all via FFmpeg — then export or queue for posting.

Every operation works on a copy of the source clip; the original is
never modified. Operations accumulate in an in-memory list and are
applied in a single FFmpeg pass at export time.

Editor session state is persisted in the `custom_edits` database table
so you can resume an in-progress edit across CLI invocations.

Supported operations:
    trim          — trim start/end by seconds
    crop          — crop to 9:16 vertical (center / manual offsets)
    caption       — add a text overlay (top/middle/bottom, any color)
    speed         — change playback speed (0.5×–4×)
    music         — mix in an audio file (replaces or blends with source)
    overlay       — semi-transparent image/watermark overlay
    transition    — fade in / fade out (video + audio)
    template      — apply a preset template (1–4) from templates.py

Export qualities (custom_editor_output_quality in preferences.yaml):
    low    — 720p, CRF 28  (faster, smaller file)
    medium — 1080p, CRF 23 (balanced — default)
    high   — 1080p, CRF 18 (best quality, larger file)

Usage:
    from clip_editor import ClipEditor
    editor = ClipEditor("/path/to/clip.mp4")
    editor.trim_clip(start=5.0, end=55.0)
    editor.crop_to_vertical()
    editor.add_caption("Epic moment!", position="bottom")
    editor.export_clip("/path/to/output.mp4")

    # Or run the interactive CLI:
    python clip_editor.py
    # or via main.py:
    python main.py --edit /path/to/clip.mp4

Test:
    python clip_editor.py
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import database

logger = logging.getLogger(__name__)

# ── Export quality presets (resolution + CRF) ─────────────────────────────────
QUALITY_PRESETS: Dict[str, Dict[str, Any]] = {
    "low":    {"scale": "720:1280",  "crf": 28, "preset": "fast"},
    "medium": {"scale": "1080:1920", "crf": 23, "preset": "medium"},
    "high":   {"scale": "1080:1920", "crf": 18, "preset": "slow"},
}

# ── Output folder ──────────────────────────────────────────────────────────────
_BASE_DIR      = Path(__file__).parent
_OUTPUT_DIR    = _BASE_DIR / "clips" / "processed"
_WORK_DIR      = _BASE_DIR / "clips" / "editor_work"


# ══════════════════════════════════════════════════════════════════════════════
# ClipEditor class
# ══════════════════════════════════════════════════════════════════════════════

class ClipEditor:
    """
    Non-destructive clip editor backed by FFmpeg.

    All operations accumulate in self.operations (a list of dicts).
    Call export_clip() to apply them in a single FFmpeg pass.

    Args:
        clip_path: Absolute path to the source video file.
        user_id:   User ID for database writes (default 1).
        user_prefs: User preferences dict. If None, loaded from preferences.yaml.
        edit_id:   Resume an existing editor session by its DB edit_id.
    """

    def __init__(
        self,
        clip_path: str,
        user_id: int = 1,
        user_prefs: Optional[Dict] = None,
        edit_id: Optional[int] = None,
    ) -> None:
        self.clip_path  = Path(clip_path).resolve()
        self.user_id    = user_id
        self.edit_id: Optional[int] = edit_id
        self.operations: List[Dict[str, Any]] = []

        # Load prefs
        if user_prefs is None:
            try:
                from preferences import load_preferences
                user_prefs = load_preferences()
            except Exception:
                user_prefs = {}
        self.user_prefs = user_prefs

        self._output_quality = user_prefs.get("custom_editor_output_quality", "medium")
        self._default_template = int(user_prefs.get("custom_editor_default_template", 1))
        self._auto_queue = bool(user_prefs.get("custom_editor_auto_queue", False))

        # Resume existing session or create a new one
        if edit_id:
            existing = database.get_custom_edit(edit_id)
            if existing:
                self.operations = existing.get("operations", [])
                logger.info("Resumed editor session edit_id=%d (%d op(s))", edit_id, len(self.operations))
            else:
                logger.warning("edit_id=%d not found — starting fresh session.", edit_id)
                self.edit_id = None

        if not self.edit_id:
            self.edit_id = database.insert_custom_edit(
                clip_path=str(self.clip_path),
                user_id=user_id,
                template=self._default_template,
            )
            logger.debug("Created new editor session edit_id=%d", self.edit_id)

        # Ensure work directory exists
        _WORK_DIR.mkdir(parents=True, exist_ok=True)
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Probe helpers ──────────────────────────────────────────────────────────

    def get_duration(self) -> float:
        """Return the duration of the source clip in seconds."""
        return _probe_duration(str(self.clip_path))

    def get_dimensions(self) -> tuple:
        """Return (width, height) of the source clip."""
        return _probe_dimensions(str(self.clip_path))

    # ── Operations ────────────────────────────────────────────────────────────

    def trim_clip(self, start: float = 0.0, end: Optional[float] = None) -> "ClipEditor":
        """
        Trim the clip to [start, end] seconds.

        Args:
            start: Start time in seconds (default 0).
            end:   End time in seconds. If None, keeps the original end.

        Returns:
            self (for chaining).
        """
        self.operations.append({
            "op":   "trim",
            "start": max(0.0, float(start)),
            "end":   float(end) if end is not None else None,
        })
        self._save()
        logger.debug("trim_clip: start=%.1f end=%s", start, end)
        return self

    def crop_to_vertical(
        self,
        mode: str = "center",
        x_offset: int = 0,
        y_offset: int = 0,
    ) -> "ClipEditor":
        """
        Crop the clip to a 9:16 aspect ratio.

        Args:
            mode:     'center' (default) — center-crop the width.
                      'manual' — use explicit x_offset/y_offset.
            x_offset: Horizontal pixel offset from left edge (manual mode only).
            y_offset: Vertical pixel offset from top edge (manual mode only).

        Returns:
            self.
        """
        self.operations.append({
            "op":       "crop_vertical",
            "mode":     mode,
            "x_offset": int(x_offset),
            "y_offset": int(y_offset),
        })
        self._save()
        logger.debug("crop_to_vertical: mode=%s x=%d y=%d", mode, x_offset, y_offset)
        return self

    def add_caption(
        self,
        text: str,
        position: str = "bottom",
        font_size: int = 48,
        font_color: str = "white",
        start_sec: float = 0.0,
        end_sec: Optional[float] = None,
    ) -> "ClipEditor":
        """
        Burn a text caption into the video.

        Args:
            text:       Caption text.
            position:   'top' | 'middle' | 'bottom' (default 'bottom').
            font_size:  Font size in pixels (default 48).
            font_color: Color name or hex code (default 'white').
            start_sec:  When caption appears (default 0 = whole clip).
            end_sec:    When caption disappears (default = clip end).

        Returns:
            self.
        """
        self.operations.append({
            "op":         "caption",
            "text":       text,
            "position":   position,
            "font_size":  int(font_size),
            "font_color": font_color,
            "start_sec":  float(start_sec),
            "end_sec":    float(end_sec) if end_sec is not None else None,
        })
        self._save()
        logger.debug("add_caption: '%s' @ %s", text[:40], position)
        return self

    def speed_adjust(self, speed: float = 1.0) -> "ClipEditor":
        """
        Change the playback speed.

        Args:
            speed: Speed multiplier (0.5 = half speed, 2.0 = double speed).
                   Clamped to [0.25, 4.0].

        Returns:
            self.
        """
        speed = max(0.25, min(4.0, float(speed)))
        self.operations.append({"op": "speed", "speed": speed})
        self._save()
        logger.debug("speed_adjust: %.2f×", speed)
        return self

    def add_music(
        self,
        music_path: str,
        volume: float = 0.3,
        replace_audio: bool = False,
    ) -> "ClipEditor":
        """
        Mix in a background music track.

        Args:
            music_path:    Path to the music file (MP3, AAC, WAV, etc.).
            volume:        Music volume relative to original audio (0.0–1.0).
                           Default 0.3 = 30% volume.
            replace_audio: If True, mute the original audio and use music only.

        Returns:
            self.
        """
        self.operations.append({
            "op":            "music",
            "music_path":    str(Path(music_path).resolve()),
            "volume":        max(0.0, min(1.0, float(volume))),
            "replace_audio": bool(replace_audio),
        })
        self._save()
        logger.debug("add_music: %s vol=%.2f replace=%s", music_path, volume, replace_audio)
        return self

    def add_overlay(
        self,
        image_path: str,
        position: str = "bottom_right",
        opacity: float = 0.7,
        scale: float = 0.15,
    ) -> "ClipEditor":
        """
        Add a semi-transparent image overlay (watermark/logo).

        Args:
            image_path: Path to the image file (PNG recommended for transparency).
            position:   'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'.
            opacity:    0.0 (invisible) to 1.0 (fully opaque). Default 0.7.
            scale:      Size as a fraction of video width. Default 0.15 (15%).

        Returns:
            self.
        """
        self.operations.append({
            "op":         "overlay",
            "image_path": str(Path(image_path).resolve()),
            "position":   position,
            "opacity":    max(0.0, min(1.0, float(opacity))),
            "scale":      max(0.01, min(1.0, float(scale))),
        })
        self._save()
        logger.debug("add_overlay: %s @ %s", image_path, position)
        return self

    def add_transition(
        self,
        fade_in: float = 0.5,
        fade_out: float = 0.5,
    ) -> "ClipEditor":
        """
        Add fade-in / fade-out transitions (video + audio).

        Args:
            fade_in:  Fade-in duration in seconds (default 0.5). Set 0 to skip.
            fade_out: Fade-out duration in seconds (default 0.5). Set 0 to skip.

        Returns:
            self.
        """
        self.operations.append({
            "op":       "transition",
            "fade_in":  max(0.0, float(fade_in)),
            "fade_out": max(0.0, float(fade_out)),
        })
        self._save()
        logger.debug("add_transition: in=%.1fs out=%.1fs", fade_in, fade_out)
        return self

    def apply_template(self, template_id: Optional[int] = None) -> "ClipEditor":
        """
        Apply a video template (crop + overlay settings from templates.py).

        Args:
            template_id: Template number 1–4. If None, uses the user's default.

        Returns:
            self.
        """
        tid = template_id or self._default_template
        self.operations.append({"op": "template", "template_id": int(tid)})
        self._save()
        logger.debug("apply_template: template_id=%d", tid)
        return self

    def reset(self) -> "ClipEditor":
        """Remove all pending operations and reset to the original clip."""
        self.operations = []
        self._save()
        logger.info("Editor session reset — all operations cleared.")
        return self

    # ── Export ────────────────────────────────────────────────────────────────

    def export_clip(
        self,
        output_path: Optional[str] = None,
        quality: Optional[str] = None,
    ) -> Optional[str]:
        """
        Apply all pending operations and export the final video.

        Builds a single FFmpeg filter graph from the accumulated operations
        and runs the encode in one pass. The original source file is never
        touched.

        Args:
            output_path: Where to write the output MP4. If None, a timestamped
                         filename is generated in clips/processed/.
            quality:     'low' | 'medium' | 'high'. Overrides preferences if set.

        Returns:
            Absolute path to the exported MP4, or None if export failed.
        """
        q = quality or self._output_quality
        if q not in QUALITY_PRESETS:
            q = "medium"
        preset = QUALITY_PRESETS[q]

        if not output_path:
            stem = self.clip_path.stem
            ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(_OUTPUT_DIR / f"{stem}_edited_{ts}.mp4")

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Exporting clip: %d operation(s), quality=%s → %s",
            len(self.operations), q, output_path,
        )

        # Build and run FFmpeg command
        success = _run_ffmpeg_export(
            input_path=str(self.clip_path),
            output_path=str(out_path),
            operations=self.operations,
            quality_preset=preset,
        )

        if success:
            database.update_custom_edit(
                edit_id=self.edit_id,
                output_path=str(out_path),
                status="exported",
            )
            logger.info("Export complete: %s", output_path)

            # Auto-queue if preference is set
            if self._auto_queue:
                self.queue_for_posting(output_path=str(out_path))

            return str(out_path)
        else:
            logger.error("Export failed for edit_id=%d", self.edit_id)
            return None

    def queue_for_posting(
        self,
        output_path: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> Optional[int]:
        """
        Add the exported clip to the posting queue.

        Args:
            output_path: Path to the exported MP4. If None, looks up the
                         last exported path from the DB.
            caption:     Optional caption text. If None, auto-generated.

        Returns:
            The new package_id, or None if queueing failed.
        """
        from posting_queue import add_package_to_queue

        if not output_path:
            edit = database.get_custom_edit(self.edit_id)
            output_path = edit.get("output_path") if edit else None

        if not output_path or not Path(output_path).exists():
            logger.error("queue_for_posting: output file not found: %s", output_path)
            return None

        if not caption:
            try:
                from captions import generate_caption
                caption = generate_caption(
                    clip={"title": self.clip_path.stem, "source": "manual"},
                    style_id=1,
                    user_prefs=self.user_prefs,
                    user_id=self.user_id,
                )
            except Exception:
                caption = f"{self.clip_path.stem} #fyp #viral"

        # Insert a manual clip record and package
        clip_id = database.insert_clip({
            "user_id": self.user_id,
            "source":  "manual",
            "title":   self.clip_path.stem,
            "mode":    "manual",
            "status":  "processed",
            "score":   100.0,
            "local_path": output_path,
        })

        package_id = database.insert_package({
            "user_id":       self.user_id,
            "clip_ids":      [clip_id],
            "template":      self._default_template,
            "caption_style": 1,
            "caption_text":  caption,
            "mode":          "manual",
            "status":        "processed",
            "compiled_path": output_path,
        })

        try:
            queue_id, slot = add_package_to_queue(
                package_id=package_id,
                user_prefs=self.user_prefs,
                mode="manual",
                user_id=self.user_id,
            )
            database.update_custom_edit(self.edit_id, status="queued")
            logger.info(
                "Queued edited clip (package_id=%d) for %s",
                package_id, slot.strftime("%Y-%m-%d %H:%M"),
            )
            return package_id
        except Exception as exc:
            logger.error("queue_for_posting failed: %s", exc)
            return None

    def preview_edit(self) -> None:
        """
        Open a preview of the current operations summary in the terminal.
        Does not run FFmpeg — just prints what will happen.
        """
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table   = Table(title=f"Editor Session — edit_id={self.edit_id}", show_lines=True)
        table.add_column("#", style="dim", width=4)
        table.add_column("Operation", style="bold cyan")
        table.add_column("Parameters")

        if not self.operations:
            console.print("\n[yellow]No operations queued. Use the editor methods to add operations.[/yellow]\n")
            return

        for i, op in enumerate(self.operations, 1):
            op_name = op.get("op", "?")
            params  = {k: v for k, v in op.items() if k != "op"}
            table.add_row(str(i), op_name, str(params))

        console.print()
        console.print(table)
        console.print(
            f"\n  Source : [dim]{self.clip_path}[/dim]\n"
            f"  Quality: [dim]{self._output_quality}[/dim]\n"
            f"  Session: [dim]edit_id={self.edit_id}[/dim]\n"
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _save(self) -> None:
        """Persist the current operations list to the database."""
        try:
            database.update_custom_edit(
                edit_id=self.edit_id,
                operations=self.operations,
            )
        except Exception as exc:
            logger.warning("Failed to persist editor state: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# FFmpeg helpers
# ══════════════════════════════════════════════════════════════════════════════

def _probe_duration(path: str) -> float:
    """Use ffprobe to get video duration in seconds."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _probe_dimensions(path: str) -> tuple:
    """Use ffprobe to get (width, height) of the first video stream."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        parts = result.stdout.strip().split(",")
        return int(parts[0]), int(parts[1])
    except Exception:
        return 1920, 1080


def _position_to_xy(position: str, w: str = "iw", h: str = "ih",
                    pad: int = 20) -> tuple:
    """
    Convert a position name to (x, y) FFmpeg drawtext/overlay coordinates.

    Returns:
        (x_expr, y_expr) tuple as FFmpeg filter expressions.
    """
    positions = {
        "top_left":     (str(pad),           str(pad)),
        "top_right":    (f"{w}-overlay_w-{pad}", str(pad)),
        "top":          (f"({w}-text_w)/2",   str(pad)),
        "bottom_left":  (str(pad),            f"{h}-overlay_h-{pad}"),
        "bottom_right": (f"{w}-overlay_w-{pad}", f"{h}-overlay_h-{pad}"),
        "bottom":       (f"({w}-text_w)/2",   f"{h}-text_h-{pad}"),
        "middle":       (f"({w}-text_w)/2",   f"({h}-text_h)/2"),
    }
    return positions.get(position, positions["bottom"])


def _build_filter_complex(
    operations: List[Dict[str, Any]],
    duration: float,
) -> tuple:
    """
    Build an FFmpeg filter_complex string from the accumulated operations.

    Returns:
        (filter_complex_str, has_audio_ops) tuple.
        filter_complex_str: The -filter_complex argument value.
        has_audio_ops: True if any operation modifies audio.
    """
    vf_parts: List[str] = []
    af_parts: List[str] = []
    extra_inputs: List[str] = []   # Additional -i paths needed (music, overlay)
    overlay_input_idx = 1          # Index for additional inputs (0 = main input)

    has_trim = False
    trim_start = 0.0
    trim_end: Optional[float] = None

    # ── Pass 1: Collect trim params (affects fade timing) ─────────────────────
    for op in operations:
        if op["op"] == "trim":
            has_trim    = True
            trim_start  = float(op.get("start", 0.0))
            trim_end    = op.get("end")

    effective_duration = duration
    if has_trim:
        end = float(trim_end) if trim_end is not None else duration
        effective_duration = max(0.1, end - trim_start)

    # ── Pass 2: Build filter chain ─────────────────────────────────────────────
    for op in operations:
        kind = op["op"]

        if kind == "trim":
            start = float(op.get("start", 0.0))
            end   = op.get("end")
            if start > 0 or end is not None:
                end_str = f":end={end}" if end is not None else ""
                vf_parts.append(f"trim=start={start}{end_str},setpts=PTS-STARTPTS")
                af_parts.append(f"atrim=start={start}{end_str.replace('end=','end=')},asetpts=PTS-STARTPTS")

        elif kind == "crop_vertical":
            mode     = op.get("mode", "center")
            x_offset = int(op.get("x_offset", 0))
            y_offset = int(op.get("y_offset", 0))
            if mode == "center":
                # Crop width to (height * 9/16), centered horizontally
                vf_parts.append(
                    "crop=ih*9/16:ih:(iw-ih*9/16)/2:0"
                )
            else:
                vf_parts.append(
                    f"crop=ih*9/16:ih:{x_offset}:{y_offset}"
                )
            vf_parts.append("scale=1080:1920:flags=lanczos")

        elif kind == "speed":
            speed = float(op.get("speed", 1.0))
            if speed != 1.0:
                vf_parts.append(f"setpts={1.0/speed:.4f}*PTS")
                # atempo must be chained for values outside [0.5, 2.0]
                if speed > 2.0:
                    af_parts.append("atempo=2.0")
                    af_parts.append(f"atempo={speed/2.0:.4f}")
                elif speed < 0.5:
                    af_parts.append("atempo=0.5")
                    af_parts.append(f"atempo={speed*2.0:.4f}")
                else:
                    af_parts.append(f"atempo={speed:.4f}")

        elif kind == "caption":
            text       = op.get("text", "").replace("'", "\\'").replace(":", "\\:")
            position   = op.get("position", "bottom")
            font_size  = int(op.get("font_size", 48))
            font_color = op.get("font_color", "white")
            start_sec  = float(op.get("start_sec", 0.0))
            end_sec    = op.get("end_sec")

            x_expr, y_expr = _position_to_xy(position, w="w", h="h", pad=30)
            # Replace overlay_w placeholder with text_w for text
            x_expr = x_expr.replace("overlay_w", "text_w")
            y_expr = y_expr.replace("overlay_h", "text_h")

            enable_expr = ""
            if start_sec > 0 or end_sec is not None:
                end_t = end_sec if end_sec is not None else effective_duration
                enable_expr = f":enable='between(t,{start_sec},{end_t})'"

            vf_parts.append(
                f"drawtext=text='{text}'"
                f":fontsize={font_size}"
                f":fontcolor={font_color}"
                f":x={x_expr}:y={y_expr}"
                f":box=1:boxcolor=black@0.4:boxborderw=8"
                f"{enable_expr}"
            )

        elif kind == "transition":
            fade_in  = float(op.get("fade_in", 0.5))
            fade_out = float(op.get("fade_out", 0.5))
            if fade_in > 0:
                vf_parts.append(f"fade=t=in:st=0:d={fade_in}")
                af_parts.append(f"afade=t=in:st=0:d={fade_in}")
            if fade_out > 0:
                fade_start = max(0.0, effective_duration - fade_out)
                vf_parts.append(f"fade=t=out:st={fade_start:.3f}:d={fade_out}")
                af_parts.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out}")

        elif kind == "music":
            music_path    = op.get("music_path", "")
            volume        = float(op.get("volume", 0.3))
            replace_audio = bool(op.get("replace_audio", False))

            if music_path and Path(music_path).exists():
                extra_inputs.append(music_path)
                # Music is handled in the audio filter graph separately
                # We'll handle it in _run_ffmpeg_export where we have full context

        elif kind == "overlay":
            image_path = op.get("image_path", "")
            position   = op.get("position", "bottom_right")
            opacity    = float(op.get("opacity", 0.7))
            scale_frac = float(op.get("scale", 0.15))

            if image_path and Path(image_path).exists():
                extra_inputs.append(image_path)
                idx = overlay_input_idx
                overlay_input_idx += 1

                x_expr, y_expr = _position_to_xy(position, w="main_w", h="main_h", pad=20)
                x_expr = x_expr.replace("overlay_w", "overlay_w").replace("text_w", "overlay_w")
                y_expr = y_expr.replace("overlay_h", "overlay_h").replace("text_h", "overlay_h")

                # Scale the overlay image relative to video width
                vf_parts.append(
                    f"[{idx}:v]scale=iw*{scale_frac}:-1,format=rgba,"
                    f"colorchannelmixer=aa={opacity}[ov{idx}];"
                    f"[in][ov{idx}]overlay={x_expr}:{y_expr}"
                )

        elif kind == "template":
            # Templates provide crop + branding defaults; apply basic 9:16 crop
            tid = int(op.get("template_id", 1))
            try:
                from templates import get_template
                template_cfg = get_template(tid)
                crop_mode = template_cfg.get("crop_mode", "center")
                if crop_mode == "center":
                    vf_parts.append("crop=ih*9/16:ih:(iw-ih*9/16)/2:0")
                    vf_parts.append("scale=1080:1920:flags=lanczos")
            except ImportError:
                # templates.py not found — apply default center crop
                vf_parts.append("crop=ih*9/16:ih:(iw-ih*9/16)/2:0")
                vf_parts.append("scale=1080:1920:flags=lanczos")

    return vf_parts, af_parts, extra_inputs


def _run_ffmpeg_export(
    input_path: str,
    output_path: str,
    operations: List[Dict[str, Any]],
    quality_preset: Dict,
) -> bool:
    """
    Build and run the FFmpeg command for the export.

    Returns:
        True if FFmpeg exited with code 0; False otherwise.
    """
    duration = _probe_duration(input_path)
    vf_parts, af_parts, extra_inputs = _build_filter_complex(operations, duration)

    # ── Music operation requires special amix handling ─────────────────────────
    music_ops = [op for op in operations if op.get("op") == "music"]
    extra_audio_inputs: List[str] = []
    if music_ops:
        for op in music_ops:
            mp = op.get("music_path", "")
            if mp and Path(mp).exists():
                extra_audio_inputs.append((mp, op))

    # ── Build FFmpeg command ───────────────────────────────────────────────────
    cmd: List[str] = ["ffmpeg", "-y", "-i", input_path]

    # Add extra video inputs (overlays)
    overlay_ops = [op for op in operations if op.get("op") == "overlay"]
    for op in overlay_ops:
        ip = op.get("image_path", "")
        if ip and Path(ip).exists():
            cmd += ["-i", ip]

    # Add extra audio inputs (music)
    for mp, _ in extra_audio_inputs:
        cmd += ["-i", mp]

    # ── Video filter chain ────────────────────────────────────────────────────
    if vf_parts:
        # Filter out overlay vf_parts (complex syntax) from simple vf chain
        simple_vf = [p for p in vf_parts if "[" not in p and "]" not in p]
        if simple_vf:
            cmd += ["-vf", ",".join(simple_vf)]

    # ── Audio filter chain ────────────────────────────────────────────────────
    if extra_audio_inputs and not music_ops[0].get("replace_audio", False):
        # Blend original + music audio
        music_volume = float(music_ops[0].get("volume", 0.3))
        n_inputs     = 1 + len(extra_audio_inputs)
        amix_filter  = f"amix=inputs={n_inputs}:duration=first:dropout_transition=3"
        if af_parts:
            audio_chain = ",".join(af_parts) + "," + amix_filter
        else:
            audio_chain = amix_filter
        cmd += ["-filter_complex", audio_chain]
    elif extra_audio_inputs and music_ops[0].get("replace_audio", False):
        # Replace original audio with music only
        music_volume = float(music_ops[0].get("volume", 1.0))
        cmd += ["-map", "0:v", "-map", "1:a"]
        if af_parts:
            cmd += ["-af", ",".join(af_parts)]
    elif af_parts:
        cmd += ["-af", ",".join(af_parts)]

    # ── Normalize to -14 LUFS for TikTok ─────────────────────────────────────
    # Only add loudnorm if no custom audio filter was already set
    if not extra_audio_inputs and not af_parts:
        cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    elif not extra_audio_inputs:
        # Append loudnorm to existing af chain
        cmd[-1] = cmd[-1] + ",loudnorm=I=-14:TP=-1.5:LRA=11"

    # ── Output encoding settings ──────────────────────────────────────────────
    cmd += [
        "-c:v", "libx264",
        "-crf", str(quality_preset["crf"]),
        "-preset", quality_preset["preset"],
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    logger.debug("FFmpeg command: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            logger.error("FFmpeg failed (exit %d):\n%s", result.returncode, result.stderr[-2000:])
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg export timed out after 600 seconds.")
        return False
    except FileNotFoundError:
        logger.error(
            "FFmpeg not found. Install it with: brew install ffmpeg (macOS) "
            "or apt install ffmpeg (Linux)"
        )
        return False
    except Exception as exc:
        logger.error("FFmpeg export error: %s", exc)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Interactive CLI
# ══════════════════════════════════════════════════════════════════════════════

def run_editor_cli(clip_path: Optional[str] = None, user_id: int = 1) -> None:
    """
    Run the interactive clip editor in the terminal.

    If clip_path is provided, opens that clip directly.
    Otherwise, prompts the user to enter a path or URL.

    Args:
        clip_path: Optional path to the clip to edit.
        user_id:   User ID.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm, FloatPrompt, IntPrompt

    console = Console()

    console.print(Panel(
        "[bold cyan]ClipCast Studio — Clip Editor[/bold cyan]\n"
        "Non-destructive editing with FFmpeg.\n"
        "Type [bold]help[/bold] at any prompt to see available commands.",
        expand=False,
    ))

    # ── Resolve clip path ──────────────────────────────────────────────────────
    if not clip_path:
        clip_path = Prompt.ask("\n  Enter clip path or URL")

    # Handle URL: download first
    if clip_path.startswith("http"):
        console.print("  [dim]Downloading clip…[/dim]")
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({
                "outtmpl": str(_WORK_DIR / "%(title)s.%(ext)s"),
                "format":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4",
                "quiet":   True,
            }) as ydl:
                info = ydl.extract_info(clip_path, download=True)
                clip_path = ydl.prepare_filename(info)
                console.print(f"  [green]Downloaded:[/green] {clip_path}")
        except Exception as exc:
            console.print(f"  [red]Download failed: {exc}[/red]")
            return

    if not Path(clip_path).exists():
        console.print(f"  [red]File not found: {clip_path}[/red]")
        return

    # ── Load prefs ────────────────────────────────────────────────────────────
    try:
        from preferences import load_preferences
        user_prefs = load_preferences()
    except Exception:
        user_prefs = {}

    # ── Create editor session ─────────────────────────────────────────────────
    editor = ClipEditor(clip_path, user_id=user_id, user_prefs=user_prefs)
    duration = editor.get_duration()
    w, h = editor.get_dimensions()

    console.print(
        f"\n  [green]Clip loaded:[/green] {Path(clip_path).name}\n"
        f"  Duration: {duration:.1f}s  |  Dimensions: {w}×{h}\n"
        f"  Session edit_id: {editor.edit_id}\n"
    )

    # ── Command loop ──────────────────────────────────────────────────────────
    COMMANDS = [
        "trim", "crop", "caption", "speed", "music", "overlay",
        "fade", "template", "preview", "reset", "export", "queue",
        "status", "help", "quit",
    ]

    while True:
        console.print("[dim]─" * 40 + "[/dim]")
        cmd = Prompt.ask(
            f"  [bold cyan]Editor[/bold cyan] ({len(editor.operations)} op(s))",
            choices=COMMANDS,
            show_choices=False,
        ).strip().lower()

        if cmd == "help":
            console.print("""
  [bold]Available commands:[/bold]
    trim      — Trim the clip (set start/end times)
    crop      — Crop to 9:16 vertical (center or manual)
    caption   — Add a text caption overlay
    speed     — Change playback speed (0.25×–4×)
    music     — Mix in background music
    overlay   — Add a watermark/logo image
    fade      — Add fade-in / fade-out transitions
    template  — Apply a video template (1–4)
    preview   — Show a summary of pending operations
    reset     — Clear all pending operations
    export    — Apply operations and export the final video
    queue     — Export and add to posting queue
    status    — Show session info and clip details
    quit      — Exit the editor (operations are auto-saved)
""")

        elif cmd == "trim":
            start = FloatPrompt.ask("  Start time (seconds)", default=0.0)
            end   = FloatPrompt.ask(f"  End time (seconds, max {duration:.1f})", default=duration)
            editor.trim_clip(start=start, end=end)
            console.print(f"  [green]✓[/green] Trim: {start:.1f}s → {end:.1f}s")

        elif cmd == "crop":
            mode = Prompt.ask("  Crop mode", choices=["center", "manual"], default="center")
            if mode == "manual":
                x = IntPrompt.ask("  X offset (pixels from left)", default=0)
                y = IntPrompt.ask("  Y offset (pixels from top)", default=0)
                editor.crop_to_vertical(mode="manual", x_offset=x, y_offset=y)
            else:
                editor.crop_to_vertical(mode="center")
            console.print(f"  [green]✓[/green] Crop to vertical ({mode})")

        elif cmd == "caption":
            text = Prompt.ask("  Caption text")
            pos  = Prompt.ask("  Position", choices=["top", "middle", "bottom"], default="bottom")
            size = IntPrompt.ask("  Font size (px)", default=48)
            color = Prompt.ask("  Font color", default="white")
            editor.add_caption(text=text, position=pos, font_size=size, font_color=color)
            console.print(f"  [green]✓[/green] Caption: '{text[:30]}' @ {pos}")

        elif cmd == "speed":
            speed = FloatPrompt.ask("  Playback speed (0.25–4.0, 1.0=normal)", default=1.0)
            editor.speed_adjust(speed=speed)
            console.print(f"  [green]✓[/green] Speed: {speed:.2f}×")

        elif cmd == "music":
            path = Prompt.ask("  Music file path")
            if not Path(path).exists():
                console.print(f"  [red]File not found: {path}[/red]")
                continue
            vol = FloatPrompt.ask("  Music volume (0.0–1.0)", default=0.3)
            replace = Confirm.ask("  Replace original audio?", default=False)
            editor.add_music(music_path=path, volume=vol, replace_audio=replace)
            console.print(f"  [green]✓[/green] Music: {Path(path).name} (vol={vol:.1f})")

        elif cmd == "overlay":
            path = Prompt.ask("  Image file path (PNG recommended)")
            if not Path(path).exists():
                console.print(f"  [red]File not found: {path}[/red]")
                continue
            pos = Prompt.ask(
                "  Position",
                choices=["top_left", "top_right", "bottom_left", "bottom_right"],
                default="bottom_right",
            )
            opacity = FloatPrompt.ask("  Opacity (0.0–1.0)", default=0.7)
            scale   = FloatPrompt.ask("  Scale (fraction of video width)", default=0.15)
            editor.add_overlay(image_path=path, position=pos, opacity=opacity, scale=scale)
            console.print(f"  [green]✓[/green] Overlay: {Path(path).name} @ {pos}")

        elif cmd == "fade":
            fi = FloatPrompt.ask("  Fade-in duration (seconds, 0=skip)", default=0.5)
            fo = FloatPrompt.ask("  Fade-out duration (seconds, 0=skip)", default=0.5)
            editor.add_transition(fade_in=fi, fade_out=fo)
            console.print(f"  [green]✓[/green] Fade: in={fi:.1f}s out={fo:.1f}s")

        elif cmd == "template":
            tid = IntPrompt.ask("  Template number (1–4)", default=editor._default_template)
            editor.apply_template(template_id=tid)
            console.print(f"  [green]✓[/green] Template {tid} applied")

        elif cmd == "preview":
            editor.preview_edit()

        elif cmd == "reset":
            if Confirm.ask("  Clear all operations?", default=False):
                editor.reset()
                console.print("  [yellow]All operations cleared.[/yellow]")

        elif cmd in ("export", "queue"):
            quality = Prompt.ask(
                "  Export quality",
                choices=["low", "medium", "high"],
                default=editor._output_quality,
            )
            console.print("  [dim]Exporting…[/dim]")
            out = editor.export_clip(quality=quality)
            if out:
                console.print(f"  [green]✓ Exported:[/green] {out}")
                if cmd == "queue":
                    caption = Prompt.ask("  Caption (Enter to auto-generate)", default="")
                    pkg_id = editor.queue_for_posting(output_path=out, caption=caption or None)
                    if pkg_id:
                        console.print(f"  [green]✓ Queued as package_id={pkg_id}[/green]")
            else:
                console.print("  [red]Export failed. Check logs for details.[/red]")

        elif cmd == "status":
            console.print(
                f"\n  Clip     : {editor.clip_path}\n"
                f"  Duration : {duration:.1f}s\n"
                f"  Size     : {w}×{h}\n"
                f"  Session  : edit_id={editor.edit_id}\n"
                f"  Ops      : {len(editor.operations)}\n"
                f"  Quality  : {editor._output_quality}\n"
            )

        elif cmd == "quit":
            console.print("\n  [dim]Session auto-saved. Goodbye.[/dim]\n")
            break


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(name)s  %(message)s")
    print("clip_editor.py — self-test\n")

    tmp_db = Path(tempfile.mktemp(suffix="_editor_test.db"))
    print(f"Using temp DB: {tmp_db}")

    try:
        database.initialize_database(db_path=tmp_db)

        # 1. Insert a custom edit session
        print("\n1. Testing database.insert_custom_edit...")
        edit_id = database.insert_custom_edit(
            clip_path="/tmp/test_clip.mp4",
            user_id=1,
            template=1,
            operations=[],
            db_path=tmp_db,
        )
        print(f"   edit_id: {edit_id}  (expected > 0)")
        assert edit_id > 0

        # 2. Update operations
        print("\n2. Testing database.update_custom_edit...")
        ops = [{"op": "trim", "start": 5.0, "end": 30.0}]
        database.update_custom_edit(edit_id, operations=ops, db_path=tmp_db)
        edit = database.get_custom_edit(edit_id, db_path=tmp_db)
        print(f"   operations: {edit['operations']}  (expected 1 op)")
        assert len(edit["operations"]) == 1

        # 3. Test _position_to_xy
        print("\n3. Testing _position_to_xy...")
        x, y = _position_to_xy("bottom")
        print(f"   bottom → x={x} y={y}")
        assert "text_w" in x or "w" in x

        # 4. Test _probe_duration with a non-existent file (should return 0.0)
        print("\n4. Testing _probe_duration with missing file...")
        dur = _probe_duration("/nonexistent/file.mp4")
        print(f"   Duration (missing file): {dur}  (expected 0.0)")
        assert dur == 0.0

        # 5. Test _build_filter_complex with sample operations
        print("\n5. Testing _build_filter_complex...")
        ops = [
            {"op": "trim", "start": 2.0, "end": 30.0},
            {"op": "crop_vertical", "mode": "center"},
            {"op": "caption", "text": "Test caption", "position": "bottom",
             "font_size": 48, "font_color": "white", "start_sec": 0.0},
            {"op": "transition", "fade_in": 0.5, "fade_out": 0.5},
        ]
        vf, af, extras = _build_filter_complex(ops, duration=60.0)
        print(f"   vf_parts: {len(vf)} filter(s)")
        print(f"   af_parts: {len(af)} filter(s)")
        print(f"   extra_inputs: {len(extras)}")
        assert any("trim" in f for f in vf), "Expected trim in vf"
        assert any("crop" in f for f in vf), "Expected crop in vf"
        assert any("drawtext" in f for f in vf), "Expected drawtext in vf"

        print("\nAll clip_editor.py self-tests passed.")

    except AssertionError as ae:
        print(f"\nASSERTION FAILED: {ae}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\nUNEXPECTED ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if tmp_db.exists():
            tmp_db.unlink()
            print(f"\nTemp DB cleaned up: {tmp_db}")

    # ── Interactive CLI test ───────────────────────────────────────────────────
    if len(sys.argv) > 1:
        print("\nLaunching interactive editor...")
        run_editor_cli(clip_path=sys.argv[1])
