"""Transcript nikalna - 3 fallback layers ke saath.

Layer 1: youtube-transcript-api (free, fastest)
Layer 2: yt-dlp se auto-subtitles (jab layer 1 block ho)
Layer 3: koi nahi -> video skip (thin content Google ko pasand nahi)

NOTE: YouTube GitHub Actions ke datacenter IPs ko aksar block karta hai.
Isliye TRANSCRIPT_PROXY secret (residential proxy) set karna strongly
recommended hai. Proxy ke bina layer 1/2 kabhi kabhi fail hote hain.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config import SECRETS


@dataclass
class Transcript:
    text: str
    language: str
    source: str
    segments: list[dict]

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def timestamped_outline(self, every_seconds: int = 300) -> list[tuple[str, str]]:
        """Har ~5 min par ek marker, taaki post me chapters ban sakein."""
        out, next_mark = [], 0.0
        for seg in self.segments:
            start = float(seg.get("start", 0))
            if start >= next_mark:
                mm, ss = divmod(int(start), 60)
                hh, mm = divmod(mm, 60)
                stamp = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
                out.append((stamp, seg.get("text", "").strip()))
                next_mark = start + every_seconds
        return out


_NOISE = re.compile(
    r"\[(music|applause|laughter|laughs|संगीत|तालियाँ|हंसी|"
    r"inaudible|silence|foreign|__)\]",
    re.I,
)


def _clean(text: str) -> str:
    text = _NOISE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _via_api(video_id: str) -> Transcript | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import (
            GenericProxyConfig,
            WebshareProxyConfig,
        )
    except ImportError as exc:
        print(f"  [transcript] youtube-transcript-api import failed: {exc}")
        return None

    kwargs = {}
    if SECRETS.webshare_username and SECRETS.webshare_password:
        # Webshare "Residential" — rotating IPs, YouTube ke liye sabse reliable
        kwargs["proxy_config"] = WebshareProxyConfig(
            proxy_username=SECRETS.webshare_username,
            proxy_password=SECRETS.webshare_password,
        )
        print("  [transcript] Webshare residential proxy istemal ho raha hai")
    elif SECRETS.proxy:
        kwargs["proxy_config"] = GenericProxyConfig(
            http_url=SECRETS.proxy, https_url=SECRETS.proxy
        )
        print("  [transcript] generic proxy istemal ho raha hai")
    else:
        print(
            "  [transcript] koi proxy set nahi hai — GitHub ke IP par YouTube "
            "aksar block karta hai. WEBSHARE_PROXY_USERNAME/PASSWORD secrets daalein."
        )

    # TFD ke videos par aksar sirf Hindi auto-caption (ASR) hota hai.
    # Manual captions behtar hote hain, isliye pehle wo try karte hain.
    langs = ["hi", "en", "en-IN", "hi-IN"]
    try:
        api = YouTubeTranscriptApi(**kwargs)
        listing = api.list(video_id)
        tr = None
        for finder in (
            listing.find_manually_created_transcript,
            listing.find_generated_transcript,
        ):
            try:
                tr = finder(langs)
                break
            except Exception:
                continue
        if tr is None:
            # Jo bhi mile, wahi le lo
            tr = next(iter(listing), None)
        if tr is None:
            return None

        segs = [
            {"start": s.start, "duration": s.duration, "text": s.text}
            for s in tr.fetch()
        ]
        return Transcript(
            text=_clean(" ".join(s["text"] for s in segs)),
            language=tr.language_code,
            source="youtube-transcript-api",
            segments=segs,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [transcript] api layer failed: {type(exc).__name__}: {exc}")
    return None


def _via_ytdlp(video_id: str) -> Transcript | None:
    if not shutil.which("yt-dlp"):
        return None

    # yt-dlp ke liye proxy URL chahiye. Webshare ka rotating endpoint:
    proxy_url = SECRETS.proxy
    if not proxy_url and SECRETS.webshare_username and SECRETS.webshare_password:
        proxy_url = (
            f"http://{SECRETS.webshare_username}-rotate:"
            f"{SECRETS.webshare_password}@p.webshare.io:80"
        )

    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs",
            "hi,en,en-IN",
            "--sub-format",
            "json3",
            "-o",
            f"{tmp}/%(id)s.%(ext)s",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        if proxy_url:
            cmd += ["--proxy", proxy_url]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        except Exception as exc:  # noqa: BLE001
            print(f"  [transcript] yt-dlp layer failed: {exc}")
            return None

        for f in sorted(Path(tmp).glob("*.json3")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            segs = []
            for ev in data.get("events", []):
                txt = "".join(s.get("utf8", "") for s in ev.get("segs", []))
                if txt.strip():
                    segs.append(
                        {
                            "start": ev.get("tStartMs", 0) / 1000,
                            "duration": ev.get("dDurationMs", 0) / 1000,
                            "text": txt,
                        }
                    )
            if segs:
                lang = f.name.split(".")[-2]
                return Transcript(
                    text=_clean(" ".join(s["text"] for s in segs)),
                    language=lang,
                    source="yt-dlp",
                    segments=segs,
                )
    return None


def fetch_transcript(video_id: str) -> Transcript | None:
    for layer in (_via_api, _via_ytdlp):
        tr = layer(video_id)
        if tr and tr.word_count > 50:
            print(f"  [transcript] {tr.word_count} words via {tr.source} ({tr.language})")
            return tr
    print("  [transcript] NOT AVAILABLE")
    return None
