"""Config + secrets loading."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

load_dotenv(ROOT / ".env")


def load_yaml(name: str) -> dict:
    with open(ROOT / name, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


CONFIG = load_yaml("config.yaml")
KEYWORDS = load_yaml("keywords.yaml")


def env(key: str, required: bool = True, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required secret: {key}. "
            f"GitHub -> Settings -> Secrets and variables -> Actions me add karein."
        )
    return val or ""


class Secrets:
    """Read lazily so --help / --list work without every secret set."""

    @property
    def youtube_api_key(self) -> str:
        return env("YOUTUBE_API_KEY")

    @property
    def anthropic_api_key(self) -> str:
        return env("ANTHROPIC_API_KEY")

    @property
    def wp_user(self) -> str:
        return env("WP_USERNAME")

    @property
    def wp_app_password(self) -> str:
        # WordPress application password (spaces allowed, they get stripped)
        return env("WP_APP_PASSWORD").replace(" ", "")

    @property
    def proxy(self) -> str:
        """Optional generic proxy URL (http://user:pass@host:port).
        Webshare use kar rahe hain to iski zarurat nahi — neeche wale
        do secrets kaafi hain."""
        return env("TRANSCRIPT_PROXY", required=False, default="")

    @property
    def tfd_publish_key(self) -> str:
        """Plan B endpoint ki key. Set ho to normal Basic Auth ke bajaye
        /wp-json/tfd/v1/publish istemal hota hai — un servers ke liye jo
        Authorization header kaat dete hain."""
        # .strip() zaruri hai — GitHub secret box me paste karte waqt
        # aksar ek invisible newline saath chala jaata hai.
        return env("TFD_PUBLISH_KEY", required=False, default="").strip()

    @property
    def webshare_username(self) -> str:
        """Webshare dashboard -> Proxy -> Settings -> "Proxy Username".
        ZARURI: "Residential" package hona chahiye, "Proxy Server" ya
        "Static Residential" nahi — wo YouTube par kaam nahi karte."""
        return env("WEBSHARE_PROXY_USERNAME", required=False, default="")

    @property
    def webshare_password(self) -> str:
        return env("WEBSHARE_PROXY_PASSWORD", required=False, default="")


SECRETS = Secrets()
