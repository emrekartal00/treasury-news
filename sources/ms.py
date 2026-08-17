"""sources/ms.py - Morgan Stanley Matrix research adapter (source key: ms).

Matrix is a heavy Angular portal (the /eqr/ app). There is no flat "latest research"
API for an unentitled feed, so the report list is aggregated from the curated GLOBAL homepage
sections (content/Home + content/auto/Home); each card carries the uuid, headline, ISO date,
authors, and abstract. Everything is addressable by the report uuid:
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

    def _feed_urls(self):
        base = f"{_ORIGIN}{_CONTENT}"
        return [
            f"{base}/content/Home?entityType=REGION&entityId={_REGION}&language=EN",
            f"{base}/content/auto/Home?entityType=REGION&entityId={_REGION}&language=EN&reportLanguages=EN",
        ]

    def feed_url(self, offset, limit):
        return self._feed_urls()[0]

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

    def fetch_items(self, page, offset, limit):
        if offset:  # curated homepage; no offset pagination
            return []
        by_id = {}
        errors = 0
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
