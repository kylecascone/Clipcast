"""
fetcher_manual.py
=================
Handles manual clip input from two sources:
  1. Files dropped into the clips/manual/ folder (watched continuously).
  2. A Twitch or YouTube URL provided via the CLI (--manual flag).

All manual clips are flagged with mode="manual" and assigned the
maximum priority score so they are always processed before automated clips.

SaaS Note:
    The folder watcher is currently per-process (one user). In multi-user
    mode, the watcher would be started per user with their specific watched
    folder path, and user_id would be passed through to all database writes.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
MANUAL_CLIPS_DIR = BASE_DIR / "clips" / "manual"

# ── Supported file formats for dropped files ───────────────────────────────────
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# ── URL patterns for Twitch clips and YouTube videos ──────────────────────────
_TWITCH_CLIP_PATTERN = re.compile(
    r"https?://(?:clips\.twitch\.tv/[\w-]+|www\.twitch\.tv/\w+/clip/[\w-]+)"
)
_YOUTUBE_VIDEO_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)[\w-]+|youtu\.be/[\w-]+)"
)

# Manual clips always get this score so they are always prioritized
MANUAL_DEFAULT_SCORE = 100.0


# ══════════════════════════════════════════════════════════════════════════════
# URL handling
# ══════════════════════════════════════════════════════════════════════════════

def classify_url(url: str) -> Optional[str]:
    """
    Determine whether a URL is a Twitch clip or YouTube video.

    Args:
        url: Raw URL string.

    Returns:
        "twitch", "youtube", or None if the URL is not recognized.
    """
    url = url.strip()
    if _TWITCH_CLIP_PATTERN.match(url):
        return "twitch"
    if _YOUTUBE_VIDEO_PATTERN.match(url):
        return "youtube"
    return None


def build_clip_from_url(
    url: str,
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
) -> Dict[str, Any]:
    """
    Build a manual clip dict from a raw URL (Twitch or YouTube).

    The clip is not downloaded yet — downloading happens in editor.py.
    This function just creates the metadata record for the database.

    Args:
        url: Twitch clip URL or YouTube video URL.
        user_prefs: User preferences dict. If None, loads from preferences.yaml.
        user_id: User ID for database record.

    Returns:
        Clip dict ready to be inserted into the database via database.insert_clip().

    Raises:
        ValueError: If the URL is not a recognized Twitch or YouTube URL.
    """
    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    source = classify_url(url)
    if source is None:
        raise ValueError(
            f"Unrecognized URL: '{url}'\n"
            "Supported formats:\n"
            "  Twitch clip:   https://clips.twitch.tv/...\n"
            "                 https://www.twitch.tv/username/clip/...\n"
            "  YouTube video: https://www.youtube.com/watch?v=...\n"
            "                 https://youtu.be/...\n"
            "                 https://www.youtube.com/shorts/..."
        )

    template = user_prefs.get("manual_mode_default_template", 1)
    caption_style = user_prefs.get("default_caption_style", 1)

    clip = {
        "user_id":       user_id,
        "source":        source,
        "title":         f"Manual clip from {url[:60]}",  # Updated after download
        "creator_name":  None,                            # Updated after download
        "url":           url.strip(),
        "local_path":    None,
        "duration":      None,
        "score":         MANUAL_DEFAULT_SCORE,
        "is_solo_worthy": True,
        "template_used": template,
        "caption_used":  caption_style,
        "mode":          "manual",
        "status":        "queued",
    }

    logger.info("Manual clip queued from URL: %s (source=%s)", url, source)
    return clip


def build_clip_from_file(
    file_path: Path,
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
) -> Dict[str, Any]:
    """
    Build a manual clip dict from a local video file.

    Args:
        file_path: Absolute path to the video file.
        user_prefs: User preferences dict. If None, loads from preferences.yaml.
        user_id: User ID for database record.

    Returns:
        Clip dict ready to be inserted into the database.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file extension is not supported.
    """
    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Manual clip file not found: {file_path}")

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format: '{file_path.suffix}'\n"
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    template = user_prefs.get("manual_mode_default_template", 1)
    caption_style = user_prefs.get("default_caption_style", 1)

    clip = {
        "user_id":        user_id,
        "source":         "manual",
        "title":          file_path.stem,   # Filename without extension as title
        "creator_name":   None,
        "url":            None,
        "local_path":     str(file_path),
        "duration":       None,             # Measured in editor.py
        "score":          MANUAL_DEFAULT_SCORE,
        "is_solo_worthy": True,
        "template_used":  template,
        "caption_used":   caption_style,
        "mode":           "manual",
        "status":         "queued",
    }

    logger.info("Manual clip queued from file: %s", file_path)
    return clip


# ══════════════════════════════════════════════════════════════════════════════
# Folder watcher
# ══════════════════════════════════════════════════════════════════════════════

class ManualClipHandler(FileSystemEventHandler):
    """
    Watchdog event handler for the clips/manual folder.

    When a new video file appears (created or moved in), it calls the
    provided callback function with a clip dict.

    Args:
        callback: Function called with (clip_dict, user_prefs) when a new clip
                  is detected. Typically this will add the clip to the database
                  and processing queue.
        user_prefs: User preferences dict.
        user_id: User ID for database records.
    """

    def __init__(
        self,
        callback: Callable[[Dict[str, Any]], None],
        user_prefs: Optional[Dict] = None,
        user_id: int = 1,
    ):
        super().__init__()
        self.callback = callback
        self.user_prefs = user_prefs
        self.user_id = user_id
        self._seen: set = set()  # Track files already processed this session

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle_file(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        """Handle files moved/renamed into the folder."""
        if not event.is_directory:
            self._handle_file(Path(event.dest_path))

    def _handle_file(self, file_path: Path) -> None:
        """Process a newly detected file."""
        # Ignore hidden files (e.g. .DS_Store) and temp files
        if file_path.name.startswith("."):
            return
        if file_path.name.endswith(".part"):
            return
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            logger.debug("Skipping non-video file: %s", file_path.name)
            return
        if str(file_path) in self._seen:
            return

        # Brief wait to make sure the file has finished copying/writing
        time.sleep(1)
        if not file_path.exists():
            return

        self._seen.add(str(file_path))
        logger.info("New manual clip detected: %s", file_path.name)

        try:
            clip = build_clip_from_file(
                file_path,
                user_prefs=self.user_prefs,
                user_id=self.user_id,
            )
            self.callback(clip)
        except (FileNotFoundError, ValueError) as e:
            logger.error("Could not process manual file '%s': %s", file_path.name, e)


def start_folder_watcher(
    callback: Callable[[Dict[str, Any]], None],
    watch_dir: Optional[Path] = None,
    user_prefs: Optional[Dict] = None,
    user_id: int = 1,
) -> Observer:
    """
    Start watching the clips/manual folder for new video files.

    This is non-blocking — it returns an Observer object. Call
    observer.stop() and observer.join() to stop watching.

    Args:
        callback: Called with a clip dict each time a new file is detected.
        watch_dir: Override the default MANUAL_CLIPS_DIR.
        user_prefs: User preferences (loaded from file if not provided).
        user_id: User ID for database records.

    Returns:
        Running watchdog Observer instance.
    """
    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    folder = watch_dir or MANUAL_CLIPS_DIR
    folder.mkdir(parents=True, exist_ok=True)

    handler = ManualClipHandler(
        callback=callback,
        user_prefs=user_prefs,
        user_id=user_id,
    )

    observer = Observer()
    observer.schedule(handler, str(folder), recursive=False)
    observer.start()

    logger.info("Watching for manual clips in: %s", folder)
    return observer


def stop_folder_watcher(observer: Observer) -> None:
    """
    Stop a running folder watcher.

    Args:
        observer: The Observer returned by start_folder_watcher().
    """
    observer.stop()
    observer.join()
    logger.info("Manual clip folder watcher stopped.")


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing fetcher_manual.py...")
    print()

    # Test URL classification
    test_urls = [
        "https://clips.twitch.tv/AbcdEfghIjklMnop",
        "https://www.twitch.tv/shroud/clip/AbcdEfgh",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc123",
        "https://www.notavalidurl.com/video/123",
    ]

    print("URL classification test:")
    for url in test_urls:
        result = classify_url(url)
        status = f"[{result}]" if result else "[NOT RECOGNIZED]"
        print(f"  {status:12}  {url}")

    print()

    # Test building a clip from a URL (no download, just metadata)
    print("Building clip dict from Twitch URL...")
    clip = build_clip_from_url("https://clips.twitch.tv/TestClip12345")
    print(f"  source:       {clip['source']}")
    print(f"  mode:         {clip['mode']}")
    print(f"  score:        {clip['score']}")
    print(f"  template:     {clip['template_used']}")
    print(f"  status:       {clip['status']}")
    print()

    # Test building from a file
    test_file = MANUAL_CLIPS_DIR / "test_clip.mp4"
    MANUAL_CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    test_file.touch()  # Create an empty test file

    print("Building clip dict from local file...")
    clip_file = build_clip_from_file(test_file)
    print(f"  title:        {clip_file['title']}")
    print(f"  local_path:   {clip_file['local_path']}")
    print(f"  mode:         {clip_file['mode']}")
    print(f"  score:        {clip_file['score']}")
    test_file.unlink()  # Clean up
    print()

    # Test the folder watcher (runs for 10 seconds then stops)
    print("Starting folder watcher on clips/manual/ for 10 seconds...")
    print(f"Try dropping a .mp4 file into:  {MANUAL_CLIPS_DIR}")
    print()

    def on_new_clip(clip_dict: Dict) -> None:
        print(f"\n  ✓ New clip detected!")
        print(f"    title: {clip_dict['title']}")
        print(f"    mode:  {clip_dict['mode']}")
        print(f"    score: {clip_dict['score']}")

    observer = start_folder_watcher(callback=on_new_clip)

    try:
        for i in range(10, 0, -1):
            print(f"\r  Watching... {i}s remaining ", end="", flush=True)
            time.sleep(1)
    finally:
        stop_folder_watcher(observer)

    print("\n\nManual fetcher test complete.")
