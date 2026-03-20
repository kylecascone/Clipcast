"""
youtube_uploader.py
===================
Handles YouTube Shorts uploads via official YouTube Data API v3.
Uses OAuth 2.0 with offline access so tokens persist between runs.
"""

import os
import json
import pickle
from pathlib import Path
from datetime import datetime

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parent

CREDENTIALS_FILE = ROOT / "youtube_credentials.json"
TOKEN_FILE       = ROOT / "youtube_token.pickle"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def get_youtube_client():
    creds = None

    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"youtube_credentials.json not found at {CREDENTIALS_FILE}. "
                    "Download OAuth credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube_shorts(
    video_path: str,
    title: str,
    description: str = "",
    hashtags: list = None,
    thumbnail_path: str = None,
) -> dict:
    """
    Upload a video to YouTube Shorts.
    Returns dict with video_id, url, and status.
    """
    if not Path(video_path).exists():
        return {"success": False, "error": f"Video file not found: {video_path}"}

    try:
        youtube = get_youtube_client()

        hashtag_str = ""
        if hashtags:
            hashtag_str = " ".join(f'#{h.strip("#")}' for h in hashtags[:8])

        # YouTube Shorts requires #Shorts in title or description
        if "#Shorts" not in title and "#Shorts" not in description:
            hashtag_str = "#Shorts " + hashtag_str

        full_description = f"{description}\n\n{hashtag_str}".strip()

        body = {
            "snippet": {
                "title":       title[:100],
                "description": full_description[:5000],
                "tags":        [h.strip("#") for h in (hashtags or [])[:15]],
                "categoryId":  "22",  # People & Blogs
            },
            "status": {
                "privacyStatus":           "public",
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            video_path,
            mimetype="video/mp4",
            resumable=True,
            chunksize=1024 * 1024 * 5,  # 5 MB chunks
        )

        print(f"Uploading to YouTube Shorts: {title[:60]}...")

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"Upload progress: {int(status.progress() * 100)}%")

        video_id = response["id"]
        url = f"https://youtube.com/shorts/{video_id}"
        print(f"Upload complete: {url}")

        if thumbnail_path and Path(thumbnail_path).exists():
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg"),
                ).execute()
                print("Thumbnail set successfully")
            except Exception as e:
                print(f"Thumbnail upload failed (non-critical): {e}")

        return {
            "success":     True,
            "video_id":    video_id,
            "url":         url,
            "title":       title,
            "uploaded_at": datetime.utcnow().isoformat(),
        }

    except HttpError as e:
        error_msg = f"YouTube API error: {e.resp.status} {e.content}"
        print(error_msg)
        return {"success": False, "error": error_msg}
    except Exception as e:
        print(f"Upload failed: {e}")
        return {"success": False, "error": str(e)}


def test_youtube_connection() -> bool:
    try:
        youtube = get_youtube_client()
        response = youtube.channels().list(part="snippet", mine=True).execute()
        channels = response.get("items", [])
        if channels:
            channel_name = channels[0]["snippet"]["title"]
            print(f"Connected to YouTube channel: {channel_name}")
            return True
        return False
    except Exception as e:
        print(f"YouTube connection test failed: {e}")
        return False


if __name__ == "__main__":
    print("Testing YouTube connection...")
    result = test_youtube_connection()
    print("Connected:", result)
