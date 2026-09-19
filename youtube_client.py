"""YouTube Data API v3 se videos fetch karna + Shorts filter karna."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from config import CONFIG, SECRETS

API = "https://www.googleapis.com/youtube/v3"
_DUR = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _parse_duration(iso: str) -> int:
    m = _DUR.match(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


@dataclass
class Video:
    video_id: str
    title: str
    description: str
    published_at: str
    duration_seconds: int
    thumbnail_url: str
    tags: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def published_date(self) -> datetime:
        if not self.published_at:
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        except ValueError:
            return datetime(1970, 1, 1, tzinfo=timezone.utc)


class YouTubeClient:
    def __init__(self, api_key: str | None = None):
        self.key = api_key or SECRETS.youtube_api_key
        self.session = requests.Session()

    def _get(self, path: str, **params) -> dict:
        params["key"] = self.key
        r = self.session.get(f"{API}/{path}", params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"YouTube API {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def uploads_playlist_id(self, channel_id: str) -> str:
        data = self._get("channels", part="contentDetails", id=channel_id)
        items = data.get("items") or []
        if not items:
            raise RuntimeError(f"Channel not found: {channel_id}")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def all_video_ids(self, channel_id: str, max_pages: int = 40) -> list[str]:
        """Uploads playlist se saare video IDs, newest -> oldest."""
        pid = self.uploads_playlist_id(channel_id)
        ids: list[str] = []
        token = None
        for _ in range(max_pages):
            data = self._get(
                "playlistItems",
                part="contentDetails",
                playlistId=pid,
                maxResults=50,
                **({"pageToken": token} if token else {}),
            )
            ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    def hydrate(self, video_ids: list[str]) -> list[Video]:
        """Video IDs ko full metadata me badalna (50 per call)."""
        out: list[Video] = []
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i : i + 50]
            data = self._get(
                "videos", part="snippet,contentDetails", id=",".join(chunk)
            )
            for item in data.get("items", []):
                sn = item["snippet"]
                thumbs = sn.get("thumbnails", {})
                best = (
                    thumbs.get("maxres")
                    or thumbs.get("standard")
                    or thumbs.get("high")
                    or thumbs.get("medium")
                    or {}
                )
                out.append(
                    Video(
                        video_id=item["id"],
                        title=sn.get("title", ""),
                        description=sn.get("description", ""),
                        published_at=sn.get("publishedAt", ""),
                        duration_seconds=_parse_duration(
                            item["contentDetails"].get("duration", "")
                        ),
                        thumbnail_url=best.get("url", ""),
                        tags=sn.get("tags", []) or [],
                    )
                )
        return out


def is_eligible(video: Video) -> tuple[bool, str]:
    """Long-form interview hai ya Short/clip?"""
    sel = CONFIG["selection"]
    if video.duration_seconds < sel["min_duration_seconds"]:
        return False, f"too short ({video.duration_seconds}s)"
    low = video.title.lower()
    for pat in sel.get("skip_title_patterns", []):
        if pat.lower() in low:
            return False, f"title matches skip pattern '{pat}'"
    return True, "ok"


def pick_next_videos(
    yt: YouTubeClient, processed_ids: set[str], count: int = 1
) -> list[Video]:
    """Ek hi baar channel scan karke `count` videos chuno.

    Priority: pehle recent window ke naye eligible videos (newest first),
    phir backlog (config ke order ke hisaab se). Recent window ke liye ek
    hydrate; backlog chahiye to ek aur. --count 3 par bhi utna hi quota
    lagta hai jitna --count 1 par.
    """
    sel = CONFIG["selection"]
    channel_id = CONFIG["channel"]["channel_id"]

    all_ids = yt.all_video_ids(channel_id)
    unseen = [v for v in all_ids if v not in processed_ids]
    if not unseen:
        return []

    # Recent window (all_ids newest -> oldest order me hai)
    cutoff = datetime.now(timezone.utc) - timedelta(days=sel["lookback_days"])
    head_ids = unseen[:50]
    head = yt.hydrate(head_ids)

    fresh = [v for v in head if v.published_date >= cutoff and is_eligible(v)[0]]
    fresh.sort(key=lambda v: v.published_date, reverse=True)

    picked: list[Video] = fresh[:count]
    if len(picked) >= count or not sel.get("backlog_fallback", True):
        return picked

    # Backlog: jo head me pehle se hydrate ho chuke hain unhe dobara mat maango
    head_set = set(head_ids)
    rest = yt.hydrate([v for v in unseen if v not in head_set])
    chosen = {v.video_id for v in picked}
    backlog = [
        v for v in (head + rest) if is_eligible(v)[0] and v.video_id not in chosen
    ]
    backlog.sort(
        key=lambda v: v.published_date,
        reverse=(sel.get("backlog_order") == "newest"),
    )
    return picked + backlog[: count - len(picked)]


def pick_next_video(yt: YouTubeClient, processed_ids: set[str]) -> Video | None:
    """Backward-compatible single-video wrapper."""
    got = pick_next_videos(yt, processed_ids, 1)
    return got[0] if got else None
