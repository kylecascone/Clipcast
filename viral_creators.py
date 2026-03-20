"""
viral_creators.py
=================
Curated whitelist of Tier 1 viral creators on Twitch, YouTube, and Kick.

WHITELIST-ONLY POLICY
---------------------
The shared pool ONLY fetches clips from creators on these lists.
No trending discovery, no unknown streamers, no global search.
Every clip in the pool comes from a named, verified English-speaking creator.

These lists are used by:
  - pool_fetcher.py    — source of truth for what goes in the pool
  - scorer.py          — Tier 1 creator score bonus
  - fetcher_twitch.py  — target streamers
  - fetcher_youtube.py — target YouTube channels
"""

from typing import Dict, List


# ── Tier 1 Twitch creators ─────────────────────────────────────────────────────
# Lowercase login names exactly as they appear in the Twitch API.
TIER1_TWITCH: List[str] = [
    "xqc", "kai_cenat", "jynxzi", "caseoh_", "hasanabi", "nickmercs",
    "shroud", "summit1g", "sodapoppin", "lirik", "ishowspeed", "adinross",
    "moistcr1tikal", "pokimane", "trainwreckstv", "mizkif", "nmplol",
    "emiru", "forsen", "qtcinderella", "destiny", "timthetatman",
    "drlupo", "nickeh30", "myth", "tfue", "cloakzy", "symfuhny",
    "nadeshot", "courage", "highdistortion", "dragonarius",
    "loltyler1", "trick2g", "yassuo", "doublelift", "imaqtpie",
    "alinity", "amouranth", "fuslie", "nymn",
    "tarik", "ohnepixel", "s1mple", "autimatic", "stewie2k",
]

# ── Tier 1 YouTube creators ────────────────────────────────────────────────────
# Dict: {display_name: channel_id}
# Channel IDs are stable even when channel handles change.
TIER1_YOUTUBE: Dict[str, str] = {
    "MrBeast":          "UCX6OQ3DkcsbYNE6H8uQQuVA",
    "IShowSpeed":       "UCkS_HP3m9L4I3bI_hPCVMKA",
    "Logan Paul":       "UCG0MgGnAZEAKMGJNLeDn9gA",
    "KSI":              "UC3gNmTGu-TTbFPpfSe5f9uA",
    "Pokimane":         "UCpqXJOEqGS-TCnazcHCo0rA",
    "Markiplier":       "UC7_YgWpupZT9xMYMZaVd7YA",
    "Ninja":            "UCYVinkwSX7szARULgYpgbDQ",
    "TimTheTatman":     "UCxRdHsGBYEFuVqCiDMbmYEA",
    "Nickmercs":        "UCt6UPiZ8e_2bNmDg_0Uz1kg",
    "HasanAbi":         "UCLiMO3KQNM7M2tnRDOQDuRg",
    "Ludwig":           "UCyB4MHYpBMHIXPyXBMtCFkQ",
    "Valkyrae":         "UCPvg6WWxOHHaLcBMzBjXYYA",
    "Disguised Toast":  "UCnUYZLuoy1rq1aVMwx4sFsA",
    "Sykkuno":          "UCRBMbbQGiFDUGEGbcBmyjbQ",
    "Jacksepticeye":    "UCYzPXprvl5Y-Sf0g4vX-m6g",
    "Pewdiepie":        "UC-lHJZR3Gqxm24_Vd_AJ5Yw",
    "Dream":            "UCTkXRDQl0luXxVQrRQvWS6w",
    "TommyInnit":       "UCq6VFHwMzcMXbuKyG7SQYIg",
    "Moistcr1tikal":    "UC1JTQBa5QxZCpXrFSkMxmPw",
    "Caseoh":           "UCWsDFcIhY2DBi3GB5uykGXA",
}

# ── Tier 1 Kick creators ───────────────────────────────────────────────────────
# Lowercase channel slugs as they appear in kick.com URLs.
TIER1_KICK: List[str] = [
    "xqc", "adinross", "trainwreck", "destiny", "sneako",
    "n3on", "jidion", "ishowspeed", "kai_cenat", "flair",
]

# ── Viral title signal words ───────────────────────────────────────────────────
VIRAL_TITLE_SIGNALS: List[str] = [
    "first time", "reaction", "ban", "banned", "rage", "rage quit",
    "funny", "insane", "crazy", "wtf", "lol", "broke", "record",
    "never", "always", "best", "worst", "fail", "win", "clutch",
    "caught", "moment", "shocked", "gone wrong", "unbelievable",
    "pog", "clip", "highlight", "compilation", "funniest",
]


# ── Combined sets for fast lookup ──────────────────────────────────────────────
_ALL_TIER1_LOWER = (
    {n.lower() for n in TIER1_TWITCH}
    | {n.lower() for n in TIER1_YOUTUBE.keys()}
    | {n.lower() for n in TIER1_KICK}
)


def get_tier1_youtube_channel_ids() -> List[str]:
    """Return list of all Tier 1 YouTube channel IDs."""
    return list(TIER1_YOUTUBE.values())


def is_tier1_creator(name: str) -> bool:
    """
    Return True if the creator name matches a known Tier 1 viral creator.

    Matching is case-insensitive and normalises underscores, hyphens, and
    spaces so that 'Kai Cenat', 'kai_cenat', and 'kai-cenat' all match.
    """
    if not name:
        return False

    lower = name.lower().strip()

    if lower in _ALL_TIER1_LOWER:
        return True

    def _norm(s: str) -> str:
        return s.replace(" ", "").replace("_", "").replace("-", "")

    cleaned = _norm(lower)
    for tier1_name in _ALL_TIER1_LOWER:
        if cleaned == _norm(tier1_name):
            return True

    return False


def get_creator_tier(name: str) -> int:
    """Return 1 for Tier 1 creators, 2 for all others."""
    return 1 if is_tier1_creator(name) else 2


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("viral_creators.py — self-test\n")
    print(f"Tier 1 Twitch:         {len(TIER1_TWITCH)} creators")
    print(f"Tier 1 YouTube:        {len(TIER1_YOUTUBE)} creators")
    print(f"Tier 1 Kick:           {len(TIER1_KICK)} creators")
    print(f"Viral title signals:   {len(VIRAL_TITLE_SIGNALS)} terms")
    print()

    test_cases = [
        ("xqc",                  True),
        ("XQC",                  True),
        ("kai_cenat",            True),
        ("Kai Cenat",            True),
        ("MrBeast",              True),
        ("mrbeast",              True),
        ("IShowSpeed",           True),
        ("ishowspeed",           True),
        ("shroud",               True),
        ("unknown_streamer_xyz", False),
        ("пакистанский стример", False),
    ]

    all_passed = True
    for name, expected in test_cases:
        result = is_tier1_creator(name)
        status = "OK" if result == expected else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"  [{status}] is_tier1('{name}') = {result}  (expected {expected})")

    print()
    print("All tests passed." if all_passed else "Some tests FAILED.")
