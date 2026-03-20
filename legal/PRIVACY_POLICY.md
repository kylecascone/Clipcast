# ClipCast Studio — Privacy Policy

**Version 1.0 — Effective February 2026**

---

## Overview

ClipCast Studio is a **local-first application**. Almost all data stays on your machine.
We have no backend server, no analytics pipeline, and no telemetry. What happens on your
computer stays on your computer.

---

## Data Stored Locally

The following data is stored **only** on your computer:

| Data                  | Location            | Purpose                                          |
|-----------------------|---------------------|--------------------------------------------------|
| API credentials       | `config.yaml`       | Authenticate with Twitch, YouTube, TikTok        |
| User preferences      | `preferences.yaml`  | Your clip length, posting schedule, and settings |
| Clip metadata         | `clipcast.db`       | Track which clips have been processed            |
| Post history          | `clipcast.db`       | Avoid duplicate posts                            |
| Audit log             | `clipcast.db`       | Your own legal protection record                 |
| Consent record        | `clipcast.db`       | Timestamp of when you accepted the Terms         |
| API quota usage       | `clipcast.db`       | Track daily API usage to stay within limits      |

**None of this data is transmitted to ClipCast servers.** There are no analytics,
no crash reporting, and no telemetry of any kind.

---

## Third-Party API Calls

ClipCast Studio communicates with third-party services **directly from your machine**:

| Service       | Data Sent               | Purpose                               |
|---------------|-------------------------|---------------------------------------|
| Twitch API    | Client credentials      | Fetch public clip metadata            |
| YouTube API   | API key                 | Fetch public video data               |
| TikTok API    | OAuth token             | Post videos you authorize             |
| Instagram API | OAuth token             | Post videos you authorize             |

All API calls go directly from your machine to the respective platform. ClipCast Studio
does not proxy, log, or inspect these communications beyond what is shown in the terminal.

---

## Video Content

Raw clips, processed videos, and temporary files are stored in the `clips/` folder on
your machine. ClipCast Studio does not upload your content anywhere other than the
platforms you configure.

---

## Future Web Dashboard

A future optional web dashboard may collect:

- Usage statistics (clips processed, posts made) — anonymized, opt-in only
- Error reports — if you opt in explicitly
- Billing information — if you subscribe to a paid tier

The web dashboard will have its own Privacy Policy and will require explicit consent
before any data collection begins.

---

## Your Rights

Because all data is stored locally, you have complete control:

- **Delete your data**: Delete `clipcast.db` and the `clips/` folder
- **Export your data**: Run `python main.py --audit` to export your activity log to CSV
- **Revoke API access**: Revoke app permissions in each platform's developer dashboard

---

## Contact

See `legal/DMCA_POLICY.md` for contact information.
