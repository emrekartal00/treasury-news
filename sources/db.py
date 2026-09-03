"""sources/db.py - Deutsche Bank Research adapter (source key: db).

DB Research is an IHS Markit white-label. PRIMARY enumeration is the portal's "Latest" list
API - the full research stream behind the /research/Research/Latest page, NOT just the 6
homepage featured cards:
  GET api/1.0/research/latest?includeFacets=false&itemsPerPage=<=20&sortBy=date&sortOrder=desc&startIndex=N
  -> {"data":{"count":..., "items":[{documentKey, title, dateAsOf, abstract/synopsis,
      analysts, region, topics, periodicalName, productType, pageCount}, ...]}}
`itemsPerPage` is capped at 20 server-side, so we page it in chunks of 20 via startIndex.
Each item's `documentKey` is '<client>-<rid>-<YYYYMMDD>' (rid in underscore form); we parse it
back to the dash-form rid + date the rest of this adapter already uses. The homepage
featured-card parse is kept only as a fallback if the Latest API is unavailable.

Login is a one-time email-verified registration that then persists via cookie, so it runs
unattended after a one-time recon login. Body text comes from the PDF (the Document page is a
JS shell). PDF is a two-step token flow (both same-origin):
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
_LATEST_MAX = 20  # server caps itemsPerPage at 20; we chunk startIndex to fill bigger windows

# documentKey = '<client>-<rid>-<YYYYMMDD>' (rid uses '_' where the Article href uses '-')
_DOCKEY_RE = re.compile(r"^(\d+)-(.+)-(\d{8})$")

# --- homepage featured cards (fallback only) ---
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

    # ------------------------------------------------------------- Latest API (primary)
    def _latest_url(self, start, per_page):
        return (f"{_ORIGIN}/research/api/1.0/research/latest?includeFacets=false"
                f"&itemsPerPage={per_page}&sortBy=date&sortOrder=desc&startIndex={start}"
                f"&_={int(time.time() * 1000)}")

    def fetch_items(self, page, offset, limit):
        # daily.py asks for the window [offset, offset+limit). The API caps itemsPerPage at
        # _LATEST_MAX, so fill the window with chunked calls (no overlap with the next page).
        want = limit or 30
        got, start, auth_fail = [], offset, None
        while len(got) < want:
            n = min(_LATEST_MAX, want - len(got))
            res = page.evaluate(
                """async (u) => {
                    const r = await fetch(u, { credentials: 'include',
                        headers: { 'Accept': 'application/json' } });
                    return { ok: r.ok, status: r.status, body: await r.text() };
                }""", self._latest_url(start, n))
            body = res.get("body") or ""
            if body.lstrip()[:1] == "<":
                auth_fail = "feed returned HTML (session expired / not logged in)"
                break
            if not res.get("ok"):
                auth_fail = f"latest {res.get('status')}"
                break
            batch = ((json.loads(body).get("data") or {}).get("items")) or []
            for it in batch:
                parsed = self._from_latest(it)
                if parsed:
                    got.append(parsed)
            if len(batch) < n:
                break  # ran out of results
            start += len(batch)
        if got:
            return got
        # Fall back to the homepage featured cards only for the first page.
        if offset == 0:
            return self._homepage_items(page)
        if auth_fail and "expired" in auth_fail:
            raise RuntimeError(auth_fail)
        return []

    def _from_latest(self, it):
        """Map a Latest-API item to this adapter's neutral item (rid/date + rich metadata)."""
        m = _DOCKEY_RE.match(str(it.get("documentKey") or ""))
        if not m:
            return None
        rid = m.group(2).replace("_", "-")  # dash form (matches the Article-href form)
        return {
            "rid": rid,
            "date": m.group(3),  # YYYYMMDD
            "dateAsOf": it.get("dateAsOf"),
            "title": (it.get("title") or "").strip(),
            "synopsis": it.get("synopsis") or it.get("abstract"),
            "analysts": it.get("analysts"),
            "region": it.get("region"),
            "pageCount": it.get("pageCount"),
        }

    # ------------------------------------------------------- homepage cards (fallback)
    def _homepage_items(self, page):
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
        s = item.get("dateAsOf")  # ISO 8601 (Latest API); more precise than the YYYYMMDD
        if s:
            try:
                return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                pass
        d = item.get("date")  # YYYYMMDD (homepage cards / documentKey)
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

    @staticmethod
    def _authors(item):
        out = []
        for a in item.get("analysts") or []:
            if isinstance(a, str):
                name = a.strip()
            elif isinstance(a, dict):
                name = (a.get("name") or a.get("displayName") or a.get("fullName") or "").strip()
            else:
                name = ""
            if name and name not in out:
                out.append(name)
        return out

    def normalize(self, item):
        rid = item.get("rid")
        region = item.get("region")
        rtypes = [region] if isinstance(region, str) and region else []
        return {
            "id": self.report_id(rid),
            "source": self.key,
            "source_native_id": rid,
            "title": item.get("title"),
            "distributionHeadline": None,
            "publicationDateTime": self.pubdate_ms(item),
            "authors": self._authors(item),
            "synopsis": item.get("synopsis"),
            "reportTypes": rtypes,
            "totalPages": item.get("pageCount"),
        }
