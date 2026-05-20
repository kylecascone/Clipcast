"""
tiktok_shop
===========
TikTok Shop affiliate automation module for ClipCast Studio.

Pipeline:
  1. product_analyzer  — Import Kalodata CSV, score products with Claude
  2. script_generator  — Generate voiceover scripts with Claude
  3. voiceover         — Convert scripts to MP3 via ElevenLabs API  [coming soon]
  4. video_assembler   — Stitch product images + audio into MP4 via FFmpeg  [coming soon]
  5. drive_exporter    — Upload finished video to Google Drive for Make.com  [coming soon]
"""
