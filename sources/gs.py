"""sources/gs.py - Goldman Sachs adapter (the original source).

Enumerates via the my-stream JSON feed, HTML via navigation, PDF via in-page fetch - the
behavior that used to live inline in daily.py. Keeps its bare native UUID as report_id for
back-compat with rows already stored that way; DB source column = 'gs'.

Host/paths come from env (masked in this public repo). Back-compat: the original TARGET_*
names still work, so an existing .env keeps running; GS_* names take precedence if set.
"""
import os
import re

from sources.base import Source

_ORIGIN = (os.environ.get("GS_ORIGIN") or os.environ.get("TARGET_ORIGIN")
           or "https://REDACTED.example.com")
_MYCONTENT = (os.environ.get("GS_MYCONTENT") or os.environ.get("TARGET_MYCONTENT")
              or f"{_ORIGIN}/REDACTED/my-content")
_FEED_BASE = (os.environ.get("GS_FEED_BASE") or os.environ.get("TARGET_FEED_BASE")
              or f"{_ORIGIN}/REDACTED/feed")

_DATE_IN_PATH = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/")


class GoldmanSachs(Source):
    key = "gs"
    label = "Goldman Sachs"
    id_prefix = None  # bare UUID

    def warm_url(self):
        return _MYCONTENT

    def feed_url(self, offset, limit):
        return f"{_FEED_BASE}?offset={offset}&limit={limit}&getNewResults=false"

    def date(self, item):
        m = _DATE_IN_PATH.search(item.get("path") or "")
        return "-".join(m.groups()) if m else super().date(item)

    def content_url(self, item):
        return _ORIGIN + item["path"] if item.get("path") else None

    def pdf_url(self, item):
        return _ORIGIN + item["downloadPath"] if item.get("downloadPath") else None

    def normalize(self, item):
        native = item.get("id")
        return {
            "id": self.report_id(native),
            "source": self.key,
            "source_native_id": native,
            "title": item.get("title"),
            "distributionHeadline": item.get("distributionHeadline"),
            "publicationDateTime": item.get("publicationDateTime"),
            "authors": item.get("authors"),
            "synopsis": item.get("synopsis"),
            "reportTypes": item.get("reportTypes"),
            "totalPages": item.get("totalPages"),
        }
