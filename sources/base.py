"""sources/base.py - Source adapter interface + shared defaults.

A Source encapsulates everything portal-specific: how to warm the session, fetch the
list/feed, map a raw feed item to our neutral meta schema, and build the content/PDF URLs.
daily.py owns the shared download loop and only calls these hooks, so most adapters need
to override just a handful of methods.

The defaults replicate the original GS behavior discovered earlier:
  - the feed is JSON, fetched INSIDE the page (Chrome's stack -> inherits the corporate
    proxy + logged-in cookies; Playwright's own request client bypasses the proxy);
  - report HTML needs a real navigation to authorize the content route;
  - the PDF is fetched in-page too and returned as base64.

report_id policy: GS keeps its bare native UUID (rows are already keyed that way). Every
other source namespaces as '<id_prefix>:<native>' so ids never collide across portals. The
column is VARCHAR2(64); if a namespaced id would overflow, we fall back to
'<id_prefix>:' + a short blake2 hash of the native id.
"""
import base64
import hashlib
from datetime import datetime, timezone

import config  # noqa: F401  # importing config loads .env into os.environ


class Source:
    key = "base"            # short stable id, e.g. "gs", "jpm"; also the DB source value
    label = "Base"          # human label for the UI
    id_prefix = None        # None -> bare native id; otherwise '<id_prefix>:<native>'
    # When True, an item is marked seen even if the content fetch came back empty (as long as
    # it didn't error). Use for portals whose feed mixes in text-less items (e.g. MS videos/
    # calendars) so they aren't re-attempted every run. Default keeps the retry-on-empty.
    mark_seen_on_empty = False

    # ----------------------------------------------------------- session / feed
    def warm_url(self):
        """Landing/'My Content' URL to navigate to so the SPA authorizes content routes."""
        raise NotImplementedError

    def warm(self, page, nav_timeout, warm_ms):
        """Navigate to warm_url so the session/SPA authorizes API + content routes.
        Override when a portal needs longer to complete an on-load auth handshake."""
        try:
            page.goto(self.warm_url(), wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception as exc:
            print("  (goto note:", exc, ")")
        page.wait_for_timeout(warm_ms)

    def login(self, page, nav_timeout):
        """Optionally perform a SCRIPTED login in this same browser session, for portals
        whose session does not persist across launches. Return True if a login was attempted.
        Default: no-op (cookie-based portals stay logged in via the persistent profile)."""
        return False

    def feed_url(self, offset, limit):
        """URL of the JSON list/feed endpoint (used by the default fetch_items)."""
        raise NotImplementedError

    def fetch_items(self, page, offset, limit):
        """Return a list of raw feed items. Default: GET feed_url from inside the page."""
        res = page.evaluate(
            """async (u) => {
                const r = await fetch(u, { credentials: 'include' });
                return { status: r.status, ok: r.ok, body: await r.text() };
            }""",
            self.feed_url(offset, limit),
        )
        if not res["ok"]:
            raise RuntimeError(f"feed {res['status']} at offset {offset}")
        import json
        body = res["body"] or ""
        if body.lstrip()[:1] == "<":
            raise RuntimeError("feed returned HTML (session expired / not logged in)")
        data = json.loads(body) if body else []
        return self.items_from_feed(data)

    def items_from_feed(self, data):
        """Pull the list of items out of a parsed feed payload (shape varies per portal)."""
        if isinstance(data, list):
            return data
        return data.get("results") or data.get("items") or []

    # ----------------------------------------------------------------- per-item
    def native_id(self, item):
        """The portal's own id for an item (used for dedupe / seen-state)."""
        return item.get("id")

    def pubdate_ms(self, item):
        """Publication time as epoch milliseconds, or None."""
        return item.get("publicationDateTime")

    def date(self, item):
        """YYYY-MM-DD used for the downloads/<key>/<date>/ folder and publication_date."""
        ms = self.pubdate_ms(item)
        dt = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms
              else datetime.now(timezone.utc))
        return dt.strftime("%Y-%m-%d")

    def content_url(self, item):
        """Absolute URL of the report HTML page, or None."""
        return None

    def pdf_url(self, item):
        """Absolute URL of the report PDF, or None."""
        return None

    def normalize(self, item):
        """Map a raw feed item -> the neutral meta dict that ingest.py consumes.

        Must set at least: id (namespaced via self.report_id), source, title. daily.py adds
        the runtime fields (htmlUrl/pdfUrl/htmlBytes/pdf/fetchedAt/date) afterwards."""
        raise NotImplementedError

    # ------------------------------------------------- downloads (shared defaults)
    def fetch_html(self, page, url, nav_timeout):
        page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
        try:
            page.wait_for_function(
                "() => document.title && !/login/i.test(document.title) "
                "&& document.body.innerText.length > 500",
                timeout=45000,
            )
        except Exception:
            pass
        page.wait_for_timeout(2000)
        html = ""
        for _ in range(5):
            try:
                html = page.content()
            except Exception:
                html = ""
            if html:
                break
            page.wait_for_timeout(1500)
        return html

    def fetch_pdf(self, page, url):
        """Return (bytes, http_status). In-page fetch inherits proxy + cookies."""
        res = page.evaluate(
            """async (u) => {
                const r = await fetch(u, { credentials: 'include' });
                const buf = new Uint8Array(await r.arrayBuffer());
                let bin = ''; const CH = 0x8000;
                for (let i = 0; i < buf.length; i += CH) {
                    bin += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
                }
                return { status: r.status, b64: btoa(bin) };
            }""",
            url,
        )
        return base64.b64decode(res["b64"]), res["status"]

    # ----------------------------------------------------------------- id helper
    def report_id(self, native):
        native = str(native or "")
        if not self.id_prefix:
            return native[:64]
        rid = f"{self.id_prefix}:{native}"
        if len(rid) <= 64:
            return rid
        h = hashlib.blake2b(native.encode("utf-8"), digest_size=12).hexdigest()
        return f"{self.id_prefix}:{h}"
