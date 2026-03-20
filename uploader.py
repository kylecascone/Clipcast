"""
uploader.py
===========
Uploads compiled video packages to TikTok, YouTube Shorts, and Instagram Reels.

Auth flow:
  - TikTok uses OAuth 2.0. You must complete the initial auth flow once
    (which ClipCast will guide you through when you first run --run).
  - After that, tokens are stored in config.yaml and refreshed automatically.
  - Access tokens expire in 24 hours; refresh tokens expire in 365 days.

API used:
  - TikTok Content Posting API v2 (direct post)
  - https://developers.tiktok.com/doc/content-posting-api-reference-direct-post

SaaS Note:
    All functions accept user_config for multi-user support. Each user
    would have their own TikTok OAuth tokens stored per-user in the database.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
import yaml

logger = logging.getLogger(__name__)

# ── TikTok API endpoints ───────────────────────────────────────────────────────
_TIKTOK_AUTH_URL    = "https://open.tiktokapis.com/v2/oauth/token/"
_TIKTOK_INIT_URL    = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_TIKTOK_UPLOAD_URL  = "https://open.tiktokapis.com/v2/post/publish/video/upload/"
_TIKTOK_STATUS_URL  = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
_TIKTOK_AUTH_SCOPES = "user.info.basic,video.upload,video.publish"

# Upload polling settings
_UPLOAD_POLL_INTERVAL  = 5    # seconds between status checks
_UPLOAD_POLL_MAX_TRIES = 60   # max checks before timing out (~5 minutes)


# ══════════════════════════════════════════════════════════════════════════════
# Token management
# ══════════════════════════════════════════════════════════════════════════════

def get_auth_url(client_key: str, redirect_uri: str, state: str = "clipcast") -> str:
    """
    Generate the TikTok OAuth authorization URL for first-time setup.

    The user must open this URL in a browser, log in with TikTok, grant
    permissions, and paste the redirect URL back into ClipCast.

    Args:
        client_key: TikTok app client key from config.yaml.
        redirect_uri: The redirect URI registered in your TikTok app.
        state: CSRF token (any string is fine for single-user).

    Returns:
        Full authorization URL string.
    """
    params = {
        "client_key":     client_key,
        "response_type":  "code",
        "scope":          _TIKTOK_AUTH_SCOPES,
        "redirect_uri":   redirect_uri,
        "state":          state,
    }
    param_str = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://www.tiktok.com/v2/auth/authorize/?{param_str}"


def exchange_code_for_tokens(
    code: str,
    client_key: str,
    client_secret: str,
    redirect_uri: str,
) -> Dict[str, str]:
    """
    Exchange an authorization code for access and refresh tokens.

    Args:
        code: The code parameter from the TikTok redirect URL.
        client_key: TikTok app client key.
        client_secret: TikTok app client secret.
        redirect_uri: Must match the URI used in the auth URL.

    Returns:
        Dict with 'access_token', 'refresh_token', 'expires_in'.

    Raises:
        RuntimeError: If the token exchange fails.
    """
    resp = requests.post(
        _TIKTOK_AUTH_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key":     client_key,
            "client_secret":  client_secret,
            "code":           code,
            "grant_type":     "authorization_code",
            "redirect_uri":   redirect_uri,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"TikTok token exchange failed ({resp.status_code}): {resp.text}"
        )

    data = resp.json()
    if data.get("error"):
        raise RuntimeError(
            f"TikTok token exchange error: {data.get('error_description', data.get('error'))}"
        )

    return {
        "access_token":    data["access_token"],
        "refresh_token":   data["refresh_token"],
        "expires_in":      str(data.get("expires_in", 86400)),
    }


def refresh_access_token(
    refresh_token: str,
    client_key: str,
    client_secret: str,
) -> Dict[str, str]:
    """
    Refresh an expired access token using the stored refresh token.

    Args:
        refresh_token: The current refresh token.
        client_key: TikTok client key.
        client_secret: TikTok client secret.

    Returns:
        Dict with new 'access_token', 'refresh_token', 'expires_in'.

    Raises:
        RuntimeError: If the refresh fails (e.g. refresh token expired).
    """
    resp = requests.post(
        _TIKTOK_AUTH_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key":     client_key,
            "client_secret":  client_secret,
            "grant_type":     "refresh_token",
            "refresh_token":  refresh_token,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"TikTok token refresh failed ({resp.status_code}): {resp.text}"
        )

    data = resp.json()
    if data.get("error"):
        raise RuntimeError(
            f"TikTok token refresh error: {data.get('error_description', data.get('error'))}"
        )

    return {
        "access_token":  data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_in":    str(data.get("expires_in", 86400)),
    }


def get_valid_access_token(
    user_config: Dict[str, Any],
    config_file: Optional[Path] = None,
) -> str:
    """
    Return a valid TikTok access token, refreshing if necessary.

    Checks whether the stored token is expired. If so, refreshes it
    and saves the new tokens back to config.yaml.

    Args:
        user_config: The loaded config dict (with tiktok credentials).
        config_file: Path to config.yaml (for saving updated tokens).

    Returns:
        A valid Bearer access token string.

    Raises:
        RuntimeError: If no tokens are configured or refresh fails.
    """
    from preferences import BASE_DIR
    cfg_path = config_file or (BASE_DIR / "config.yaml")

    tiktok_cfg = user_config.get("tiktok", {})
    access_token  = tiktok_cfg.get("access_token", "")
    refresh_token = tiktok_cfg.get("refresh_token", "")
    expires_at    = tiktok_cfg.get("token_expires_at", "")
    client_key    = tiktok_cfg.get("client_key", "")
    client_secret = tiktok_cfg.get("client_secret", "")

    if not access_token:
        raise RuntimeError(
            "TikTok access token not configured.\n"
            "Run the TikTok auth flow first:\n"
            "  python main.py --run  (will prompt you to authenticate)"
        )

    # Check if token is expired (with 5-minute buffer)
    is_expired = False
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if exp_dt <= now + timedelta(minutes=5):
                is_expired = True
        except (ValueError, TypeError):
            is_expired = True  # Can't parse — try refresh

    if is_expired:
        logger.info("TikTok access token expired. Refreshing...")
        if not refresh_token:
            raise RuntimeError(
                "TikTok access token expired and no refresh token is stored.\n"
                "You need to re-authenticate. Run:  python main.py --run"
            )
        new_tokens = refresh_access_token(refresh_token, client_key, client_secret)
        access_token = new_tokens["access_token"]

        # Save updated tokens to config.yaml
        user_config["tiktok"]["access_token"]   = access_token
        user_config["tiktok"]["refresh_token"]  = new_tokens["refresh_token"]
        expires_seconds = int(new_tokens.get("expires_in", 86400))
        from datetime import timedelta
        exp = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
        user_config["tiktok"]["token_expires_at"] = exp.isoformat()

        with open(cfg_path, "w") as f:
            yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)

        logger.info("TikTok tokens refreshed and saved.")

    return access_token


# ══════════════════════════════════════════════════════════════════════════════
# Upload
# ══════════════════════════════════════════════════════════════════════════════

def upload_package(
    package: Dict[str, Any],
    user_config: Optional[Dict] = None,
    user_prefs: Optional[Dict] = None,
    test_mode: bool = False,
) -> Dict[str, Optional[str]]:
    """
    Upload a compiled video package to all configured target platforms.

    Reads target_platforms from user_prefs and uploads to each enabled platform
    (tiktok, youtube_shorts, instagram_reels). Platforms with missing or
    unconfigured credentials are skipped with a warning rather than failing.

    Args:
        package:     Package dict with 'compiled_path' and 'caption_text'.
        user_config: Config dict with API credentials. Loaded from config.yaml
                     if None.
        user_prefs:  User preferences. Loaded from preferences.yaml if None.
        test_mode:   If True, skip actual uploads and return fake post IDs.

    Returns:
        Dict mapping platform name → post_id (str) or None on failure.
        e.g. {"tiktok": "7123...", "youtube_shorts": None, "instagram_reels": None}
    """
    if user_config is None:
        from preferences import load_config
        user_config = load_config()
    if user_prefs is None:
        from preferences import load_preferences
        user_prefs = load_preferences()

    compiled_path = package.get("compiled_path")
    if not compiled_path or not Path(compiled_path).exists():
        logger.error("Upload failed: compiled_path not found at '%s'", compiled_path)
        return {}

    caption = package.get("caption_text") or ""
    target_platforms = user_prefs.get("target_platforms", ["tiktok"])

    results: Dict[str, Optional[str]] = {}

    for platform in target_platforms:
        if test_mode:
            fake_id = f"test_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logger.info("[TEST MODE] Skipping %s upload → %s", platform, fake_id)
            results[platform] = fake_id
            continue

        if platform == "tiktok":
            results["tiktok"] = _upload_tiktok(
                Path(compiled_path), caption, user_config,
            )

        elif platform == "youtube_shorts":
            # Enrich package with shared_clip data so post_package can pull
            # hashtags and thumbnail from the DB.
            _clip_ctx: Dict[str, Any] = {"title": caption or "Viral Clip 🔥"}
            _clip_ids = package.get("clip_ids") or []
            if _clip_ids:
                try:
                    from database import get_connection as _gc
                    _conn = _gc()
                    _clip_row = _conn.execute(
                        "SELECT url, title FROM clips WHERE clip_id = ? LIMIT 1",
                        (_clip_ids[0],),
                    ).fetchone()
                    if _clip_row:
                        _sc_row = _conn.execute(
                            "SELECT shared_clip_id, title, creator_name, "
                            "hashtags_youtube, hashtags_tiktok, thumbnail_path "
                            "FROM shared_clips WHERE url = ? LIMIT 1",
                            (_clip_row[0],),
                        ).fetchone()
                        if _sc_row:
                            _clip_ctx = dict(_sc_row)
                    _conn.close()
                except Exception as _exc:
                    logger.warning("youtube_shorts clip lookup failed: %s", _exc)
            _pkg_for_yt = dict(package, clip=_clip_ctx)
            yt_result = post_package(_pkg_for_yt, compiled_path, platform="youtube_shorts")
            results["youtube_shorts"] = yt_result.get("video_id") if yt_result.get("success") else None

        elif platform == "instagram_reels":
            results["instagram_reels"] = _upload_instagram_reels(
                Path(compiled_path), caption, user_config,
            )

        else:
            logger.warning("Unknown platform '%s' in target_platforms — skipped.", platform)

    return results


def _upload_tiktok(
    video_path: Path,
    caption: str,
    user_config: Dict,
) -> Optional[str]:
    """Upload to TikTok. Returns publish_id or None."""
    tiktok_cfg = user_config.get("tiktok", {})
    if tiktok_cfg.get("client_key") == "YOUR_TIKTOK_CLIENT_KEY_HERE":
        logger.error("TikTok credentials not configured in config.yaml.")
        return None
    try:
        access_token = get_valid_access_token(user_config)
        publish_id = _direct_post_video(
            video_path=video_path,
            caption=caption,
            access_token=access_token,
        )
        logger.info("TikTok upload successful. publish_id=%s", publish_id)
        return publish_id
    except RuntimeError as e:
        logger.error("TikTok upload failed: %s", e)
        return None
    except Exception as e:
        logger.exception("Unexpected TikTok upload error: %s", e)
        return None


def _direct_post_video(
    video_path: Path,
    caption: str,
    access_token: str,
) -> str:
    """
    Upload a video to TikTok using the Direct Post flow.

    Steps:
      1. Initialize the upload to get an upload_url.
      2. PUT the video file to the upload_url.
      3. Poll the status endpoint until processing completes.

    Args:
        video_path: Path to the final MP4 file.
        caption: TikTok post caption / description.
        access_token: Valid TikTok Bearer token.

    Returns:
        TikTok publish_id string.

    Raises:
        RuntimeError: On any API error.
    """
    file_size = video_path.stat().st_size
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    # ── Step 1: Initialize upload ──────────────────────────────────────────────
    logger.info("Initializing TikTok upload for '%s' (%d bytes)...", video_path.name, file_size)
    init_resp = requests.post(
        _TIKTOK_INIT_URL,
        headers=headers,
        json={
            "post_info": {
                "title":           caption[:2200],  # TikTok caption limit
                "privacy_level":   "SELF_ONLY",      # Use SELF_ONLY for safety;
                                                     # change to PUBLIC_TO_EVERYONE to go live
                "disable_duet":    False,
                "disable_comment": False,
                "disable_stitch":  False,
            },
            "source_info": {
                "source":          "FILE_UPLOAD",
                "video_size":      file_size,
                "chunk_size":      file_size,   # Single-chunk upload (files up to 128MB)
                "total_chunk_count": 1,
            },
        },
        timeout=30,
    )

    if init_resp.status_code != 200:
        raise RuntimeError(
            f"TikTok init failed ({init_resp.status_code}): {init_resp.text}"
        )

    init_data  = init_resp.json().get("data", {})
    publish_id = init_data.get("publish_id")
    upload_url = init_data.get("upload_url")

    if not publish_id or not upload_url:
        raise RuntimeError(f"TikTok init response missing publish_id or upload_url: {init_resp.text}")

    # ── Step 2: Upload the video file ──────────────────────────────────────────
    logger.info("Uploading video to TikTok... (publish_id=%s)", publish_id)
    with open(video_path, "rb") as f:
        video_data = f.read()

    upload_resp = requests.put(
        upload_url,
        headers={
            "Content-Type":           "video/mp4",
            "Content-Range":          f"bytes 0-{file_size - 1}/{file_size}",
            "Content-Length":         str(file_size),
        },
        data=video_data,
        timeout=300,   # 5-minute timeout for large files
    )

    if upload_resp.status_code not in (200, 201, 206):
        raise RuntimeError(
            f"TikTok video upload failed ({upload_resp.status_code}): {upload_resp.text}"
        )

    # ── Step 3: Poll for processing completion ─────────────────────────────────
    logger.info("Video uploaded. Waiting for TikTok to process...")
    _poll_upload_status(publish_id, access_token)

    return publish_id


def _poll_upload_status(publish_id: str, access_token: str) -> None:
    """
    Poll TikTok's status endpoint until the video is published or fails.

    Raises:
        RuntimeError: If the video fails processing or polling times out.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    for attempt in range(_UPLOAD_POLL_MAX_TRIES):
        time.sleep(_UPLOAD_POLL_INTERVAL)

        resp = requests.post(
            _TIKTOK_STATUS_URL,
            headers=headers,
            json={"publish_id": publish_id},
            timeout=15,
        )

        if resp.status_code != 200:
            logger.warning("Status check failed (%d): %s", resp.status_code, resp.text[:200])
            continue

        status_data = resp.json().get("data", {})
        status      = status_data.get("status", "")

        logger.debug("TikTok processing status: %s (attempt %d)", status, attempt + 1)

        if status == "PUBLISH_COMPLETE":
            logger.info("TikTok video published successfully.")
            return
        elif status in ("FAILED", "PUBLISH_FAILED"):
            fail_reason = status_data.get("fail_reason", "Unknown error")
            raise RuntimeError(f"TikTok video processing failed: {fail_reason}")
        # Other statuses: PROCESSING_UPLOAD, PROCESSING_DOWNLOAD, SENDING_TO_USER_INBOX
        # These are in-progress — keep polling

    raise RuntimeError(
        f"TikTok upload timed out after {_UPLOAD_POLL_MAX_TRIES * _UPLOAD_POLL_INTERVAL}s. "
        f"publish_id={publish_id}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# YouTube Shorts uploader
# ══════════════════════════════════════════════════════════════════════════════

_YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_YT_TOKEN_URL  = "https://oauth2.googleapis.com/token"


def _upload_youtube_shorts(
    video_path: Path,
    caption: str,
    user_config: Dict,
) -> Optional[str]:
    """
    Upload a video to YouTube as a Short using the YouTube Data API v3
    resumable upload endpoint.

    Requires config.yaml to have:
        youtube:
          upload_client_id:     YOUR_YT_CLIENT_ID
          upload_client_secret: YOUR_YT_CLIENT_SECRET
          upload_access_token:  ...
          upload_refresh_token: ...

    Returns:
        YouTube video ID (str) on success, or None on failure.
    """
    yt_cfg = user_config.get("youtube", {})
    client_id     = yt_cfg.get("upload_client_id", "")
    client_secret = yt_cfg.get("upload_client_secret", "")
    access_token  = yt_cfg.get("upload_access_token", "")
    refresh_token = yt_cfg.get("upload_refresh_token", "")

    if not access_token and not refresh_token:
        logger.warning(
            "YouTube Shorts credentials not configured "
            "(youtube.upload_access_token / upload_refresh_token in config.yaml). "
            "Skipping YouTube Shorts upload."
        )
        return None

    # Refresh access token if needed
    if refresh_token and not access_token:
        access_token = _refresh_yt_token(client_id, client_secret, refresh_token)
        if not access_token:
            return None

    # Add #shorts to caption/description to ensure YouTube treats it as a Short
    shorts_title = (caption[:97] + "...") if len(caption) > 100 else caption
    shorts_desc  = f"{caption}\n\n#shorts"

    file_size = video_path.stat().st_size

    try:
        # Step 1: Initiate resumable upload
        init_resp = requests.post(
            _YT_UPLOAD_URL,
            headers={
                "Authorization":  f"Bearer {access_token}",
                "Content-Type":   "application/json; charset=UTF-8",
                "X-Upload-Content-Type":   "video/mp4",
                "X-Upload-Content-Length": str(file_size),
            },
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
            },
            json={
                "snippet": {
                    "title":       shorts_title,
                    "description": shorts_desc,
                    "categoryId":  "20",   # Gaming
                    "tags":        ["shorts", "gaming", "clips"],
                },
                "status": {
                    "privacyStatus": "public",
                },
            },
            timeout=30,
        )

        if init_resp.status_code not in (200, 201):
            logger.error(
                "YouTube Shorts init failed (%d): %s",
                init_resp.status_code, init_resp.text[:300],
            )
            return None

        upload_url = init_resp.headers.get("Location")
        if not upload_url:
            logger.error("YouTube Shorts: no upload URL in response headers.")
            return None

        # Step 2: Upload the file
        with open(video_path, "rb") as f:
            video_data = f.read()

        upload_resp = requests.put(
            upload_url,
            headers={
                "Content-Type":   "video/mp4",
                "Content-Length": str(file_size),
            },
            data=video_data,
            timeout=300,
        )

        if upload_resp.status_code not in (200, 201):
            logger.error(
                "YouTube Shorts upload failed (%d): %s",
                upload_resp.status_code, upload_resp.text[:300],
            )
            return None

        video_id = upload_resp.json().get("id")
        logger.info("YouTube Shorts upload successful. video_id=%s", video_id)
        return video_id

    except requests.RequestException as e:
        logger.error("YouTube Shorts upload request error: %s", e)
        return None


def _refresh_yt_token(client_id: str, client_secret: str, refresh_token: str) -> Optional[str]:
    """Refresh a YouTube OAuth2 access token. Returns new access_token or None."""
    try:
        resp = requests.post(
            _YT_TOKEN_URL,
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except requests.RequestException as e:
        logger.error("YouTube token refresh failed: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Instagram Reels uploader
# ══════════════════════════════════════════════════════════════════════════════

_IG_GRAPH_BASE = "https://graph.facebook.com/v21.0"


def _upload_instagram_reels(
    video_path: Path,
    caption: str,
    user_config: Dict,
) -> Optional[str]:
    """
    Upload a video to Instagram as a Reel using the Instagram Graph API.

    Requires config.yaml to have:
        instagram:
          account_id:   YOUR_IG_ACCOUNT_ID
          access_token: YOUR_LONG_LIVED_TOKEN

    The video must be publicly accessible via a URL for the container creation
    step. This implementation uploads the file to a temporary public URL using
    a simple multipart POST to the Instagram resumable upload endpoint, if
    available. Otherwise it falls back to requiring the video already be at a
    public URL (stored in package['video_url']).

    Returns:
        Instagram media ID (str) on success, or None on failure.
    """
    ig_cfg       = user_config.get("instagram", {})
    account_id   = ig_cfg.get("account_id", "")
    access_token = ig_cfg.get("access_token", "")

    if not account_id or not access_token:
        logger.warning(
            "Instagram credentials not configured "
            "(instagram.account_id / instagram.access_token in config.yaml). "
            "Skipping Instagram Reels upload."
        )
        return None

    # Instagram Graph API requires the video to be at a public URL.
    # Use the resumable upload endpoint to upload the file directly.
    try:
        # Step 1: Create an upload session
        session_resp = requests.post(
            f"{_IG_GRAPH_BASE}/{account_id}/uploads",
            params={
                "upload_type":    "resumable",
                "file_name":      video_path.name,
                "file_length":    str(video_path.stat().st_size),
                "file_type":      "video/mp4",
                "access_token":   access_token,
            },
            timeout=30,
        )

        if session_resp.status_code not in (200, 201):
            logger.error(
                "Instagram upload session failed (%d): %s",
                session_resp.status_code, session_resp.text[:300],
            )
            return None

        upload_id = session_resp.json().get("id")

        # Step 2: Upload the video bytes
        with open(video_path, "rb") as f:
            upload_resp = requests.post(
                f"https://rupload.facebook.com/video-upload/v21.0/{upload_id}",
                headers={
                    "Authorization":    f"OAuth {access_token}",
                    "offset":           "0",
                    "file_size":        str(video_path.stat().st_size),
                    "Content-Type":     "video/mp4",
                },
                data=f,
                timeout=300,
            )

        if not upload_resp.json().get("success"):
            logger.error(
                "Instagram video upload failed: %s", upload_resp.text[:300],
            )
            return None

        video_fb_url = f"upload:{upload_id}"

        # Step 3: Create media container
        container_resp = requests.post(
            f"{_IG_GRAPH_BASE}/{account_id}/media",
            params={
                "media_type":   "REELS",
                "video_url":    video_fb_url,
                "caption":      caption[:2200],
                "access_token": access_token,
            },
            timeout=30,
        )

        if container_resp.status_code not in (200, 201):
            logger.error(
                "Instagram container creation failed (%d): %s",
                container_resp.status_code, container_resp.text[:300],
            )
            return None

        container_id = container_resp.json().get("id")
        if not container_id:
            logger.error("Instagram: no container ID in response.")
            return None

        # Step 4: Poll until the container is ready
        for _ in range(30):
            time.sleep(5)
            status_resp = requests.get(
                f"{_IG_GRAPH_BASE}/{container_id}",
                params={
                    "fields":       "status_code",
                    "access_token": access_token,
                },
                timeout=15,
            )
            status = status_resp.json().get("status_code", "")
            if status == "FINISHED":
                break
            if status == "ERROR":
                logger.error("Instagram container processing failed.")
                return None

        # Step 5: Publish the container
        publish_resp = requests.post(
            f"{_IG_GRAPH_BASE}/{account_id}/media_publish",
            params={
                "creation_id":  container_id,
                "access_token": access_token,
            },
            timeout=30,
        )

        if publish_resp.status_code not in (200, 201):
            logger.error(
                "Instagram publish failed (%d): %s",
                publish_resp.status_code, publish_resp.text[:300],
            )
            return None

        media_id = publish_resp.json().get("id")
        logger.info("Instagram Reels upload successful. media_id=%s", media_id)
        return media_id

    except requests.RequestException as e:
        logger.error("Instagram Reels upload request error: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# High-level YouTube Shorts helper (uses youtube_uploader.py / Google API)
# ══════════════════════════════════════════════════════════════════════════════

def post_package(package: dict, video_path: str, platform: str = "youtube") -> dict:
    """
    Upload a processed package to the specified platform.

    Retrieves hashtags and thumbnail from shared_clips DB, then delegates
    to upload_to_youtube_shorts for YouTube. Returns a result dict with
    'success', 'video_id', 'url', and optionally 'error'.
    """
    from database import get_connection

    if not Path(video_path).exists():
        return {"success": False, "error": "Video file not found"}

    clip       = package.get("clip", {}) or {}
    # Also try clips list (automated pipeline stores clips differently)
    if not clip and package.get("clips"):
        clip = package["clips"][0] if package["clips"] else {}

    title      = clip.get("viral_title") or clip.get("title") or "Viral Clip 🔥"
    creator    = clip.get("creator_name", "") or ""
    shared_id  = clip.get("shared_clip_id")

    hashtags: list = []
    thumbnail_path: Optional[str] = None

    if shared_id:
        try:
            conn = get_connection()
            row  = conn.execute(
                "SELECT hashtags_youtube, hashtags_tiktok, thumbnail_path "
                "FROM shared_clips WHERE shared_clip_id = ?",
                (shared_id,),
            ).fetchone()
            conn.close()
            if row:
                ht_str     = row[0] or row[1] or ""
                hashtags   = [h.strip() for h in ht_str.split() if h.startswith("#")]
                thumbnail_path = row[2]
        except Exception as exc:
            logger.warning("post_package: DB lookup failed: %s", exc)

    if not hashtags:
        hashtags = ["viral", "fyp", "gaming", "twitch", "Shorts"]

    if platform in ("youtube", "youtube_shorts", "both"):
        try:
            from youtube_uploader import upload_to_youtube_shorts
        except ImportError as e:
            return {"success": False, "error": f"youtube_uploader not available: {e}"}

        description = "Follow for more viral clips!"
        if creator:
            description += f"\n\nOriginal creator: {creator}"

        result = upload_to_youtube_shorts(
            video_path=video_path,
            title=title,
            description=description,
            hashtags=hashtags,
            thumbnail_path=thumbnail_path,
        )

        if result.get("success"):
            try:
                conn = get_connection()
                conn.execute(
                    'UPDATE posting_queue SET status = "posted", posted_at = datetime("now") '
                    "WHERE package_id = ?",
                    (package.get("package_id"),),
                )
                conn.commit()
                conn.close()
            except Exception as exc:
                logger.warning("post_package: DB status update failed: %s", exc)
            print(f"Posted to YouTube: {result['url']}")

        return result

    return {"success": False, "error": f"Platform '{platform}' not supported yet"}


def post_to_youtube(package: dict, video_path: str) -> dict:
    """
    Upload a package to YouTube Shorts using the official Google API client.

    Falls back to _upload_youtube_shorts (raw HTTP) if youtube_uploader
    is unavailable.

    Returns:
        dict with 'success' bool and either 'video_id'+'url' or 'error'.
    """
    from database import get_connection

    clip        = package.get("clip", {}) or {}
    title       = clip.get("viral_title") or clip.get("title") or "Viral Clip"
    creator     = clip.get("creator_name", "") or ""
    shared_id   = clip.get("shared_clip_id")

    hashtags: list = []
    thumbnail_path: Optional[str] = None

    if shared_id:
        try:
            conn = get_connection()
            row  = conn.execute(
                "SELECT hashtags_youtube, thumbnail_path FROM shared_clips WHERE shared_clip_id = ?",
                (shared_id,),
            ).fetchone()
            conn.close()
            if row:
                ht_str = row[0] or ""
                hashtags = [h.strip() for h in ht_str.split() if h.startswith("#")]
                thumbnail_path = row[1]
        except Exception as exc:
            logger.warning("post_to_youtube: DB lookup failed: %s", exc)

    try:
        from youtube_uploader import upload_to_youtube_shorts
        description = f"Follow for more viral clips!"
        if creator:
            description += f"\n\nOriginal creator: {creator}"
        return upload_to_youtube_shorts(
            video_path=video_path,
            title=title,
            description=description,
            hashtags=hashtags,
            thumbnail_path=thumbnail_path,
        )
    except ImportError:
        logger.warning("youtube_uploader not available — falling back to raw HTTP upload")
        from preferences import load_config
        cfg = load_config()
        vid_id = _upload_youtube_shorts(Path(video_path), title, cfg)
        if vid_id:
            return {"success": True, "video_id": vid_id, "url": f"https://youtube.com/shorts/{vid_id}"}
        return {"success": False, "error": "YouTube upload failed"}


# ── Missing import fix ─────────────────────────────────────────────────────────
from datetime import timedelta


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing uploader.py...")
    print()

    try:
        from preferences import load_config
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    tiktok = config.get("tiktok", {})

    if tiktok.get("client_key") == "YOUR_TIKTOK_CLIENT_KEY_HERE":
        print("TikTok credentials not set in config.yaml.")
        print("To set up TikTok authentication:")
        print("  1. Create an app at https://developers.tiktok.com/")
        print("  2. Fill in client_key and client_secret in config.yaml")
        print("  3. Run 'python main.py --run' to go through the OAuth flow")
        print()
        print("Testing token structures (no real API call)...")

        auth_url = get_auth_url(
            client_key="test_client_key",
            redirect_uri="https://your-redirect-uri.com/callback",
        )
        print(f"Sample auth URL:\n  {auth_url[:100]}...")
    else:
        print("TikTok credentials found.")
        if tiktok.get("access_token"):
            print("  Access token: configured")
            print("  Refresh token:", "configured" if tiktok.get("refresh_token") else "missing")
            print("  Expires at:", tiktok.get("token_expires_at") or "unknown")
        else:
            print("  No access token yet. Run 'python main.py --run' to authenticate.")

    print("\nTest upload (test_mode=True — no real API call):")
    from pathlib import Path
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"fake video data for testing")
        fake_path = f.name

    result = upload_package(
        package={
            "compiled_path": fake_path,
            "caption_text": "Test post | #gaming #fyp",
        },
        user_config=config,
        test_mode=True,
    )
    print(f"  Test result: {result}")
    Path(fake_path).unlink()

    print("\nUploader test complete.")
