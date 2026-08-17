"""sources/db.py - Deutsche Bank Research adapter (source key: db).

DB Research is an IHS Markit white-label. There is no JSON feed for the report list; the
homepage server-renders ~6 "featured research" cards, parsed here for rid + title + date
(the date is encoded in the card's featured-image URL). Login is a one-time email-verified
registration that then persists via cookie, so it runs unattended after a one-time recon
login. Body text comes from the PDF (the Document page is a JS shell).

PDF is a two-step token flow (both same-origin):
  api/1.0/file/<client>-<rid>/validate  -> {fileName, token}
  namedFileProxy/<client>-<rid>/<fileName>?filetoken=<token>  -> the PDF
Ids namespaced 'db:<rid>'. Host + client id come from env (masked): set DB_ORIGIN in .env.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

from sources.base import Source

_ORIGIN = os.environ.get("DB_ORIGIN") or "https://REDACTED.example.com"
_CLIENT = os.environ.get("DB_CLIENT") or "2795"

# report card: <h4 class="media-heading"> <a ... href="/research/Article?rid=<rid>...>TITLE</a>
_CARD_RE = re.compile(
    r'<h4 class="media-heading">\s*<a\s+[^>]*href="/research/Article\?rid=([^"&]+)[^>]*>(.*?)</a>',
    re.I | re.S)
# featured image URL encodes rid + YYYYMMDD (rid there uses '_' where the href uses '-')
_FEAT_RE = re.compile(r'featured/2795-([A-Za-z0-9_]+)-(\d{8})/image')
_TAG_RE = re.compile(r'<[^>]+>')


class DeutscheBank(Source):
    key = "db"
    label = "Deutsche Bank"
    id_prefix = "db"

    def warm_url(self):
        return f"{_ORIGIN}/research"

    def fetch_items(self, page, offset, limit):
        if offset:
            return []
        html = page.evaluate(
            """async (u) => { const r = await fetch(u, {credentials:'include'}); return await r.text(); }""",
            f"{_ORIGIN}/research") or ""
        dates = {rid.replace("_", "-"): d for rid, d in _FEAT_RE.findall(html)}
        items, seen = [], set()
        for rid, title in _CARD_RE.findall(html):
            if rid in seen:
                continue
            seen.add(rid)
            items.append({"rid": rid, "title": _TAG_RE.sub("", title).strip(),
                          "date": dates.get(rid)})
        if not items and ("SubmitEmail" in html or "/research/Register" in html
                          or "Register" in (page.url or "")):
            raise RuntimeError("feed returned HTML (session expired / not logged in)")
        return items

    def native_id(self, item):
        return item.get("rid")

    def pubdate_ms(self, item):
        d = item.get("date")  # YYYYMMDD
        if not d:
            return None
        try:
            return int(datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return None

    def content_url(self, item):
        return None  # the Document page is a JS shell; body text comes from the PDF

    def pdf_url(self, item):
        rid = self.native_id(item)
        if not rid:
            return None
        # The file endpoint keys on the underscore form of the rid (ULIDs have no separators
        # so are unchanged; UUID-style rids use '_' where the Article href uses '-').
        file_rid = rid.replace("-", "_")
        return f"{_ORIGIN}/research/api/1.0/file/{_CLIENT}-{file_rid}/validate?_={int(time.time()*1000)}"

    def fetch_pdf(self, page, url):
        res = page.evaluate(
            """async (u) => { const r = await fetch(u, {credentials:'include'});
                return { ok:r.ok, status:r.status, body: await r.text() }; }""", url)
        if not res.get("ok"):
            raise RuntimeError(f"validate {res.get('status')}")
        data = (json.loads(res.get("body") or "{}") or {}).get("data") or {}
        fn, tok = data.get("fileName"), data.get("token")
        if not (fn and tok):
            raise RuntimeError("no file token in validate response")
        rid = url.split(f"/{_CLIENT}-", 1)[1].split("/validate", 1)[0]
        pdf = (f"{_ORIGIN}/research/namedFileProxy/{_CLIENT}-{rid}/{quote(fn)}"
               f"?filetoken={quote(tok, safe='')}")
        return super().fetch_pdf(page, pdf)

    def normalize(self, item):
        rid = item.get("rid")
        return {
            "id": self.report_id(rid),
            "source": self.key,
            "source_native_id": rid,
            "title": item.get("title"),
            "distributionHeadline": None,
            "publicationDateTime": self.pubdate_ms(item),
            "authors": [],
            "synopsis": None,
            "reportTypes": [],
            "totalPages": None,
        }
