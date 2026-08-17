"""sources/barc.py - Barclays research adapter (source key: barc).

Feed: GET /RSX/content-archive/v1/REST/publication/subscriptions (the user's followed
research grid, newest first). Each publication carries pubId, basicInfo.releasedDateTime,
and a documents[] list; the PRIMARY application/pdf document gives a docId that addresses
the PDF at /PRC/servlets/dv.search?docID=<docId>. Ids are namespaced 'barc:<pubId>'.

The subscription grid is scoped by the account's product groups, so BARC_PRODUCT_GROUPS
(a comma-separated id list, account-specific) is set in the local .env. The article HTML is
a client-rendered SPA, so we skip HTML and let ingest.py's PDF-text fallback read the body.

Host + product groups come from env (masked in this public repo).
"""
import os
import re
from datetime import datetime, timezone

from sources.base import Source

# URL markers that mean we're still in the PingFederate login/SSO chain, not the app.
_LOGIN_URL = re.compile(r"logon|user_logon|authorization\.ping|/as/", re.I)

_ORIGIN = os.environ.get("BARC_ORIGIN") or "https://REDACTED.example.com"
_GROUPS = os.environ.get("BARC_PRODUCT_GROUPS") or ""
_RANGE = os.environ.get("BARC_PUB_DATE_RANGE") or "1+month"
_PAGE_SIZE = os.environ.get("BARC_PAGE_SIZE") or "30"


class Barclays(Source):
    key = "barc"
    label = "Barclays"
    id_prefix = "barc"

    def warm_url(self):
        # Bare origin: when unauthenticated it top-level-redirects to the PingFederate login
        # page (so login() can see + fill it); when authenticated it redirects to the app.
        # (/BU/?rce=home is the SPA shell that handles auth internally without a URL change.)
        return _ORIGIN

    def warm(self, page, nav_timeout, warm_ms):
        # Barclays' PingFederate session does NOT persist across browser launches, so a fresh
        # run lands on the login page. Give the SPA time to settle; authentication itself is
        # handled interactively (daily.py --login) in the same session.
        try:
            page.goto(self.warm_url(), wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception as exc:
            print("  (goto note:", exc, ")")
        try:
            page.wait_for_load_state("networkidle", timeout=nav_timeout)
        except Exception:
            pass
        page.wait_for_timeout(max(warm_ms, 10000))

    def login(self, page, nav_timeout):
        """Scripted PingFederate 'basic' logon (no 2FA): fill user/pass and submit, in this
        same session, so the run authenticates unattended. Needs BARC_USER + BARC_PASS in env;
        without them this is a no-op and you fall back to `daily.py --login`. The page encrypts
        the password client-side, so we drive the real form (fill + click), never a raw POST."""
        user = os.environ.get("BARC_USER")
        pw = os.environ.get("BARC_PASS")
        if not (user and pw):
            return False
        print(f"  [barc] login: page is {page.url[:75]}")
        # Detect the login by a real password field OR a login URL (the app shell can show a
        # "enable JavaScript" / loading state without changing the top URL).
        try:
            page.wait_for_selector("input[name=password]", timeout=20000)
        except Exception:
            pass
        has_pwd = False
        try:
            has_pwd = bool(page.query_selector("input[name=password]"))
        except Exception:
            pass
        if not (has_pwd or _LOGIN_URL.search(page.url or "")):
            print("  [barc] login: no login form found; assuming already authenticated.")
            return False
        # A OneTrust cookie banner can overlay the form - dismiss it if present.
        for sel in ("#onetrust-accept-btn-handler", "#accept-recommended-btn-handler"):
            try:
                if page.query_selector(sel):
                    page.click(sel, timeout=3000)
                    print("  [barc] login: dismissed cookie banner")
                    break
            except Exception:
                pass
        try:
            page.wait_for_selector("input[name=password]", timeout=45000)
        except Exception:
            print("  [barc] login: password field never appeared (timeout).")
            return False
        print("  [barc] login: form found; filling credentials + submitting...")
        try:
            page.check("#standardLogin", timeout=3000)  # standard (non-employee) logon
        except Exception:
            pass
        page.fill("input[name=user]", user)
        page.fill("input[name=password]", pw)
        try:
            page.check("#rememberLogin", timeout=3000)
        except Exception:
            pass
        try:
            page.click("#submit", timeout=5000)
        except Exception:
            page.press("input[name=password]", "Enter")  # fallback
        # Wait for the SSO chain to land back on the app.
        for _ in range(20):
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            if not _LOGIN_URL.search(page.url or ""):
                break
            page.wait_for_timeout(1500)
        print(f"  [barc] login: post-submit page is {page.url[:75]}")
        page.wait_for_timeout(6000)
        return True

    def feed_url(self, offset, limit):
        params = ("responseDetailLevel=STANDARD&pubLang=user_profile"
                  f"&pubDateRange={_RANGE}&pageSize={_PAGE_SIZE}"
                  "&bclDestination.code=RESEARCH_PUB_GRID")
        if _GROUPS:
            params += f"&productGroupId={_GROUPS}"
        return f"{_ORIGIN}/RSX/content-archive/v1/REST/publication/subscriptions?{params}"

    def fetch_items(self, page, offset, limit):
        if offset:  # single-page grid; no offset pagination
            return []
        return super().fetch_items(page, offset, limit)

    def items_from_feed(self, data):
        try:
            return (data["data"]["publications"]) or []
        except (KeyError, TypeError):
            return []

    def _primary_pdf(self, item):
        docs = item.get("documents") or []
        for d in docs:  # prefer the PRINT/PRIMARY PDF
            if d.get("mediaType") == "application/pdf" and d.get("channel") == "PRINT":
                return d
        for d in docs:
            if d.get("mediaType") == "application/pdf":
                return d
        return None

    def native_id(self, item):
        return (item.get("basicInfo") or {}).get("pubId")

    def pubdate_ms(self, item):
        bi = item.get("basicInfo") or {}
        iso = bi.get("releasedDateTime")
        if iso:
            try:
                return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                pass
        day = bi.get("pubDate")
        if day:
            try:
                return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
            except ValueError:
                pass
        return None

    def _title(self, item):
        d = self._primary_pdf(item) or {}
        titles = d.get("titles") or []
        pub = next((t.get("value") for t in titles if t.get("type") == "PUBLICATION"), None)
        return pub or next((t.get("value") for t in titles), None)

    def content_url(self, item):
        return None  # HTML is a client-rendered SPA; rely on the PDF for body text

    def pdf_url(self, item):
        d = self._primary_pdf(item)
        if not d or not d.get("docId"):
            return None
        return f"{_ORIGIN}/PRC/servlets/dv.search?docID={d['docId']}"

    def normalize(self, item):
        pub = self.native_id(item)
        return {
            "id": self.report_id(pub),
            "source": self.key,
            "source_native_id": pub,
            "title": self._title(item),
            "distributionHeadline": None,
            "publicationDateTime": self.pubdate_ms(item),
            "authors": [],
            "synopsis": None,
            "reportTypes": [],
            "totalPages": None,
        }
