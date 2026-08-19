"""sources/jpm.py - J.P. Morgan Markets research adapter (source key: jpm).

Feed: a single JSON list at /mcp-home/api/data-service/feed-content?type=research (served
as text/plain but it's JSON). Items are ResearchDocuments; the doc id (e.g. GPS-5407159-0)
addresses both the HTML article (/research/content/<id>) and the PDF
(/research/PubServlet?forcePdf=1&action=open&doc=<id>). Ids are namespaced 'jpm:<id>'.

Host/paths come from env (masked in this public repo): set JPM_ORIGIN in your local .env.
"""
import os
from datetime import datetime, timezone

from sources.base import Source

_ORIGIN = os.environ.get("JPM_ORIGIN") or "https://REDACTED.example.com"
# Warm at the origin root (or JPM_HOME): it loads the authenticated app when logged in and
# redirects to login when not. /mcp-home is a deep path that 404s for a logged-out session.
_HOME = os.environ.get("JPM_HOME") or os.environ.get("JPM_MYCONTENT") or _ORIGIN
_FEED_BASE = os.environ.get("JPM_FEED_BASE") or f"{_ORIGIN}/mcp-home/api/data-service/feed-content"


class JPMorgan(Source):
    key = "jpm"
    label = "J.P. Morgan"
    id_prefix = "jpm"

    def warm_url(self):
        return _HOME

    def feed_url(self, offset, limit):
        # The feed returns the latest research in one payload; no offset/limit params.
        return f"{_FEED_BASE}?type=research"

    def fetch_items(self, page, offset, limit):
        # Single-page feed: only fetch offset 0, so daily.py's pagination loop stops cleanly.
        if offset:
            return []
        return super().fetch_items(page, offset, limit)

    def items_from_feed(self, data):
        try:
            results = (data["Data"]["feedResearch"]["data"]["researchService"]
                       ["research"]["results"])
            return results or []
        except (KeyError, TypeError):
            return []

    def native_id(self, item):
        return item.get("id")

    def pubdate_ms(self, item):
        s = (item.get("publicationDate") or "").strip()
        if not s:
            return None
        # e.g. "Thu Aug 13 22:09:08 UTC 2026" (always UTC in the feed).
        try:
            dt = datetime.strptime(s.replace("UTC ", ""), "%a %b %d %H:%M:%S %Y")
            return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return None

    def content_url(self, item):
        native = self.native_id(item)
        return f"{_ORIGIN}/research/content/{native}" if native else None

    def pdf_url(self, item):
        native = self.native_id(item)
        if not native:
            return None
        formats = item.get("documentFormats") or []
        has_pdf = any((f or {}).get("mimeType") == "application/pdf" for f in formats)
        if formats and not has_pdf:
            return None
        return f"{_ORIGIN}/research/PubServlet?forcePdf=1&action=open&doc={native}"

    def normalize(self, item):
        native = item.get("id")
        analysts = ((item.get("analysts") or {}).get("results")) or []
        authors = [a.get("displayName") for a in analysts if a.get("displayName")]
        bg = (item.get("businessGroup") or {}).get("displayName")
        rtypes = [x for x in (item.get("productCategory"), bg) if x] + (item.get("regions") or [])
        return {
            "id": self.report_id(native),
            "source": self.key,
            "source_native_id": native,
            "title": item.get("title"),
            "distributionHeadline": item.get("subtitle"),
            "publicationDateTime": self.pubdate_ms(item),
            "authors": authors,
            "synopsis": item.get("shortAbstract") or item.get("synopsis"),
            "reportTypes": rtypes,
            "totalPages": item.get("pageCount"),
        }
