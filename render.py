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
    parts.append(json_ld(video, post, guest))
    return "\n\n".join(p for p in parts if p)
