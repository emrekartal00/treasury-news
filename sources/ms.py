"""sources/ms.py - Morgan Stanley Matrix research adapter (source key: ms).

Matrix is a heavy Angular portal (the /eqr/ app). PRIMARY enumeration is the portal's own
match-all research SEARCH feed (portal-content-service/search) sorted by date - the MS
equivalent of a "latest research" stream, covering the full entitled catalogue (hundreds of
reports/day across all regions/sectors), not just the curated homepage. The homepage
aggregation (content/Home + content/auto/Home) is kept only as a fallback if search fails.

Both the search cards and the homepage cards carry the same fields (id/hl/pd/ab/a/co), so a
single normalize()/content_url()/pdf_url() handles either. Everything is addressable by the
report uuid:
  - body text: /eqr/article/webapp/services/published/article/sections?uuid=<uuid> returns the
    article as HTML inside JSON (same-origin fetch), stitched for ingest.py to summarize.
  - PDF: frontmatter?uuid=<uuid> exposes a same-origin `pdfRenditionUrl` (carries the
    per-report cobaltId); fetch_pdf() resolves it then downloads the official PDF.
Ids are namespaced 'ms:<uuid>'. Host comes from env (masked): set MS_ORIGIN in the local .env.
"""
import json
import os
import re
from datetime import datetime

from sources.base import Source

# Homepage cards mix in media/non-article content that has no text sections - skip those.
_SKIP_TITLE = re.compile(r"^\s*(video|audio|podcast|replay)\b", re.I)
# The match-all search feed also surfaces Excel financial MODELS ('Regular Update' cards:
# pcat1='Model', dt='application/xls'). They have no article body and no PDF (frontmatter
# 500s), so they'd only waste the daily budget - identify and skip them by format.
_MODEL_FORMATS = {"xls", "xlsx", "xlsm", "csv"}

_ORIGIN = os.environ.get("MS_ORIGIN") or "https://REDACTED.example.com"
_CONTENT = "/eqr/research/webapp/portalservices/portal-content-service"
_ARTICLE = "/eqr/article/webapp/services/published/article"
_REGION = os.environ.get("MS_REGION") or "GLOBAL"
_SECTION_END = os.environ.get("MS_SECTION_END") or "60"  # sections range end (not lazy-loaded)


class MorganStanley(Source):
    key = "ms"
    label = "Morgan Stanley"
    id_prefix = "ms"
    mark_seen_on_empty = True  # feed mixes in text-less items (calendars) - don't retry them

    def warm_url(self):
        return f"{_ORIGIN}/eqr/research/portal/home"

    # ------------------------------------------------------------- search feed (primary)
    def _search_url(self):
        return f"{_ORIGIN}{_CONTENT}/search"

    def _search_body(self, page_no, size):
        # Discovered from the portal's own request: '(text==*)' is match-all, sort 'd' = date
        # desc. invokeAskResearch is disabled (we don't want the AI side-effect); userJourneyId
        # is a fixed nil-uuid (the server does not validate it for the results payload).
        return {
            "compositeRequest": {
                "search": "(text==*)", "sort": "d", "noSearch": False, "gn": False,
                "didyoumean": False, "countMode": "best", "showcard": True,
                "size": size, "page": page_no,
            },
            "arRequest": {
                "skipSpellCheck": True,
                "userJourneyId": "00000000-0000-0000-0000-000000000000",
                "invokeAskResearch": False, "dateFilter": "",
                "filtersMap": {"queryWithoutStopwords": ""},
            },
        }

    def fetch_items(self, page, offset, limit):
        # daily.py paginates by stepping `offset` by `limit`; map that to the search feed's
        # 1-based `page` with `size == limit` so the windows line up exactly.
        size = limit or 30
        page_no = (offset // size) + 1
        res = page.evaluate(
            """async ({u, b}) => {
                const r = await fetch(u, { method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
                return { ok: r.ok, status: r.status, body: await r.text() };
            }""", {"u": self._search_url(), "b": self._search_body(page_no, size)})
        body = res.get("body") or ""
        if body.lstrip()[:1] == "<":
            raise RuntimeError("feed returned HTML (session expired / not logged in)")
        if not res.get("ok"):
            # Fall back to the curated homepage on the first page only (keeps us running if
            # the search service is briefly unavailable); deeper pages just stop.
            if page_no == 1:
                return self._homepage_items(page)
            raise RuntimeError(f"search {res.get('status')} at page {page_no}")
        data = json.loads(body) if body else {}
        cards = (((data or {}).get("rcsResponse") or {}).get("reportcards")) or []
        return [c for c in cards if self._is_article(c)]

    @staticmethod
    def _is_article(card):
        """Keep only real articles: has an id, not a media title, not a spreadsheet model."""
        if not card.get("id") or _SKIP_TITLE.match(card.get("hl") or ""):
            return False
        af = (card.get("af") or "").lower()
        dt = (card.get("dt") or "").lower()
        if af in _MODEL_FORMATS or "xls" in dt or "excel" in dt or "spreadsheet" in dt:
            return False
        return True

    # ------------------------------------------------------- curated homepage (fallback)
    def _feed_urls(self):
        base = f"{_ORIGIN}{_CONTENT}"
        return [
            f"{base}/content/Home?entityType=REGION&entityId={_REGION}&language=EN",
            f"{base}/content/auto/Home?entityType=REGION&entityId={_REGION}&language=EN&reportLanguages=EN",
        ]

    def _get_json(self, page, url):
        res = page.evaluate(
            """async (u) => {
                const r = await fetch(u, { credentials: 'include' });
                return { ok: r.ok, status: r.status, body: await r.text() };
            }""", url)
        body = res.get("body") or ""
        if body.lstrip()[:1] == "<":
            raise RuntimeError("feed returned HTML (session expired / not logged in)")
        if not res.get("ok"):
            raise RuntimeError(f"feed {res.get('status')}")
        return json.loads(body) if body else None

    def _homepage_items(self, page):
        by_id = {}
        errors = 0
        last = None
        for url in self._feed_urls():
            try:
                data = self._get_json(page, url)
            except Exception as exc:
                errors += 1
                last = exc
                continue
            for card in self._cards(data):
                rid = card.get("id")
                if rid and rid not in by_id:
                    by_id[rid] = card
        if not by_id and errors:
            raise last  # surface the (auth) error so daily.py retries
        return list(by_id.values())

    def _cards(self, data):
        """Flatten REPORT cards out of the section -> sectionContentList -> cardList tree."""
        out = []
        for section in (data or []):
            for sc in section.get("sectionContentList") or []:
                for card in sc.get("cardList") or []:
                    if card.get("type") == "REPORT":
                        d = card.get("reportCardDetail")
                        if d and d.get("id") and not _SKIP_TITLE.match(d.get("hl") or ""):
                            out.append(d)
        return out

    def native_id(self, item):
        return item.get("id")

    def pubdate_ms(self, item):
        s = item.get("pd")  # e.g. "2026-08-13T11:00:12.000Z"
        if not s:
            return None
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None

    def _authors(self, item):
        names = []
        a = item.get("a") or {}
        if a.get("n"):
            names.append(a["n"].strip())
        for c in item.get("co") or []:
            n = (c.get("n") or "").strip()
            if n and n not in names:
                names.append(n)
        return names

    def content_url(self, item):
        rid = self.native_id(item)
        return f"{_ORIGIN}{_ARTICLE}/sections?uuid={rid}&start=1&end={_SECTION_END}" if rid else None

    def fetch_html(self, page, url, nav_timeout):
        # The sections endpoint returns JSON [{title, data(HTML)}]; stitch it into one HTML
        # document for ingest.extract_text (same-origin fetch inherits cookies + proxy).
        try:
            data = self._get_json(page, url)
        except Exception as exc:
            print(f"    (ms sections fetch failed: {exc})")
            return ""
        parts = []
        for sec in data or []:
            title = sec.get("title")
            body = sec.get("data")
            if title:
                parts.append(f"<h2>{title}</h2>")
            if body:
                parts.append(body)
        return "<html><body>" + "\n".join(parts) + "</body></html>" if parts else ""

    def pdf_url(self, item):
        # We return the frontmatter endpoint; fetch_pdf() resolves its pdfRenditionUrl (a
        # same-origin rendition URL carrying the per-report cobaltId) and downloads the PDF.
        uid = self.native_id(item)
        return f"{_ORIGIN}{_ARTICLE}/frontmatter?uuid={uid}" if uid else None

    def fetch_pdf(self, page, url):
        fm = self._get_json(page, url)
        rel = ((fm or {}).get("frontMatter") or {}).get("pdfRenditionUrl")
        if not rel:
            raise RuntimeError("no pdfRenditionUrl (not a PDF-backed report)")
        rel = rel.replace("&amp;", "&")
        pdf = rel if rel.startswith("http") else f"{_ORIGIN}{rel}"
        return super().fetch_pdf(page, pdf)

    def normalize(self, item):
        rid = item.get("id")
        return {
            "id": self.report_id(rid),
            "source": self.key,
            "source_native_id": rid,
            "title": item.get("hl"),
            "distributionHeadline": None,
            "publicationDateTime": self.pubdate_ms(item),
            "authors": self._authors(item),
            "synopsis": item.get("ab"),
            "reportTypes": [],
            "totalPages": None,
        }
