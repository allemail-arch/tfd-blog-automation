"""WordPress REST API client - media upload, tags, posts, dedupe."""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass

import requests
from requests.auth import HTTPBasicAuth

from config import CONFIG, SECRETS


@dataclass
class PublishResult:
    post_id: int
    url: str
    status: str


class WordPressClient:
    def __init__(self):
        self.base = CONFIG["wordpress"]["base_url"].rstrip("/")
        self.api = f"{self.base}/wp-json/wp/v2"
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "TFD-Blog-Automation/1.0"

        # Plan B: kuch servers Authorization header kaat dete hain, to
        # normal Basic Auth kaam hi nahi karta. Us soorat me hum apne
        # endpoint se likhte hain jo key body me leta hai.
        self.publish_key = SECRETS.tfd_publish_key
        self.custom = bool(self.publish_key)
        self.custom_url = f"{self.base}/wp-json/tfd/v1/publish"

        if not self.custom:
            self.session.auth = HTTPBasicAuth(
                SECRETS.wp_user, SECRETS.wp_app_password
            )

    # ------------------------------------------------- custom endpoint call

    def _tfd(self, payload: dict, attempts: int = 4) -> dict:
        body = {"key": self.publish_key, **payload}
        r = None
        for attempt in range(1, attempts + 1):
            r = self.session.post(self.custom_url, json=body, timeout=120)
            if r.status_code < 400:
                break

            # Host ka firewall kabhi kabhi GitHub ke IP ko challenge page
            # dikha deta hai — wo HTML hota hai, hamara JSON error nahi.
            # Ye asthayi hota hai, isliye thoda ruk kar dobara koshish.
            looks_like_firewall = (
                "application/json" not in (r.headers.get("Content-Type") or "")
                or r.text.lstrip().startswith("<")
            )
            if not looks_like_firewall or attempt == attempts:
                break

            wait = 20 * attempt
            print(
                f"  [wp] host ne block kiya (HTTP {r.status_code}, firewall page) "
                f"— attempt {attempt}/{attempts}, {wait}s baad dobara"
            )
            import time as _t

            _t.sleep(wait)

        if r.status_code >= 400:
            hint = ""
            if r.text.lstrip().startswith("<"):
                # HTML jawab = host ka firewall, hamara endpoint nahi
                raise RuntimeError(
                    f"TFD endpoint {payload.get('action')} -> {r.status_code}: "
                    f"host ke firewall ne block kiya (HTML challenge page mila, "
                    f"JSON nahi). Key galat NAHI hai. {attempts} koshishein ki. "
                    f"Baar baar ho to Hostinger support se kahein ki GitHub "
                    f"Actions ke IPs ko REST API par allow karein."
                )
            if r.status_code == 403:
                # Key kabhi log mat karo — sirf lambai aur shakl, taaki
                # mismatch pakda ja sake bina secret leak kiye.
                k = self.publish_key
                hint = (
                    f"\n      GitHub wali key: {len(k)} characters, "
                    f"shuru '{k[:3]}...', aakhir '...{k[-3:]}'"
                    f"\n      WordPress snippet ki pehli line se milaakar dekhein."
                )
            raise RuntimeError(
                f"TFD endpoint {payload.get('action')} -> {r.status_code}: "
                f"{r.text[:300]}{hint}"
            )
        return r.json()

    # ------------------------------------------------------------- utilities

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        r = self.session.request(method, f"{self.api}/{path}", timeout=90, **kw)
        if r.status_code >= 400:
            raise RuntimeError(
                f"WP {method} {path} -> {r.status_code}: {r.text[:500]}"
            )
        return r

    def verify_auth(self) -> str:
        if self.custom:
            d = self._tfd({"action": "ping"})
            return f"TFD publish endpoint OK — site '{d.get('site')}'"
        r = self._req("GET", "users/me")
        me = r.json()
        return f"{me.get('name')} (id {me.get('id')}, roles={me.get('roles')})"

    def list_categories(self) -> list[dict]:
        r = self._req(
            "GET", "categories", params={"per_page": 100, "_fields": "id,name,slug"}
        )
        return r.json()

    # ---------------------------------------------------------------- dedupe

    def find_by_video_id(self, video_id: str) -> list[dict]:
        if self.custom:
            d = self._tfd({"action": "find", "video_id": video_id})
            return [
                {
                    "id": p["id"],
                    "link": p["link"],
                    "slug": p.get("slug", ""),
                    "meta": {"tfd_language": p.get("language", "")},
                }
                for p in d.get("posts", [])
            ]
        return self._find_by_video_id_rest(video_id)

    def _find_by_video_id_rest(self, video_id: str) -> list[dict]:
        """Kya is video ki post pehle se hai? meta_key search ke bina bhi
        kaam kare, isliye slug aur search dono try karte hain."""
        hits: list[dict] = []
        r = self.session.get(
            f"{self.api}/posts",
            params={
                "search": video_id,
                "per_page": 10,
                "status": "publish,draft,pending,future,private",
                "_fields": "id,link,slug,title,meta",
            },
            timeout=60,
        )
        if r.ok and isinstance(r.json(), list):
            hits += r.json()
        r2 = self.session.get(
            f"{self.api}/posts",
            params={
                "meta_key": "tfd_youtube_id",
                "meta_value": video_id,
                "per_page": 10,
                "status": "publish,draft,pending,future,private",
                "_fields": "id,link,slug,title,meta",
            },
            timeout=60,
        )
        if r2.ok and isinstance(r2.json(), list):
            hits += r2.json()
        seen, out = set(), []
        for h in hits:
            if h["id"] not in seen:
                seen.add(h["id"])
                out.append(h)
        return out

    def related_posts(self, terms: list[str], limit: int = 4) -> list[dict]:
        """Site par pehle se maujood milti-julti posts dhoondho, taaki nayi
        post unse link kar sake. Fixed links se ye kaafi behtar hai —
        Google ko topical clusters pasand hain."""
        scored: dict[int, dict] = {}
        for term in [t for t in terms if t and len(t) > 3][:6]:
            r = self.session.get(
                f"{self.api}/posts",
                params={
                    "search": term,
                    "per_page": 5,
                    "status": "publish",
                    "orderby": "relevance",
                    "_fields": "id,link,title",
                },
                timeout=60,
            )
            if not r.ok:
                continue
            for rank, p in enumerate(r.json()):
                rec = scored.setdefault(
                    p["id"],
                    {
                        "url": p["link"],
                        "title": re.sub(
                            r"<[^>]+>", "", (p.get("title") or {}).get("rendered", "")
                        ).strip(),
                        "score": 0,
                    },
                )
                rec["score"] += 5 - rank

        out = sorted(scored.values(), key=lambda x: -x["score"])[:limit]
        return [o for o in out if o["title"]]

    def slug_exists(self, slug: str) -> bool:
        if not slug:
            return True  # khali slug kabhi allow mat karo
        r = self.session.get(
            f"{self.api}/posts",
            params={
                "slug": slug,
                "status": "publish,draft,pending,future,private",
                "_fields": "id",
            },
            timeout=60,
        )
        return bool(r.ok and r.json())

    def unique_slug(self, slug: str) -> str:
        if not self.slug_exists(slug):
            return slug
        for n in range(2, 12):
            cand = f"{slug}-{n}"
            if not self.slug_exists(cand):
                return cand
        return slug

    # ----------------------------------------------------------------- media

    def upload_thumbnail(self, image_url: str, filename: str, alt: str) -> int | None:
        try:
            img = requests.get(image_url, timeout=60)
            img.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"  [wp] thumbnail download failed: {exc}")
            return None

        ctype = img.headers.get("Content-Type") or mimetypes.guess_type(filename)[0]
        ctype = ctype or "image/jpeg"
        ext = ".png" if "png" in ctype else ".jpg"
        if not filename.endswith(ext):
            filename += ext

        r = self.session.post(
            f"{self.api}/media",
            data=img.content,
            headers={
                "Content-Type": ctype,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
            timeout=120,
        )
        if r.status_code >= 400:
            print(f"  [wp] media upload failed {r.status_code}: {r.text[:300]}")
            return None
        media_id = r.json()["id"]
        # alt text set karo (accessibility + image SEO)
        a = self.session.post(
            f"{self.api}/media/{media_id}",
            json={"alt_text": alt, "caption": alt},
            timeout=60,
        )
        if a.status_code >= 400:
            print(f"  [wp] alt text set failed {a.status_code}: {a.text[:200]}")
        return media_id

    # ------------------------------------------------------------------ tags

    def ensure_tags(self, names: list[str]) -> list[int]:
        ids: list[int] = []
        for name in names:
            name = name.strip()
            if not name:
                continue
            r = self.session.get(
                f"{self.api}/tags",
                params={"search": name, "per_page": 100, "_fields": "id,name"},
                timeout=60,
            )
            found = next(
                (t for t in (r.json() if r.ok else []) if t["name"].lower() == name.lower()),
                None,
            )
            if found:
                ids.append(found["id"])
                continue
            c = self.session.post(f"{self.api}/tags", json={"name": name}, timeout=60)
            if c.status_code < 400:
                ids.append(c.json()["id"])
            elif c.status_code == 400 and "term_exists" in c.text:
                existing = c.json().get("data", {}).get("term_id")
                if existing:
                    ids.append(int(existing))
        return ids

    # ------------------------------------------------------------------ post

    def create_post(
        self,
        *,
        title: str,
        slug: str,
        content: str,
        excerpt: str,
        category_ids: list[int],
        tag_names: list[str],
        thumbnail_url: str = "",
        thumbnail_alt: str = "",
        schema_json: str = "",
        video_id: str = "",
        video_url: str,
        language: str,
        rankmath: dict | None = None,
    ) -> PublishResult:
        wp = CONFIG["wordpress"]
        meta = {
            "tfd_youtube_id": video_id,
            "tfd_youtube_url": video_url,
            "tfd_language": language,
            "tfd_generated": "1",
        }
        if wp.get("write_rankmath_meta") and rankmath:
            meta.update(rankmath)

        # --- Plan B raasta ---
        if self.custom:
            d = self._tfd(
                {
                    "action": "create",
                    "title": title,
                    "slug": slug,
                    "content": content,
                    "excerpt": excerpt,
                    "status": wp.get("post_status", "draft"),
                    "categories": category_ids,
                    "tags": tag_names or [],
                    "meta": meta,
                    "author": wp.get("author_id") or 1,
                    "featured_image_url": thumbnail_url or "",
                    "featured_image_alt": thumbnail_alt or title,
                    "schema": schema_json or "",
                }
            )
            if d.get("featured_image") and d["featured_image"] != "ok":
                print(f"  [wp] featured image: {d['featured_image']}")
            return PublishResult(
                post_id=d["id"], url=d["link"], status=d["status"]
            )

        # --- normal REST raasta (Basic Auth) ---
        tag_ids = self.ensure_tags(tag_names or [])
        featured_media = None
        if wp.get("upload_thumbnail") and thumbnail_url:
            featured_media = self.upload_thumbnail(
                thumbnail_url, f"tfd-{video_id}", thumbnail_alt or title
            )

        payload: dict = {
            "title": title,
            # NOTE: caller pehle hi unique_slug() laga chuka hai, taaki
            # cross-language links sahi slug par point karein.
            "slug": slug,
            "content": content,
            "excerpt": excerpt,
            "status": wp.get("post_status", "draft"),
            "categories": category_ids,
            "tags": tag_ids,
            "meta": meta,
            "comment_status": "open",
        }
        if featured_media:
            payload["featured_media"] = featured_media
        if wp.get("author_id"):
            payload["author"] = wp["author_id"]

        try:
            r = self._req("POST", "posts", json=payload)
        except RuntimeError as exc:
            # Agar mu-plugin install nahi hai to WP custom meta reject karta hai.
            # Sirf usi soorat me retry karo — baaki errors chhupane nahi hain.
            msg = str(exc)
            meta_rejected = "rest_invalid_param" in msg and any(
                k in msg for k in meta
            )
            if meta_rejected:
                print(
                    "  [wp] custom meta rejected — mu-plugin install nahi hai? "
                    "Bina meta ke retry kar raha hoon (dedupe kamzor ho jayega)."
                )
                payload.pop("meta", None)
                r = self._req("POST", "posts", json=payload)
            else:
                raise

        data = r.json()
        return PublishResult(
            post_id=data["id"], url=data["link"], status=data["status"]
        )

    def update_post(self, post_id: int, fields: dict) -> dict:
        """Publish ke baad content patch karna (cross-language links ke liye)."""
        if self.custom:
            return self._tfd(
                {"action": "update", "post_id": post_id, **fields}
            )
        return self._req("POST", f"posts/{post_id}", json=fields).json()

    def set_lang_switch(self, post_id: int, paragraph_html: str) -> None:
        if self.custom:
            # Purani post ka raw content custom endpoint se nahi milta,
            # isliye prepend nahi kar sakte — chhod dete hain. Nayi post
            # me link fir bhi lagta hai, bas jodi ek-tarfa reh jaati hai.
            print(
                f"  [wp] post {post_id} par back-link skip "
                f"(custom endpoint mode me raw content nahi milta)"
            )
            return

        """Pehle se live post ke upar language-switch link laga/replace karna.

        Zarurat tab padti hai jab pichli run me sirf ek bhasha publish hui thi
        aur doosri ab ban rahi hai — purani post ko bhi back-link chahiye.
        """
        r = self._req(
            "GET", f"posts/{post_id}", params={"context": "edit", "_fields": "content"}
        )
        raw = (r.json().get("content") or {}).get("raw", "")
        cleaned = re.sub(
            r'<p class="tfd-lang-switch">.*?</p>\s*', "", raw, flags=re.S
        )
        self.update_post(post_id, {"content": paragraph_html + "\n\n" + cleaned})
