"""config.py — target endpoints from environment variables.

Committed defaults are PLACEHOLDERS on purpose (this file is public). Real values live
in .env (gitignored). See .env.example / LOCAL_SETUP.md. No third-party deps: .env is
parsed here directly.
"""
import os
import re
from pathlib import Path

_ENV = Path(__file__).with_name(".env")


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        # real OS env wins over .env
        os.environ.setdefault(key.strip(), val.strip())


_load_env(_ENV)

ORIGIN = os.environ.get("TARGET_ORIGIN", "https://REDACTED.example.com")
HOMEPAGE = os.environ.get("TARGET_HOMEPAGE", f"{ORIGIN}/REDACTED/homepage.html")
MYCONTENT = os.environ.get("TARGET_MYCONTENT", f"{ORIGIN}/REDACTED/my-content")
FEED_BASE = os.environ.get("TARGET_FEED_BASE", f"{ORIGIN}/REDACTED/feed")
REPORT_PREFIX = os.environ.get("TARGET_REPORT_PREFIX", "/REDACTED/reports")


def feed_url(offset: int, limit: int) -> str:
    return f"{FEED_BASE}?offset={offset}&limit={limit}&getNewResults=false"


def report_regex() -> "re.Pattern[str]":
    """Report-URL matcher built from the (masked) path prefix — no real path hardcoded."""
    return re.compile(re.escape(REPORT_PREFIX) + r"/(\d{4})/(\d{2})/(\d{2})/([0-9a-f-]{36})\.html")
