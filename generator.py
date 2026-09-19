"""Claude se founder details + bilingual blog post generate karna."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from anthropic import Anthropic

from budget import Usage, get_budget
from config import CONFIG, KEYWORDS, SECRETS
from transcripts import Transcript
from youtube_client import Video

_client: Anthropic | None = None
_auto_cache: dict | None = None


def _load_auto() -> dict:
    """keywords.auto.yaml optional hai — build_keywords.py na chalaya ho to
    file nahi hogi, aur sab kuch normal chalta rahega."""
    global _auto_cache
    if _auto_cache is None:
        import yaml

        from config import ROOT

        path = ROOT / "keywords.auto.yaml"
        try:
            _auto_cache = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            _auto_cache = {}
        except Exception as exc:  # noqa: BLE001
            print(f"  [keywords] keywords.auto.yaml padh nahi paya: {exc}")
            _auto_cache = {}
    return _auto_cache


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=SECRETS.anthropic_api_key)
    return _client


def _json_from(text: str) -> dict:
    """Model ke reply se JSON nikalna (code fence ke saath ya bina)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Model ne JSON nahi diya:\n{text[:600]}")
    blob = text[start : end + 1]

    # strict=False zaruri hai: model HTML ke andar asli newline daal deta hai,
    # jise strict JSON parser "Invalid control character" bolkar reject karta hai.
    try:
        return json.loads(blob, strict=False)
    except json.JSONDecodeError:
        # Aakhri koshish: string ke andar ke bache hue control chars escape karo
        cleaned = re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", blob.replace("\r\n", "\n")
        )
        return json.loads(cleaned, strict=False)


_META_RE = re.compile(r"={3,}\s*META\s*={3,}", re.I)
_ART_RE = re.compile(r"={3,}\s*ARTICLE\s*={3,}", re.I)


def _split_meta_article(raw: str) -> tuple[dict, str]:
    """Model ka jawab do hisson me aata hai: META (JSON) aur ARTICLE (raw HTML).

    HTML ko JSON string ke andar bhejne se newlines aur quotes par parser
    toot jaata tha, isliye article JSON se bahar rakha gaya hai.
    """
    m_art = _ART_RE.search(raw)
    if not m_art:
        # Purana format (sab kuch ek JSON me) — fallback
        data = _json_from(raw)
        return data, data.get("body_html", "")

    head = raw[: m_art.start()]
    body = raw[m_art.end() :].strip()

    m_meta = _META_RE.search(head)
    meta_text = head[m_meta.end() :] if m_meta else head
    data = _json_from(meta_text)

    # Model kabhi kabhi article ko code fence me daal deta hai
    fence = re.match(r"^```(?:html)?\s*(.+?)\s*```$", body, re.S)
    if fence:
        body = fence.group(1).strip()

    if len(body) < 200:
        raise RuntimeError(f"Article bahut chhota aaya ({len(body)} chars)")
    return data, body


def _call(prompt: str, max_tokens: int, system: str | None = None) -> str:
    model = CONFIG["content"]["model"]
    bud = get_budget()
    bud.check_hard_stop()

    resp = client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or "You are a precise assistant. Output only what is asked.",
        messages=[{"role": "user", "content": prompt}],
    )

    # Kharcha turant record karo — chahe aage parse fail ho jaye, paise to lag chuke.
    spent = bud.record(
        model,
        Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
        ),
    )
    print(f"  [cost] Rs {spent:.2f}  |  {bud.status_line()}")

    if resp.stop_reason == "max_tokens":
        # Warna aage jaakar ye ek confusing JSONDecodeError bankar aata hai.
        raise RuntimeError(
            f"Model ka jawab max_tokens ({max_tokens}) par kat gaya. "
            f"config.yaml me content.max_tokens badhaayein ya "
            f"content.target_word_count kam karein."
        )
    return "".join(b.text for b in resp.content if b.type == "text")


# ----------------------------------------------------------------- guest info


@dataclass
class GuestInfo:
    founder_name: str = ""
    company: str = ""
    role: str = ""
    industry: str = ""
    one_line_bio: str = ""
    key_topics: list[str] = field(default_factory=list)
    is_interview: bool = True


def extract_guest(video: Video, transcript: Transcript) -> GuestInfo:
    prompt = f"""From this podcast episode, extract the GUEST founder's details.
This is "The Founder's Dream" podcast. The HOST is Abhishek Vyas — never return the host as the guest.

TITLE: {video.title}

DESCRIPTION:
{video.description[:2000]}

TRANSCRIPT (first 6000 chars):
{transcript.text[:6000]}

Return ONLY JSON:
{{
  "founder_name": "guest's full name, or empty string if unclear",
  "company": "their company/venture, or empty string",
  "role": "e.g. Founder & CEO, or empty string",
  "industry": "e.g. D2C, SaaS, fintech, spirituality, or empty string",
  "one_line_bio": "one factual sentence about the guest, only from the material above",
  "key_topics": ["4-7 concrete topics actually discussed"],
  "is_interview": true/false
}}

Do not guess or invent. If something is not stated, use an empty string."""
    data = _json_from(_call(prompt, 1200))
    return GuestInfo(
        founder_name=data.get("founder_name", "") or "",
        company=data.get("company", "") or "",
        role=data.get("role", "") or "",
        industry=data.get("industry", "") or "",
        one_line_bio=data.get("one_line_bio", "") or "",
        key_topics=data.get("key_topics", []) or [],
        is_interview=bool(data.get("is_interview", True)),
    )


# ------------------------------------------------------------------ blog post


@dataclass
class BlogPost:
    title: str
    slug: str
    meta_title: str
    meta_description: str
    excerpt: str
    focus_keyword: str
    secondary_keywords: list[str]
    tags: list[str]
    body_html: str
    faq: list[dict]
    language: str


def _fmt_terms(items: list) -> str:
    """keywords.yaml me terms {term, volume} dicts hain."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            v = it.get("volume")
            vol = f"  [{v}/mo]" if v else "  [low volume]"
            out.append(f"  - {it['term']}{vol}")
        else:
            out.append(f"  - {it}")
    return "\n".join(out) or "  (none)"


def _auto_pillars(lang: str) -> str:
    """keywords.auto.yaml — build_keywords.py se bani, aapke apne content se."""
    auto = _load_auto()
    if not auto:
        return ""
    key = f"keywords_{lang}"
    lines = []
    for p in auto.get("pillars", [])[:8]:
        kws = p.get(key) or []
        if kws:
            lines.append(f"  {p.get('name', '?')}: " + ", ".join(kws[:6]))
    if not lines:
        return ""
    return (
        "\nTOPIC PILLARS THIS CHANNEL ACTUALLY COVERS "
        "(auto-derived from its own videos and posts):\n" + "\n".join(lines)
    )


def _kw_block(lang: str, guest: GuestInfo, related: list[dict] | None = None) -> str:
    k = KEYWORDS
    longtail = [
        t.replace("{founder}", guest.founder_name or "the founder").replace(
            "{company}", guest.company or "their company"
        )
        for t in k["longtail_templates"].get(lang, [])
    ]

    if related:
        links = "\n".join(f'  - "{r["title"]}" — {r["url"]}' for r in related)
        links_note = (
            "These are REAL existing posts on the site, chosen because they are "
            "topically close to this episode. Link to at least 2 of them from "
            "inside the body, with anchor text that fits the sentence."
        )
    else:
        links = "\n".join(
            f"  - {l['url']}  (anchor idea: {l.get('anchor_' + lang, l['anchor_en'])})"
            for l in k["internal_links"]
        )
        links_note = "Include at least 2 of these as real <a href> links in the body."

    return f"""BRAND KEYWORDS (use 1-2 times, naturally):
{chr(10).join('  - ' + x for x in k['brand'])}

HIGH-VOLUME TOPIC KEYWORDS — monthly Google searches in India shown:
{_fmt_terms(k.get('high_volume', {}).get(lang, []))}

NICHE KEYWORDS — lower volume, but the intent matches this content exactly:
{_fmt_terms(k.get('niche', {}).get(lang, []))}
{_auto_pillars(lang)}

CHOOSING THE FOCUS KEYWORD — read this carefully:
  Pick ONE focus keyword. Prefer a high-volume term, but ONLY if this episode
  genuinely delivers on it. A reader arriving from that search must find what
  they were looking for. If no high-volume term honestly fits, take a niche one,
  or a long-tail below. Forcing a big keyword onto an episode that does not
  cover it is the single worst thing you can do here — Google detects it and
  the whole site suffers. Choosing a smaller, honest keyword is the right call.

LONG-TAIL (use 2-3 — these are how this guest's own audience will find the post):
{chr(10).join('  - ' + x for x in longtail)}

INTERNAL LINKS:
{links}
  {links_note}

NEVER use these phrases:
{chr(10).join('  - ' + x for x in k['avoid'])}

CLOSING CTA (adapt, do not copy verbatim):
  {k['cta'].get(lang, k['cta']['en'])}"""


def generate_post(
    video: Video,
    transcript: Transcript,
    guest: GuestInfo,
    lang_code: str,
    related: list[dict] | None = None,
) -> BlogPost:
    c = CONFIG["content"]
    lang_name = "English" if lang_code == "en" else "Hindi (Devanagari script)"

    timestamps = ""
    if c.get("include_timestamps"):
        marks = transcript.timestamped_outline()
        timestamps = "\n".join(f"{ts} — {txt[:90]}" for ts, txt in marks[:14])

    # Transcript trim: ~45k chars comfortably fits and keeps cost sane
    body = transcript.text[:45000]

    # TFD ke videos ka transcript aksar Hindi ASR hota hai. Agar post ki
    # bhasha transcript se alag hai, model ko explicitly batana zaruri hai.
    src_lang = (transcript.language or "").split("-")[0].lower()
    cross_lingual = ""
    if src_lang and src_lang != lang_code:
        src_name = {"hi": "Hindi", "en": "English"}.get(src_lang, src_lang)
        cross_lingual = (
            f"\nIMPORTANT: The transcript is in {src_name} and it is an "
            f"auto-generated caption, so it contains recognition errors. "
            f"You are writing in {lang_name}. Translate the guest's meaning "
            f"faithfully — never translate so loosely that the claim changes. "
            f"For blockquotes, give a clean {lang_name} translation of what the "
            f"guest actually said and do not present it as a word-for-word "
            f"verbatim quote. If a passage of the caption is garbled or its "
            f"meaning is unclear, leave it out entirely rather than guessing.\n"
        )

    system = (
        "You are a senior SEO content writer for an Indian business podcast. "
        "You write specific, quote-rich articles grounded strictly in the transcript "
        "you are given. You never invent facts, numbers, names, or quotes."
    )

    prompt = f"""Write a blog post in {lang_name} based on this podcast episode.

=== EPISODE ===
Video title: {video.title}
Video URL: {video.url}
Published: {video.published_at}
Guest: {guest.founder_name or 'unnamed guest'} — {guest.role} at {guest.company}
Industry: {guest.industry}
Topics discussed: {', '.join(guest.key_topics)}

Chapter markers from the transcript:
{timestamps}

=== TRANSCRIPT (language: {transcript.language}, source: {transcript.source}) ==={cross_lingual}
{body}

=== SEO BRIEF ===
{_kw_block(lang_code, guest, related)}

=== REQUIREMENTS ===
- Length: ~{c['target_word_count']} words of real substance.
- Ground EVERY claim in the transcript. No invented statistics, funding figures, or dates.
- Include 2-4 direct quotes from the guest as <blockquote> — quote them accurately.
- Structure: short intro (no throat-clearing), then 4-6 <h2> sections, <h3> where useful.
- Use <ul>/<li> for takeaway lists. Keep paragraphs to 2-4 sentences.
- Focus keyword must appear in: title, meta description, first 100 words, and one <h2>.
- Write for a reader who has NOT watched the video — the post must stand alone.
- Tone: direct, practical, respectful. No hype, no filler, no AI clichés.
- If the episode is not a founder interview, write it as a topic/ideas article instead.
{"- For Hindi: natural spoken Hindi in Devanagari. Common business terms (startup, funding, brand) can stay in English — that is how people actually speak." if lang_code == "hi" else ""}

=== OUTPUT FORMAT ===
Output EXACTLY two sections, in this order, with these marker lines alone on
their own line. Nothing before the first marker, nothing after the article.

===META===
{{
  "title": "compelling H1, under 70 chars, contains focus keyword",
  "slug": "url-safe-lowercase-slug-in-english-ascii-only-max-8-words",
  "meta_title": "SEO title, 50-60 chars",
  "meta_description": "meta description, 140-158 chars, contains focus keyword",
  "excerpt": "2-sentence summary for listing pages",
  "focus_keyword": "the ONE primary keyword you chose",
  "secondary_keywords": ["4-6 keywords actually used in the body"],
  "tags": ["5-8 WordPress tags"],
  "faq": [{{"question": "...", "answer": "..."}}]
}}
===ARTICLE===
(the article here as a plain HTML fragment — NOT inside JSON, NOT in a code
fence. Allowed tags: h2, h3, p, ul, ol, li, blockquote, strong, em, a, table,
tr, td, th. Do NOT include <html>, <head>, <body>, the H1 title, the video
embed, or the featured image — those are added automatically.)

The META block must be valid JSON on its own. Keep FAQ answers to plain
sentences without line breaks.
{"Include 3-5 FAQ items answering real questions the episode addresses." if c.get('include_faq_schema') else 'The "faq" list can be empty.'}"""

    raw = _call(prompt, c["max_tokens"], system=system)
    data, body_html = _split_meta_article(raw)
    data["body_html"] = body_html

    for required in ("title", "body_html"):
        if not data.get(required):
            raise RuntimeError(f"Model ke JSON me '{required}' missing hai")

    # Hindi post par model kabhi kabhi Devanagari slug deta hai. Clean karne
    # par wo khali reh jaata hai — main._safe_slug() us case ko sambhaalta hai.
    slug = re.sub(
        r"-+", "-", re.sub(r"[^a-z0-9-]", "", (data.get("slug") or "").lower().replace(" ", "-"))
    )[:80]
    return BlogPost(
        title=data["title"],
        slug=slug.strip("-"),
        meta_title=data.get("meta_title", data["title"])[:70],
        meta_description=data.get("meta_description", "")[:165],
        excerpt=data.get("excerpt", ""),
        focus_keyword=data.get("focus_keyword", ""),
        secondary_keywords=data.get("secondary_keywords", []),
        tags=data.get("tags", []),
        body_html=data["body_html"],
        faq=data.get("faq", []) or [],
        language=lang_code,
    )
