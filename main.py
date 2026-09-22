"""
The Founder's Dream — YouTube to WordPress blog automation.

Usage:
  python main.py                      # ek video process karo aur publish karo
  python main.py --dry-run            # kuch publish mat karo, files me likho
  python main.py --video-id XXXX      # ek specific video
  python main.py --count 3            # backlog catch-up
  python main.py --check              # sirf connections test karo
  python main.py --force              # dedupe ignore karke dobara banao
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone

from budget import BudgetExceeded, get_budget
from config import CONFIG, ROOT
from generator import extract_guest, generate_post
from render import assemble, assemble_schema, keyword_audit, lang_switch_html
from sheets import log_posts
from state import State
from transcripts import fetch_transcript
from wordpress_client import WordPressClient
from youtube_client import YouTubeClient, is_eligible, pick_next_videos

OUT = ROOT / "output"
VERBOSE = False


class Skip(Exception):
    """Ye failure nahi hai — video bas process karne layak nahi hai.
    Isse failure counter nahi badhta, warna ek pehle se publish ho chuka
    video 3 run ke baad hamesha ke liye skip list me chala jaata."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------- check


def run_check() -> int:
    log("== Connection check ==")
    ok = True
    try:
        yt = YouTubeClient()
        pid = yt.uploads_playlist_id(CONFIG["channel"]["channel_id"])
        ids = yt.all_video_ids(CONFIG["channel"]["channel_id"], max_pages=1)
        log(f"  YouTube    OK  uploads playlist {pid}, first page {len(ids)} videos")
    except Exception as exc:  # noqa: BLE001
        log(f"  YouTube    FAIL  {exc}")
        ok = False

    try:
        wp = WordPressClient()
        log(f"  WordPress  OK  authenticated as {wp.verify_auth()}")
        cats = wp.list_categories()
        log("             categories: " + ", ".join(f"{c['id']}={c['name']}" for c in cats))
        configured = {i for l in CONFIG["languages"] for i in l["category_ids"]}
        missing = configured - {c["id"] for c in cats}
        if missing:
            log(f"  WordPress  WARN  config.yaml me aisi category IDs hain jo site par nahi: {missing}")
            ok = False
    except Exception as exc:  # noqa: BLE001
        log(f"  WordPress  FAIL  {exc}")
        ok = False

    try:
        from .generator import client

        client().messages.create(
            model=CONFIG["content"]["model"],
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with: ok"}],
        )
        log(f"  Anthropic  OK  model {CONFIG['content']['model']}")
    except Exception as exc:  # noqa: BLE001
        log(f"  Anthropic  FAIL  {exc}")
        ok = False

    from config import SECRETS

    if SECRETS.gsheet_url:
        try:
            import requests as _rq

            r = _rq.post(
                SECRETS.gsheet_url,
                json={"key": SECRETS.gsheet_key, "rows": []},
                timeout=45,
            )
            ok = r.ok and '"ok":true' in r.text.replace(" ", "")
            log(f"  Sheet      {'OK  jud gayi' if ok else 'FAIL  ' + r.text[:150]}")
        except Exception as exc:  # noqa: BLE001
            log(f"  Sheet      FAIL  {exc}")
    else:
        log("  Sheet      (GSHEET_URL set nahi hai — logging band)")

    st = State()
    log(f"  State      {len(st.processed_ids)} videos already processed")
    log(f"  Budget     {get_budget().status_line()}")
    return 0 if ok else 1


# ------------------------------------------------------------------ pipeline


def _existing_languages(
    wp: WordPressClient, video_id: str
) -> dict[str, tuple[int, str]]:
    """Is video ki jo posts pehle se site par hain: {lang_code: (post_id, url)}."""
    found: dict[str, tuple[int, str]] = {}
    for post in wp.find_by_video_id(video_id):
        meta = post.get("meta")
        lang = meta.get("tfd_language") if isinstance(meta, dict) else None
        if not lang:
            # mu-plugin na ho to slug suffix se andaza lagao
            slug = post.get("slug", "")
            lang = next(
                (
                    l["code"]
                    for l in CONFIG["languages"]
                    if l["slug_suffix"] and slug.endswith(l["slug_suffix"])
                ),
                CONFIG["languages"][0]["code"],
            )
        found.setdefault(lang, (post.get("id", 0), post.get("link", "")))
    return found


def process_video(video, wp: WordPressClient | None, dry_run: bool, force: bool) -> dict:
    log(f"\n>> {video.title}")
    log(f"   {video.url}  ({video.duration_seconds // 60} min)")

    cost_before = get_budget().spent_inr

    eligible, why = is_eligible(video)
    if not eligible:
        raise Skip(f"not eligible: {why}")

    languages = list(CONFIG["languages"])
    already: dict[str, tuple[int, str]] = {}
    if wp and not force:
        already = _existing_languages(wp, video.video_id)
        if already:
            log(f"  [wp] already on site: { {k: v[1] for k, v in already.items()} }")
        languages = [l for l in languages if l["code"] not in already]
    if not languages:
        raise Skip("all languages already published for this video")

    transcript = fetch_transcript(video.video_id)
    if not transcript:
        raise RuntimeError("no transcript available")
    min_words = CONFIG["content"]["min_transcript_words"]
    if transcript.word_count < min_words:
        raise RuntimeError(
            f"transcript too thin ({transcript.word_count} < {min_words} words)"
        )

    guest = extract_guest(video, transcript)
    log(
        f"  [guest] {guest.founder_name or '(unknown)'}"
        f"{' — ' + guest.role if guest.role else ''}"
        f"{' @ ' + guest.company if guest.company else ''}"
    )

    # --- site par milti-julti purani posts dhoondho (internal linking) ---
    # Dry-run me bhi chahiye, warna preview me links nahi dikhte aur hum
    # asli output judge nahi kar paate. Ye sirf padhta hai, kuch badalta nahi.
    link_wp = wp
    if link_wp is None:
        try:
            link_wp = WordPressClient()
        except Exception:  # noqa: BLE001
            link_wp = None

    related: list[dict] = []
    if link_wp:
        terms = [guest.company, guest.industry, *guest.key_topics[:4]]
        try:
            related = link_wp.related_posts([t for t in terms if t])
            if related:
                log(f"  [links] {len(related)} related posts: "
                    + "; ".join(r["title"][:45] for r in related))
        except Exception as exc:  # noqa: BLE001
            log(f"  [links] related posts fail: {exc}")

    # --- generate sab languages, phir slugs final karo -------------------
    rendered: list[tuple[dict, object]] = []
    errors: dict[str, str] = {}
    for lang in languages:
        try:
            post = generate_post(video, transcript, guest, lang["code"], related)
        except Exception as exc:  # noqa: BLE001
            log(f"  [gen:{lang['code']}] FAILED {exc}")
            errors[lang["code"]] = f"generation: {exc}"
            continue
        post.slug = _safe_slug(post.slug, lang, video, guest)
        if wp and not dry_run:
            post.slug = wp.unique_slug(post.slug)
        log(f"  [gen:{lang['code']}] {post.title}")
        log(f"           slug={post.slug}  focus='{post.focus_keyword}'")
        rendered.append((lang, post))

    if not rendered:
        raise RuntimeError("; ".join(errors.values()) or "nothing generated")

    # Featured image create_post ke andar hi lagti hai (dono raaston par)
    thumb_alt = (
        f"{guest.founder_name} on The Founder's Dream podcast"
        if guest.founder_name
        else video.title
    )

    # --- publish ----------------------------------------------------------
    results: list[dict] = []
    live_urls: dict[str, str] = {k: v[1] for k, v in already.items()}

    for lang, post in rendered:
        record = {
            "language": lang["code"],
            "title": post.title,
            "slug": post.slug,
            "focus_keyword": post.focus_keyword,
        }
        content = assemble(video, post, guest, sibling_url=None)

        audit = keyword_audit(post, content, guest)

        if dry_run or wp is None:
            OUT.mkdir(exist_ok=True)
            f = OUT / f"{video.video_id}-{lang['code']}.html"
            f.write_text(
                f"<!--\n{audit}\n-->\n\n"
                f"<!-- meta_title       : {post.meta_title} -->\n"
                f"<!-- meta_description : {post.meta_description} -->\n\n"
                f"<h1>{post.title}</h1>\n\n{content}",
                encoding="utf-8",
            )
            log(f"  [dry-run] wrote {f.relative_to(ROOT)}")
            log("\n" + audit + "\n")
            record["file"] = str(f.relative_to(ROOT))
            record["audit"] = audit
            results.append(record)
            continue

        log("\n" + audit + "\n")
        record["audit"] = audit

        try:
            res = wp.create_post(
                title=post.title,
                slug=post.slug,
                content=content,
                excerpt=post.excerpt,
                category_ids=lang["category_ids"],
                tag_names=post.tags,
                thumbnail_url=video.thumbnail_url,
                thumbnail_alt=thumb_alt,
                video_id=video.video_id,
                video_url=video.url,
                language=lang["code"],
                schema_json=assemble_schema(video, post, guest),
                rankmath={
                    "rank_math_title": post.meta_title,
                    "rank_math_description": post.meta_description,
                    "rank_math_focus_keyword": post.focus_keyword,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  [wp:{lang['code']}] FAILED {exc}")
            errors[lang["code"]] = f"publish: {exc}"
            continue

        log(f"  [wp:{lang['code']}] {res.status} -> {res.url}")
        record.update({"post_id": res.post_id, "url": res.url, "status": res.status})
        record["_post_obj"] = post
        live_urls[lang["code"]] = res.url
        results.append(record)

    # --- ab asli URLs pata hain, to cross-language link laga do ----------
    if not dry_run and wp and CONFIG["content"].get("cross_link_languages"):
        for record in results:
            post = record.pop("_post_obj", None)
            sibling = next(
                (u for code, u in live_urls.items() if code != record["language"] and u),
                None,
            )
            if not (post and sibling and record.get("post_id")):
                continue
            try:
                wp.update_post(
                    record["post_id"],
                    {"content": assemble(video, post, guest, sibling_url=sibling)},
                )
                log(f"  [wp:{record['language']}] cross-link -> {sibling}")
            except Exception as exc:  # noqa: BLE001
                log(f"  [wp:{record['language']}] cross-link failed: {exc}")

        # Pichli run me jo post pehle se live thi, use bhi back-link do —
        # warna hreflang pairing ek-tarfa reh jaati hai.
        for code, (pid, _url) in already.items():
            sibling = next(
                (u for c2, u in live_urls.items() if c2 != code and u), None
            )
            if not (pid and sibling):
                continue
            try:
                wp.set_lang_switch(pid, lang_switch_html(sibling, code))
                log(f"  [wp:{code}] purani post par back-link -> {sibling}")
            except Exception as exc:  # noqa: BLE001
                log(f"  [wp:{code}] back-link failed: {exc}")

    # --- Google Sheet log (sirf jo posts sach me live hui) -------------
    posts_by_lang = {
        lang["code"]: post for lang, post in rendered
    }
    if not dry_run:
        try:
            log_posts(
                video,
                guest,
                results,
                posts_by_lang,
                run_cost=get_budget().spent_inr - cost_before,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  [sheet] fail: {exc}")

    for record in results:
        record.pop("_post_obj", None)

    pending = [
        l["code"] for l in CONFIG["languages"] if l["code"] not in live_urls
    ] if not dry_run else []

    return {"posts": results, "errors": errors, "pending_languages": pending}


def _safe_slug(raw: str, lang: dict, video, guest) -> str:
    """Model kabhi kabhi Devanagari slug deta hai jo clean hone par khali reh
    jaata hai. Tab video ID se ek stable ASCII slug banao."""
    slug = (raw or "").strip("-")
    if len(slug) < 3:
        base = guest.founder_name or video.title
        ascii_base = "".join(
            ch if ch.isascii() and (ch.isalnum() or ch == " ") else " " for ch in base
        )
        words = [w for w in ascii_base.lower().split() if w][:6]
        slug = "-".join(words) or "tfd-episode"
        slug = f"{slug}-{video.video_id.lower()}"
    return (slug + lang["slug_suffix"])[:90].strip("-")


# ---------------------------------------------------------------------- main


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description="TFD YouTube -> WordPress automation")
    ap.add_argument("--dry-run", action="store_true", help="publish mat karo")
    ap.add_argument("--video-id", help="ek specific video process karo")
    ap.add_argument("--count", type=int, default=None, help="kitne videos")
    ap.add_argument("--check", action="store_true", help="sirf connections test")
    ap.add_argument(
        "--force",
        action="store_true",
        help="state aur WordPress dedupe dono ignore karo (duplicate ban sakte hain)",
    )
    ap.add_argument("--verbose", action="store_true", help="poora traceback dikhao")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if args.check:
        return run_check()

    state = State()
    wp = None if args.dry_run else WordPressClient()
    yt = YouTubeClient()
    count = args.count or CONFIG["selection"]["videos_per_run"]

    if args.video_id:
        queue = yt.hydrate([args.video_id])
        if not queue:
            log(f"Video not found: {args.video_id}")
            return 1
    else:
        skip = set() if args.force else state.skip_ids()
        queue = pick_next_videos(yt, skip, count)

    if not queue:
        log("Koi naya eligible video nahi mila. Sab process ho chuke hain.")
        return 0

    budget = get_budget()
    log(budget.status_line())

    summary: list[dict] = []
    exit_code = 0
    for video in queue:
        entry: dict = {"video": video.title, "id": video.video_id}
        try:
            if not args.dry_run:
                budget.check_before_video()
            outcome = process_video(video, wp, args.dry_run, args.force)
            entry["posts"] = outcome["posts"]
            if outcome["errors"]:
                entry["partial_errors"] = outcome["errors"]
                exit_code = 1
            if not args.dry_run:
                if outcome["pending_languages"]:
                    # Adhura hua: state me mark mat karo taaki agli run me
                    # baaki bhasha bhi ban jaaye (dedupe use skip karwa dega).
                    entry["pending_languages"] = outcome["pending_languages"]
                    log(f"  [state] pending: {outcome['pending_languages']} — agli run me retry")
                else:
                    state.mark_done(
                        video.video_id,
                        video.title,
                        [
                            {k: v for k, v in p.items() if k != "audit"}
                            for p in outcome["posts"]
                        ],
                    )
        except BudgetExceeded as exc:
            # Failure nahi — jaan bujh kar rok rahe hain. Baaki videos bhi chhodo.
            log(f"  [budget] {exc}")
            entry["budget_stop"] = str(exc)
            summary.append(entry)
            break
        except Skip as exc:
            # Failure nahi — counter mat badhao. Agar site par sab kuch pehle se
            # hai to state me done mark kar do taaki dobara na uthe.
            log(f"  [skipped] {exc}")
            entry["skipped"] = str(exc)
            if not args.dry_run and "already published" in str(exc):
                state.mark_done(video.video_id, video.title, [])
        except Exception as exc:  # noqa: BLE001
            log(f"  [FAILED] {exc}")
            if VERBOSE:
                traceback.print_exc()
            if not args.dry_run:
                state.mark_failed(video.video_id, str(exc)[:300])
            entry["error"] = str(exc)
            exit_code = 1
        summary.append(entry)

    log("\n== Summary ==")
    # audit lamba hai aur upar already print ho chuka hai — JSON me mat daalo
    brief = [
        {
            **s,
            "posts": [
                {k: v for k, v in p.items() if k != "audit"} for p in s.get("posts", [])
            ],
        }
        for s in summary
    ]
    log(json.dumps(brief, indent=2, ensure_ascii=False))
    log(budget.status_line())

    _write_gh_summary(summary)
    return exit_code


def _write_gh_summary(summary: list[dict]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### TFD run {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", ""]
    for s in summary:
        if "error" in s:
            lines.append(f"- **FAILED** {s['video']} — `{s['error']}`")
        for p in s.get("posts", []):
            target = p.get("url") or p.get("file", "")
            lines.append(f"- `[{p['language']}]` [{p['title']}]({target})")
        if s.get("pending_languages"):
            lines.append(f"  - pending: {', '.join(s['pending_languages'])}")

        # Keyword audit — yahin dikh jaye taaki zip download na karna pade
        for p in s.get("posts", []):
            if p.get("audit"):
                lines += [
                    "",
                    f"<details><summary>Keyword audit — {p['language']}: "
                    f"{p['title']}</summary>",
                    "",
                    "```",
                    p["audit"],
                    "```",
                    "",
                    "</details>",
                ]
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        log(f"[warn] could not write job summary: {exc}")


if __name__ == "__main__":
    sys.exit(main())
