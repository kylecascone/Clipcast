"""
hashtags.py
===========
Generates platform-optimised hashtag sets for each processed clip.

Returns a dict with keys: tiktok, youtube, instagram, all.
"""

from typing import Dict, List


def generate_hashtags(clip: dict) -> Dict[str, object]:
    creator  = (clip.get("creator_name") or "").lower().replace(" ", "")
    category = (clip.get("category") or "").lower()
    theme    = (clip.get("theme") or "")
    title    = ((clip.get("viral_title") or clip.get("title") or "")).lower()
    platform = (clip.get("source") or "twitch").lower()

    hashtags: List[str] = []

    # ── Base viral tags ───────────────────────────────────────────────────────
    hashtags.extend(["viral", "fyp", "foryou", "foryoupage", "trending"])

    # ── Platform tags ─────────────────────────────────────────────────────────
    _platform_tags = {
        "twitch":  ["twitch", "twitchclips", "twitchstreamer", "livestream"],
        "youtube": ["youtube", "youtuber", "youtubeshorts"],
        "kick":    ["kick", "kickstreaming", "kickclips"],
        "reddit":  ["reddit"],
    }
    hashtags.extend(_platform_tags.get(platform, ["streaming"]))

    # ── Creator tag ───────────────────────────────────────────────────────────
    if creator:
        clean = "".join(c for c in creator if c.isalnum())
        if clean:
            hashtags.append(clean)

    # ── Game / category tags ──────────────────────────────────────────────────
    _game_tags = [
        "fortnite", "minecraft", "valorant", "apex", "cod",
        "league", "overwatch", "gta", "warzone", "rocketleague",
        "cs2", "csgo", "dota", "pubg", "squadgame",
    ]
    added_gaming = False
    for game in _game_tags:
        if game in category or game in title:
            hashtags.append(game)
            if not added_gaming:
                hashtags.extend(["gaming", "gamer"])
                added_gaming = True
            break

    if "just chatting" in category or "irl" in category:
        hashtags.extend(["irl", "irlstreaming", "justchatting"])

    if not added_gaming and ("game" in category or "gaming" in category):
        hashtags.extend(["gaming", "gamer"])

    # ── Theme tags ────────────────────────────────────────────────────────────
    _theme_tags: Dict[str, List[str]] = {
        "FUNNY":          ["funny", "lol", "comedy", "humor"],
        "RAGE":           ["rage", "angry", "raging"],
        "SHOCKED":        ["shocked", "unbelievable", "omg"],
        "CLUTCH":         ["clutch", "insane", "gaming"],
        "FAIL":           ["fail", "fails", "epicfail"],
        "WHOLESOME":      ["wholesome", "heartwarming", "sweet"],
        "PRANK":          ["prank", "pranks", "funny"],
        "SPORTS_MOMENT":  ["sports", "athlete", "highlights"],
        "VIRAL_MOMENT":   ["viral", "viralvideo", "trending"],
        "PUBLIC_FREAKOUT":["publicfreakout", "caught", "crazy"],
        "UNEXPECTED":     ["unexpected", "surprise", "omg"],
    }
    hashtags.extend(_theme_tags.get(theme, ["viral", "trending"]))

    # ── Title signal tags ─────────────────────────────────────────────────────
    if any(w in title for w in ["ban", "banned"]):
        hashtags.extend(["banned", "twitch"])
    if any(w in title for w in ["record", "world record"]):
        hashtags.extend(["worldrecord", "record"])
    if any(w in title for w in ["react", "reaction"]):
        hashtags.extend(["reaction", "reacts"])
    if any(w in title for w in ["donate", "donation"]):
        hashtags.extend(["donation", "wholesome"])
    if any(w in title for w in ["clip", "moment", "highlights"]):
        hashtags.extend(["clips", "moments"])

    # ── Deduplicate, preserve order ───────────────────────────────────────────
    seen: set = set()
    unique: List[str] = []
    for tag in hashtags:
        clean = tag.lower().strip("#").replace(" ", "")
        if clean and clean not in seen:
            seen.add(clean)
            unique.append(f"#{clean}")

    return {
        "tiktok":    " ".join(unique[:20]),   # TikTok sweet spot 15-20
        "youtube":   " ".join(unique[:8]),    # YouTube Shorts 5-8
        "instagram": " ".join(unique[:25]),   # Instagram up to 30
        "all":       unique,
    }
