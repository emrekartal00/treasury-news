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
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import config  # noqa: F401  # importing config loads .env into os.environ


class Source:
    key = "base"            # short stable id, e.g. "gs", "jpm"; also the DB source value
    label = "Base"          # human label for the UI
    id_prefix = None        # None -> bare native id; otherwise '<id_prefix>:<native>'
    # When True, an item is marked seen even if the content fetch came back empty (as long as
    # it didn't error). Use for portals whose feed mixes in text-less items (e.g. MS videos/
    # calendars) so they aren't re-attempted every run. Default keeps the retry-on-empty.
    mark_seen_on_empty = False
    # Auth-header replay (guarded). Some SPAs add an auth header in JS (Bearer token, XSRF
    # token, client id) that a bare in-page fetch lacks -> 401 although the page is logged in.
    # When auth_capture is set, warm() starts recording the replayable headers the page ITSELF
    # sends to the feed API: same origin as feed_url, path matching auth_capture (True = the
    # feed path up to and incl. '/api/'; a str = a path regex; env <KEY>_AUTH_CAPTURE
    # overrides). The feed fetch sends them (waiting for / triggering the page's own feed call
    # first); any other fetch gets them only as a 401/403 retry, and never to another origin.
    # <KEY>_TOKEN_STORAGE_KEY (env) adds 'Authorization: Bearer <value>' read from storage
    # inside the page. <KEY>_AUTH_REPLAY=off disables all of it. Values are never printed.
    # Default None = off (bare fetch, as before).
    auth_capture = None

    _RETRY_STATUSES = (401, 403)

    # ----------------------------------------------------------- session / feed
    def warm_url(self):
        """Landing/'My Content' URL to navigate to so the SPA authorizes content routes."""
        raise NotImplementedError

    def warm(self, page, nav_timeout, warm_ms):
        """Navigate to warm_url so the session/SPA authorizes API + content routes.
        Override when a portal needs longer to complete an on-load auth handshake."""
        self.start_auth_capture(page)
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

    # ------------------------------------------------------- auth-header replay
    def _env(self, name):
        return (os.environ.get(f"{self.key.upper()}_{name}") or "").strip()

    def auth_replay_enabled(self):
        return bool(self.auth_capture) and self._env("AUTH_REPLAY").lower() != "off"

    def _feed_origin(self):
        return urlsplit(self.feed_url(0, 1)).netloc

    def _auth_path_regex(self):
        override = self._env("AUTH_CAPTURE")
        if override:
            return re.compile(override)
        if isinstance(self.auth_capture, str):
            return re.compile(self.auth_capture)
        path = urlsplit(self.feed_url(0, 1)).path
        i = path.find("/api/")
        return re.compile("^" + re.escape(path[:i + 5] if i >= 0 else path))

    def _is_auth_url(self, url):
        p = urlsplit(url)
        return p.netloc == self._auth_origin and bool(self._auth_path.search(p.path))

    def start_auth_capture(self, page):
        """Record replayable headers of the page's own feed-API requests. Non-blocking: the
        'request' handler only reads req.url / req.headers (no round trip). If those lack
        Authorization, the request is queued on 'requestfinished' and its full headers are
        read later, outside the handler - so a request that never settles (telemetry, a
        long poll) is never waited on."""
        self._auth_headers = {}
        self._auth_seq = 0          # bumps whenever a fresh Authorization is captured
        self._auth_finished = []    # finished feed-API requests whose raw headers to read
        if not self.auth_replay_enabled():
            return
        self._auth_origin = self._feed_origin()
        self._auth_path = self._auth_path_regex()

        def take(headers):
            h = replayable_headers(headers)
            if "authorization" in h:
                self._auth_headers = h
                self._auth_seq += 1
                return True
            return False

        def on_request(req):
            try:
                if self._is_auth_url(req.url):
                    req._auth_taken = take(req.headers)
            except Exception:
                pass

        def on_finished(req):
            try:
                if self._is_auth_url(req.url) and not getattr(req, "_auth_taken", False):
                    self._auth_finished = (self._auth_finished + [req])[-5:]
            except Exception:
                pass

        self._auth_take = take
        page.on("request", on_request)
        page.on("requestfinished", on_finished)

    def _drain_auth_finished(self):
        pending, self._auth_finished = getattr(self, "_auth_finished", []), []
        for req in pending:
            try:
                self._auth_take(req.all_headers())   # finished -> raw headers already in
            except Exception:
                pass

    def await_auth(self, page, nav_timeout=90000, fresh=False):
        """Wait (bounded by <KEY>_AUTH_WAIT_MS, default 15000) until the page's own feed call
        has given us an Authorization header. If none arrives within a third of the wait (or
        fresh=True), re-open warm_url to make the app issue its feed call again. Returns
        True when headers are available."""
        if not self.auth_replay_enabled() or not hasattr(self, "_auth_seq"):
            return False
        start = self._auth_seq
        timeout = int(self._env("AUTH_WAIT_MS") or 15000)

        def done():
            self._drain_auth_finished()
            return self._auth_seq > start if fresh else bool(self._auth_headers)

        if done():
            return True
        reloaded = False
        waited = 0
        while waited < timeout:
            if not reloaded and (fresh or waited >= timeout // 3):
                try:
                    page.goto(self.warm_url(), wait_until="domcontentloaded", timeout=nav_timeout)
                except Exception as exc:
                    print("  (auth re-warm note:", exc, ")")
                reloaded = True
            page.wait_for_timeout(500)
            waited += 500
            if done():
                return True
        print(f"  (no page auth header seen within {timeout} ms)")
        return False

    def _page_fetch(self, page, url, binary, headers=None, storage_key=""):
        """GET url from inside the page (proxy + cookies). JS adds the storage token itself,
        so its value never passes through Python."""
        return page.evaluate(
            """async ([u, binary, headers, storageKey]) => {
                const h = Object.assign({}, headers || {});
                if (storageKey) {
                    let v = localStorage.getItem(storageKey) || sessionStorage.getItem(storageKey);
                    try { const j = JSON.parse(v); v = (j && (j.access_token || j.accessToken
                          || j.token || j.id_token)) || v; } catch (e) {}
                    if (v && typeof v === 'string') h['Authorization'] = 'Bearer ' + v;
                }
                const r = await fetch(u, { credentials: 'include', headers: h });
                if (!binary) return { status: r.status, ok: r.ok, body: await r.text() };
                const buf = new Uint8Array(await r.arrayBuffer());
                let bin = ''; const CH = 0x8000;
                for (let i = 0; i < buf.length; i += CH) {
                    bin += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
                }
                return { status: r.status, ok: r.ok, b64: btoa(bin) };
            }""",
            [url, binary, headers or {}, storage_key],
        )

    def page_fetch(self, page, url, binary=False, send_auth=False):
        """In-page GET. With send_auth, the captured page auth headers go on the first try.
        On 401/403, retry once with them (a fresh capture if they were already sent). Auth
        headers only ever go to the feed's own origin."""
        allowed = (self.auth_replay_enabled() and hasattr(self, "_auth_origin")
                   and urlsplit(url).netloc == self._auth_origin)
        storage_key = self._env("TOKEN_STORAGE_KEY") if allowed else ""
        sent = bool(allowed and send_auth and (self._auth_headers or storage_key))
        res = (self._page_fetch(page, url, binary, self._auth_headers, storage_key) if sent
               else self._page_fetch(page, url, binary))
        if res["status"] not in self._RETRY_STATUSES or not allowed:
            return res
        if sent:
            self.await_auth(page, fresh=True)   # token may have expired; let the app refresh
        else:
            self._drain_auth_finished()
        headers = dict(self._auth_headers)
        if not headers and not storage_key:
            print(f"  ({res['status']}: no page auth headers captured to retry with)")
            return res
        names = sorted(headers) + (["authorization(storage)"] if storage_key else [])
        print(f"  ({res['status']}: retrying with page auth headers: {', '.join(names)})")
        return self._page_fetch(page, url, binary, headers, storage_key)

    # ------------------------------------------------------------------- feed
    def fetch_items(self, page, offset, limit):
        """Return a list of raw feed items. Default: GET feed_url from inside the page."""
        if self.auth_replay_enabled():
            self.await_auth(page)
        res = self.page_fetch(page, self.feed_url(offset, limit), send_auth=True)
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
        res = self.page_fetch(page, url, binary=True)
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


# Headers the browser sets itself (or forbids JS from setting) - never replayed.
_NEVER_REPLAY = {"cookie", "host", "content-length", "origin", "referer", "user-agent",
                 "connection", "accept-encoding", "x-client-data"}


def replayable_headers(headers):
    """Subset of a request's headers worth replaying on our own fetch: Authorization and
    app-specific x-* headers (XSRF/CSRF token, client id, app version). Keys lower-cased."""
    out = {}
    for name, value in (headers or {}).items():
        n = name.lower()
        if n in _NEVER_REPLAY or n.startswith(("sec-", ":")):
            continue
        if n == "authorization" or n.startswith("x-"):
            out[n] = value
    return out
