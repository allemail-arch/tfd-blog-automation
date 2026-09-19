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
        self.session.auth = HTTPBasicAuth(SECRETS.wp_user, SECRETS.wp_app_password)
        self.session.headers["User-Agent"] = "TFD-Blog-Automation/1.0"

    # ------------------------------------------------------------- utilities

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        r = self.session.request(method, f"{self.api}/{path}", timeout=90, **kw)
        if r.status_code >= 400:
            raise RuntimeError(
                f"WP {method} {path} -> {r.status_code}: {r.text[:500]}"
            )
        return r

    def verify_auth(self) -> str:
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
        tag_ids: list[int],
        featured_media: int | None,
        video_id: str,
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
        return self._req("POST", f"posts/{post_id}", json=fields).json()

    def set_lang_switch(self, post_id: int, paragraph_html: str) -> None:
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
