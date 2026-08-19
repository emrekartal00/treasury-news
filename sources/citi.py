"""sources/citi.py - Citi Velocity research adapter (source key: citi).

Feed: POST /marketbuzz-publication-service/publication/init?portletId=<id> with a portlet
config body (sources=["Research"]); the response's docs[] carry pubId (the doc id),
headline, timeInMilliseconds, authorName, summary, and a PDF. Video/commentary tiles (no
pubId) are filtered out. Ids are namespaced 'citi:<pubId>'.

The article HTML is a client-rendered SPA needing a session-specific token, so we skip HTML
entirely and let ingest.py's PDF-text fallback read the body. PDF comes from the same-origin
print endpoint (the feed's docUrl points at a different cookie domain, so we avoid it).

Host comes from env (masked in this public repo): set CITI_ORIGIN in your local .env.
"""
import json
import os

from sources.base import Source

_ORIGIN = os.environ.get("CITI_ORIGIN") or "https://REDACTED.example.com"
_PORTLET = os.environ.get("CITI_PORTLET") or "CV_CC_Publication_1627292624187"
_DISPLAY_COUNT = os.environ.get("CITI_DISPLAY_COUNT") or "50"


class Citi(Source):
    key = "citi"
    label = "Citi"
    id_prefix = "citi"

    def warm_url(self):
        return f"{_ORIGIN}/cv2/"

    def feed_url(self, offset, limit):
        return f"{_ORIGIN}/marketbuzz-publication-service/publication/init?portletId={_PORTLET}"

    def _feed_config(self):
        return {"config": {"config": {
            "portletId": _PORTLET, "content": "Publication", "hasTimestamp": True,
            "displayCount": str(_DISPLAY_COUNT), "hasDescription": True, "hasVideo": False,
            "datePeriod": {"fromDay": "", "range": "", "toDay": ""},
            "filters": [
                {"value": [], "multiple": True, "status": False, "name": "products", "isCustomized": False},
                {"value": ["Research"], "multiple": True, "status": False, "name": "sources", "isCustomized": False},
                {"value": [], "multiple": True, "status": False, "name": "regions", "isCustomized": False},
                {"value": [], "multiple": True, "status": False, "name": "sectors", "isCustomized": False},
                {"value": ["Date", "Relevancy"], "multiple": False, "status": False, "name": "sortBy", "isCustomized": False},
            ],
            "filterLogic": "And", "hasSearch": False, "showFilters": False, "showSorts": False,
            "uiColumnCount": 2, "refreshInterval": 0, "contentFilterType": "widget",
            "itemList": [], "hasMore": False, "viewType": "Medium Grid", "title": "Trending",
            "search": {"params": {}},
        }, "signal": "{}"}, "publicationRequest": {"portletId": _PORTLET}}

    def fetch_items(self, page, offset, limit):
        # Single-page portlet feed (POST); no offset pagination.
        if offset:
            return []
        res = page.evaluate(
            """async ({url, body}) => {
                const r = await fetch(url, {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                return { ok: r.ok, status: r.status, body: await r.text() };
            }""",
            {"url": self.feed_url(offset, limit), "body": self._feed_config()})
        if not res["ok"]:
            raise RuntimeError(f"feed {res['status']}")
        data = json.loads(res["body"]) if res["body"] else {}
        return self.items_from_feed(data)

    def items_from_feed(self, data):
        docs = (data or {}).get("docs") or []
        # Keep research documents (have a pubId + a PDF); drop video/commentary tiles.
        return [d for d in docs
                if d.get("pubId") and (d.get("fileType") == "PDF" or d.get("docUrl"))]

    def native_id(self, item):
        return item.get("pubId")

    def pubdate_ms(self, item):
        return item.get("timeInMilliseconds")

    def content_url(self, item):
        return None  # HTML is a client-rendered SPA; rely on the PDF for body text

    def pdf_url(self, item):
        pub = self.native_id(item)
        if not pub:
            return None
        return f"{_ORIGIN}/rendition/eppublic/uiservices/print?doc_id={pub}&type=download&isJP=false"

    def normalize(self, item):
        pub = item.get("pubId")
        authors = [a.strip() for a in (item.get("authorName") or "").split(",") if a.strip()]
        rtypes = (item.get("assetClasses") or []) + (item.get("regionList") or [])
        pages = item.get("pageCountWithoutDisc") or None
        return {
            "id": self.report_id(pub),
            "source": self.key,
            "source_native_id": pub,
            "title": item.get("headline"),
            "distributionHeadline": None,
            "publicationDateTime": self.pubdate_ms(item),
            "authors": authors,
            "synopsis": item.get("summary"),
            "reportTypes": rtypes,
            "totalPages": pages,
        }
