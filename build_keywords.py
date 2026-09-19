"""
Keyword bank apne aap banata hai — aapke hi channel aur site se.

Chalane par ye:
  1. YouTube se saare video titles + tags + descriptions padhta hai
  2. WordPress se saari posts ke titles + tags + Rank Math focus keywords padhta hai
  3. Claude se inhe topic pillars me cluster karwata hai
  4. keywords.auto.yaml likh deta hai

Ye file keywords.yaml ki JAGAH nahi leti — dono saath chalti hain.
keywords.yaml me aapke chune hue keywords aur asli search volumes hain;
keywords.auto.yaml me wo cheezein jo aap sach me baat karte hain.

Usage:
  python build_keywords.py
  python build_keywords.py --dry-run     # file mat likho, sirf dikhao
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import yaml

from budget import Usage, get_budget
from config import CONFIG, ROOT
from generator import _json_from, client
from wordpress_client import WordPressClient
from youtube_client import YouTubeClient

OUT_FILE = ROOT / "keywords.auto.yaml"


def log(m: str = "") -> None:
    print(m, flush=True)


# ------------------------------------------------------------ data gathering


def gather_youtube(limit: int = 300) -> list[dict]:
    yt = YouTubeClient()
    ids = yt.all_video_ids(CONFIG["channel"]["channel_id"])[:limit]
    log(f"  [yt] {len(ids)} videos mile")
    out = []
    for v in yt.hydrate(ids):
        out.append(
            {
                "title": v.title,
                "tags": v.tags[:15],
                "desc": v.description[:300],
                "long": v.duration_seconds >= CONFIG["selection"]["min_duration_seconds"],
            }
        )
    return out


def gather_wordpress(limit: int = 250) -> dict:
    wp = WordPressClient()
    posts, page = [], 1
    while len(posts) < limit:
        r = wp.session.get(
            f"{wp.api}/posts",
            params={
                "per_page": 100,
                "page": page,
                "_fields": "title,meta,tags",
                "status": "publish",
            },
            timeout=60,
        )
        if not r.ok:
            break
        batch = r.json()
        if not batch:
            break
        posts += batch
        page += 1
        if page > 5:
            break

    titles, focus = [], []
    for p in posts:
        t = re.sub(r"<[^>]+>", "", (p.get("title") or {}).get("rendered", "")).strip()
        if t:
            titles.append(t)
        meta = p.get("meta")
        if isinstance(meta, dict):
            fk = (meta.get("rank_math_focus_keyword") or "").strip()
            if fk:
                focus += [x.strip() for x in fk.split(",") if x.strip()]

    tags = []
    r = wp.session.get(
        f"{wp.api}/tags", params={"per_page": 100, "_fields": "name,count"}, timeout=60
    )
    if r.ok:
        tags = sorted(r.json(), key=lambda t: -t.get("count", 0))

    log(f"  [wp] {len(titles)} posts, {len(focus)} focus keywords, {len(tags)} tags")
    return {"titles": titles, "focus_keywords": focus, "tags": tags}


# ----------------------------------------------------------------- clustering


PROMPT = """You are building an SEO keyword bank for an Indian business podcast
called "The Founder's Dream" (thefoundersdream.in). It publishes blog posts from
its own YouTube episodes, in English and Hindi.

Below is everything the channel and the site have actually published. Your job is
to find the topics they GENUINELY cover — not topics you think a business podcast
should cover.

=== YOUTUBE VIDEO TITLES (long-form episodes) ===
{yt_long}

=== YOUTUBE VIDEO TITLES (shorts / clips) ===
{yt_short}

=== YOUTUBE TAGS USED ===
{yt_tags}

=== EXISTING BLOG POST TITLES ON THE SITE ===
{wp_titles}

=== FOCUS KEYWORDS ALREADY USED ON THE SITE ===
{wp_focus}

=== WORDPRESS TAGS (name: post count) ===
{wp_tags}

Return ONLY JSON:
{{
  "pillars": [
    {{
      "name": "short topic pillar name",
      "evidence": "how many episodes/posts cover this, briefly",
      "keywords_en": ["5-8 realistic search phrases in English for this pillar"],
      "keywords_hi": ["4-6 realistic search phrases in Hindi (Devanagari)"]
    }}
  ],
  "recurring_entities": ["companies, people, places that come up repeatedly"],
  "content_gaps": ["2-4 topics they clearly care about but have few posts on"],
  "suggested_tags": ["10-15 WordPress tags that would organise this content well"]
}}

Rules:
- 5 to 8 pillars. Each must be backed by real evidence in the data above.
- Keywords must be phrases a real person would type into Google, not topic labels.
- Do not invent pillars that have no evidence. Fewer, truer pillars beat more.
- Hindi keywords in Devanagari; common business words (startup, funding) may stay
  in English, because that is how people actually search."""


def cluster(yt: list[dict], wp: dict) -> dict:
    long_titles = [v["title"] for v in yt if v["long"]][:120]
    short_titles = [v["title"] for v in yt if not v["long"]][:60]
    all_tags = sorted({t for v in yt for t in v["tags"]})[:80]

    prompt = PROMPT.format(
        yt_long="\n".join("- " + t for t in long_titles) or "(none)",
        yt_short="\n".join("- " + t for t in short_titles) or "(none)",
        yt_tags=", ".join(all_tags) or "(none)",
        wp_titles="\n".join("- " + t for t in wp["titles"][:150]) or "(none)",
        wp_focus=", ".join(sorted(set(wp["focus_keywords"]))[:80]) or "(none)",
        wp_tags="\n".join(
            f"- {t['name']}: {t.get('count', 0)}" for t in wp["tags"][:60]
        )
        or "(none)",
    )

    bud = get_budget()
    bud.check_hard_stop()
    resp = client().messages.create(
        model=CONFIG["content"]["model"],
        max_tokens=6000,
        system="You are a careful SEO analyst. You never invent evidence.",
        messages=[{"role": "user", "content": prompt}],
    )
    spent = bud.record(
        CONFIG["content"]["model"],
        Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
        ),
    )
    log(f"  [cost] Rs {spent:.2f}")
    return _json_from("".join(b.text for b in resp.content if b.type == "text"))


# ---------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log("== Keyword bank build ==")
    yt = gather_youtube()
    wp = gather_wordpress()
    data = cluster(yt, wp)

    out = {
        "_generated_at": datetime.now(timezone.utc).isoformat(),
        "_source": "build_keywords.py — YouTube channel + thefoundersdream.in",
        "_note": (
            "Ye file apne aap banti hai. Hath se badlav keywords.yaml me karein, "
            "warna agli build par mit jayenge."
        ),
        **data,
    }

    text = yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=100)
    if args.dry_run:
        log(text)
    else:
        OUT_FILE.write_text(text, encoding="utf-8")
        log(f"  [out] {OUT_FILE.name} likh diya")

    log(f"\n{len(data.get('pillars', []))} pillars mile:")
    for p in data.get("pillars", []):
        log(f"  - {p['name']}  ({p.get('evidence', '')})")
    log(get_budget().status_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
