"""Har live post ka record Google Sheet me bhejna.

Google Apps Script web app istemal hota hai — koi API key ya service
account JSON nahi, sirf ek URL aur ek shared key.

Agar GSHEET_URL set nahi hai to ye chup-chaap kuch nahi karta.
Aur agar sheet me bhejna fail ho jaye to bhi post publish ho chuki
hoti hai — logging kabhi publishing ko nahi rokegi.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from config import SECRETS

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M")


def log_posts(
    video,
    guest,
    results: list[dict],
    posts_by_lang: dict,
    run_cost: float = 0.0,
) -> None:
    """results = main.py ka records list (url, title, language, ...)."""
    url = SECRETS.gsheet_url
    if not url:
        return

    live = [r for r in results if r.get("url")]
    if not live:
        return

    # kharcha barabar baant dete hain taaki har row me kuch dikhe
    per_post = round(run_cost / len(live), 2) if run_cost else ""

    rows = []
    for r in live:
        post = posts_by_lang.get(r["language"])
        rows.append(
            {
                "date": _ist_now(),
                "language": r["language"],
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "focus_keyword": r.get("focus_keyword", ""),
                "words": len(post.body_html.split()) if post else "",
                "guest": guest.founder_name or "",
                "company": guest.company or "",
                "video_title": video.title,
                "video_url": video.url,
                "tags": ", ".join(post.tags) if post else "",
                "cost": per_post,
                "status": r.get("status", ""),
            }
        )

    try:
        resp = requests.post(
            url,
            json={"key": SECRETS.gsheet_key, "rows": rows},
            timeout=60,
            allow_redirects=True,  # Apps Script /exec redirect karta hai
        )
        if resp.ok and '"ok":true' in resp.text.replace(" ", ""):
            print(f"  [sheet] {len(rows)} row Google Sheet me likh di")
        else:
            print(
                f"  [sheet] nahi likh paya (HTTP {resp.status_code}): "
                f"{resp.text[:200]}"
            )
    except Exception as exc:  # noqa: BLE001
        # Logging kabhi publishing ko fail na kare
        print(f"  [sheet] fail: {exc}")
