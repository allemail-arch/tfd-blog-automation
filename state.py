"""Kaunse videos process ho chuke hain - repo me commit hone wali state."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config import CONFIG, ROOT


class State:
    def __init__(self, path: Path | None = None):
        self.path = path or (ROOT / CONFIG["state"]["file"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"processed": {}, "failed": {}}
        self.data.setdefault("processed", {})
        self.data.setdefault("failed", {})

    @property
    def processed_ids(self) -> set[str]:
        return set(self.data["processed"])

    def skip_ids(self, max_failures: int = 3) -> set[str]:
        """Processed + wo videos jo baar baar fail ho rahe hain."""
        burned = {
            vid
            for vid, rec in self.data["failed"].items()
            if rec.get("count", 0) >= max_failures
        }
        return self.processed_ids | burned

    def mark_done(self, video_id: str, title: str, posts: list[dict]) -> None:
        self.data["processed"][video_id] = {
            "title": title,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "posts": posts,
        }
        self.data["failed"].pop(video_id, None)
        self.save()

    def mark_failed(self, video_id: str, reason: str) -> None:
        rec = self.data["failed"].get(video_id, {"count": 0})
        rec["count"] = rec.get("count", 0) + 1
        rec["reason"] = reason
        rec["last_attempt"] = datetime.now(timezone.utc).isoformat()
        self.data["failed"][video_id] = rec
        self.save()

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
