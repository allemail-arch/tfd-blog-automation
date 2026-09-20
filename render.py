"""Final post HTML assemble karna: video embed + body + FAQ + schema."""
from __future__ import annotations

import html
import json

from config import CONFIG
from generator import BlogPost, GuestInfo
from youtube_client import Video

_LABELS = {
    "en": {
        "watch": "Watch the full episode",
        "faq": "Frequently asked questions",
        "other": "Read this article in Hindi",
    },
    "hi": {
        "watch": "पूरा एपिसोड देखिए",
        "faq": "अक्सर पूछे जाने वाले सवाल",
        "other": "Read this article in English",
    },
}


def embed_html(video: Video, label: str) -> str:
    return (
        f'<figure class="wp-block-embed is-type-video is-provider-youtube">'
        f'<div class="wp-block-embed__wrapper">'
        f'<iframe width="560" height="315" loading="lazy" '
        f'src="https://www.youtube.com/embed/{video.video_id}" '
        f'title="{html.escape(video.title)}" frameborder="0" '
        f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
        f'gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>'
        f"</div><figcaption>{html.escape(label)}</figcaption></figure>"
    )


def faq_html(faq: list[dict], heading: str) -> str:
    if not faq:
        return ""
    items = "".join(
        f"<h3>{html.escape(q['question'])}</h3><p>{q['answer']}</p>"
        for q in faq
        if q.get("question") and q.get("answer")
    )
    return f"<h2>{html.escape(heading)}</h2>{items}" if items else ""


def json_ld(video: Video, post: BlogPost, guest: GuestInfo) -> str:
    graph: list[dict] = [
        {
            "@type": "VideoObject",
            "name": video.title,
            "description": post.meta_description,
            "thumbnailUrl": video.thumbnail_url,
            "uploadDate": video.published_at,
            "embedUrl": f"https://www.youtube.com/embed/{video.video_id}",
            "contentUrl": video.url,
        }
    ]
    if post.faq:
        graph.append(
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": q["question"],
                        "acceptedAnswer": {"@type": "Answer", "text": q["answer"]},
                    }
                    for q in post.faq
                    if q.get("question") and q.get("answer")
                ],
            }
        )
    if guest.founder_name:
        graph.append(
            {
                "@type": "Person",
                "name": guest.founder_name,
                **({"jobTitle": guest.role} if guest.role else {}),
                **(
                    {"worksFor": {"@type": "Organization", "name": guest.company}}
                    if guest.company
                    else {}
                ),
            }
        )
    payload = {"@context": "https://schema.org", "@graph": graph}
    # "</script>" ko JSON ke andar se script tag todne se roko
    blob = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{blob}</script>'


def keyword_audit(post, content: str, guest=None) -> str:
    """Har post ke saath ek report — kaunsa keyword kahan aur kitni baar aaya.

    Isse bina andaaze ke pata chalta hai ki SEO thik se laga ya nahi.
    Dry-run me preview file ke upar dikhta hai, aur GitHub job summary me bhi.
    """
    import re as _re

    plain = _re.sub(r"<[^>]+>", " ", content)
    plain = _re.sub(r"\s+", " ", plain).strip()
    low = plain.lower()
    words = plain.split()
    first100 = " ".join(words[:100]).lower()
    h2s = " ".join(_re.findall(r"<h2[^>]*>(.*?)</h2>", content, _re.S | _re.I)).lower()
    h2s = _re.sub(r"<[^>]+>", " ", h2s)

    def count(term: str) -> int:
        return low.count(term.lower()) if term else 0

    fk = post.focus_keyword or ""
    fk_l = fk.lower()
    n = count(fk)
    density = (n * len(fk.split()) / max(len(words), 1)) * 100

    def tick(ok: bool) -> str:
        return "YES" if ok else "NO  <-- missing"

    lines = [
        "=" * 62,
        "KEYWORD AUDIT",
        "=" * 62,
        f"language        : {post.language}",
        f"word count      : {len(words)}",
        "",
        f'FOCUS KEYWORD   : "{fk}"',
        f"  in title           : {tick(fk_l in post.title.lower())}",
        f"  in meta title      : {tick(fk_l in post.meta_title.lower())}",
        f"  in meta desc       : {tick(fk_l in post.meta_description.lower())}",
        f"  in first 100 words : {tick(fk_l in first100)}",
        f"  in an H2 heading   : {tick(fk_l in h2s)}",
        f"  in slug            : {tick(bool(fk) and fk_l.replace(' ', '-')[:20] in post.slug)}",
        f"  times in body      : {n}   (density {density:.2f}%)",
        "",
        "SECONDARY KEYWORDS:",
    ]
    for kw in post.secondary_keywords or []:
        c = count(kw)
        flag = "" if c else "   <-- claimed but not found in body"
        lines.append(f"  {c:>2}x  {kw}{flag}")
    if not post.secondary_keywords:
        lines.append("  (none)")

    if guest is not None:
        lines += ["", "GUEST / ENTITY MENTIONS:"]
        for label, val in (
            ("guest", guest.founder_name),
            ("company", guest.company),
        ):
            if val:
                lines.append(f"  {count(val):>2}x  {val}  ({label})")

    host_name = "Abhishek Vyas"
    brand = "The Founder's Dream"
    brand_n = count(brand)
    lines += [
        "",
        f"HOST MENTION    : {count(host_name)}x  {host_name}",
        f"BRAND MENTION   : {brand_n}x  {brand}",
    ]

    links = _re.findall(r'<a\s[^>]*href="([^"]+)"', content)
    internal = [l for l in links if "thefoundersdream.in" in l]
    lines += [
        "",
        f"LINKS           : {len(links)} total, {len(internal)} internal",
    ]
    for l in internal[:6]:
        lines.append(f"  - {l}")

    lines += [
        "",
        "TAGS            : " + ", ".join(post.tags or []),
        f"FAQ ITEMS       : {len(post.faq or [])}",
        "=" * 62,
    ]

    # Density warning — 2.5% se upar Google ko keyword stuffing lagta hai
    if density > 2.5:
        lines.insert(
            3, f"WARNING: focus keyword density {density:.2f}% is too high (>2.5%)"
        )
    return "\n".join(lines)


def lang_switch_html(sibling_url: str, language: str) -> str:
    """Ek hi jagah se banta hai taaki naye aur purane dono posts me same ho."""
    lbl = _LABELS.get(language, _LABELS["en"])
    return (
        f'<p class="tfd-lang-switch"><a href="{sibling_url}" '
        f'hreflang="{"hi" if language == "en" else "en"}">'
        f"{html.escape(lbl['other'])}</a></p>"
    )


def assemble(
    video: Video,
    post: BlogPost,
    guest: GuestInfo,
    sibling_url: str | None = None,
) -> str:
    c = CONFIG["content"]
    lbl = _LABELS.get(post.language, _LABELS["en"])
    parts: list[str] = []

    if sibling_url and c.get("cross_link_languages"):
        parts.append(lang_switch_html(sibling_url, post.language))

    if c.get("include_video_embed"):
        parts.append(embed_html(video, lbl["watch"]))

    parts.append(post.body_html)
    parts.append(faq_html(post.faq, lbl["faq"]))

    # NOTE: JSON-LD schema yahan JAAN BUJH KAR nahi daala jaata.
    # WordPress bahar se aaye content me se <script> tag hata deta hai
    # par andar ka text chhod deta hai — jisse poora JSON page par
    # nanga dikhne lagta hai. Schema alag se bheja jaata hai
    # (assemble_schema) aur wp_head me print hota hai.
    return "\n\n".join(p for p in parts if p)


def assemble_schema(video: Video, post: BlogPost, guest: GuestInfo) -> str:
    """Sirf JSON (bina <script> tag ke). WordPress ise meta me rakhkar
    <head> me print karta hai, jahan iski sahi jagah hai."""
    import json as _json

    graph: list[dict] = [
        {
            "@type": "VideoObject",
            "name": video.title,
            "description": post.meta_description,
            "thumbnailUrl": video.thumbnail_url,
            "uploadDate": video.published_at,
            "embedUrl": f"https://www.youtube.com/embed/{video.video_id}",
            "contentUrl": video.url,
        }
    ]
    if post.faq:
        graph.append(
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": q["question"],
                        "acceptedAnswer": {"@type": "Answer", "text": q["answer"]},
                    }
                    for q in post.faq
                    if q.get("question") and q.get("answer")
                ],
            }
        )
    if guest.founder_name:
        graph.append(
            {
                "@type": "Person",
                "name": guest.founder_name,
                **({"jobTitle": guest.role} if guest.role else {}),
                **(
                    {"worksFor": {"@type": "Organization", "name": guest.company}}
                    if guest.company
                    else {}
                ),
            }
        )
    return _json.dumps(
        {"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False
    )
