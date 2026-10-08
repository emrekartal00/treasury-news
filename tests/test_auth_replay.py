"""Auth-header replay in sources/base.py, on a fake page (no browser, no network).

URLs are built from the configured feed URL, so no host or path is hardcoded here.
Runs with `python -m pytest` or `python -m unittest discover tests`.
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.base import Source, replayable_headers  # noqa: E402
from sources.gs import GoldmanSachs  # noqa: E402

GS = GoldmanSachs()
_FEED = urlsplit(GS.feed_url(0, 6))
ORIGIN = f"{_FEED.scheme}://{_FEED.netloc}"
FEED_PATH = _FEED.path
OTHER = "https://telemetry.example"

TOKEN = "Bearer secret-token-1"
APP_HEADERS = {"Authorization": TOKEN, "X-App-Version": "v9", "Cookie": "s=abc",
               "Accept": "application/json", "sec-ch-ua": "chrome", "User-Agent": "ua"}


class FakeRequest:
    def __init__(self, url, headers=None, raw=None, never_settles=False, untouchable=False):
        self.url = url
        self._headers = headers or {}
        self._raw = raw if raw is not None else self._headers
        self.never_settles = never_settles
        self.untouchable = untouchable
        self.all_headers_calls = 0

    @property
    def headers(self):
        if self.untouchable:
            raise AssertionError("headers read on a request that must not be touched")
        return self._headers

    def all_headers(self):
        self.all_headers_calls += 1
        if self.never_settles or self.untouchable:
            raise AssertionError("all_headers() would block on this request")
        return self._raw


class FakePage:
    """In-page fetches answer from a status queue; events fire on goto / at clock times."""

    def __init__(self, statuses, on_goto=None):
        self.statuses = list(statuses)
        self.calls = []        # (url, binary, headers, storage_key)
        self.listeners = {}
        self.clock = 0
        self.scheduled = []    # (time, event, request)
        self.on_goto = on_goto or []   # list of (event, request) fired per goto, popped
        self.gotos = []

    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def emit(self, event, req):
        for cb in self.listeners.get(event, []):
            cb(req)

    def request(self, req, finished=True):
        self.emit("request", req)
        if finished and not req.never_settles:
            self.emit("requestfinished", req)

    def wait_for_timeout(self, ms):
        self.clock += ms
        due = [s for s in self.scheduled if s[0] <= self.clock]
        self.scheduled = [s for s in self.scheduled if s[0] > self.clock]
        for _, event, req in due:
            self.emit(event, req)

    def goto(self, url, **kw):
        self.gotos.append(url)
        if self.on_goto:
            for event, req in self.on_goto.pop(0):
                self.emit(event, req)

    def evaluate(self, script, arg):
        url, binary, headers, storage_key = arg
        self.calls.append((url, binary, dict(headers), storage_key))
        status = self.statuses.pop(0)
        res = {"status": status, "ok": 200 <= status < 300}
        if binary:
            res["b64"] = "JVBERi0="  # b"%PDF-"
        else:
            res["body"] = '{"results": [{"id": "a"}]}' if res["ok"] else ""
        return res


def feed_req(headers=APP_HEADERS, **kw):
    return FakeRequest(f"{ORIGIN}{FEED_PATH}?offset=0&limit=6", headers, **kw)


def rum_req():
    return FakeRequest(f"{OTHER}/api/v2/rum", {"Authorization": "Bearer rum"}, untouchable=True)


class EnvCase(unittest.TestCase):
    KEYS = ("GS_AUTH_REPLAY", "GS_AUTH_CAPTURE", "GS_TOKEN_STORAGE_KEY", "GS_AUTH_WAIT_MS")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.KEYS}
        os.environ["GS_AUTH_WAIT_MS"] = "3000"

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def started(self, statuses, on_goto=None):
        src, page = GoldmanSachs(), FakePage(statuses, on_goto)
        src.start_auth_capture(page)
        return src, page


class ReplayableHeadersTest(unittest.TestCase):
    def test_keeps_auth_and_app_headers_only(self):
        self.assertEqual(replayable_headers(APP_HEADERS),
                         {"authorization": TOKEN, "x-app-version": "v9"})

    def test_empty(self):
        self.assertEqual(replayable_headers(None), {})


class CaptureScopeTest(EnvCase):
    def test_gs_feed_url_is_the_configured_feed_route(self):
        q = [kv.split("=")[0] for kv in _FEED.query.split("&")]
        self.assertEqual(q, ["offset", "limit", "getNewResults"])

    def test_other_host_is_never_touched(self):
        src, page = self.started([401])
        rum = rum_req()
        page.request(rum)
        src._drain_auth_finished()
        self.assertEqual(src._auth_headers, {})
        self.assertEqual(rum.all_headers_calls, 0)

    def test_same_origin_non_api_path_not_captured(self):
        src, page = self.started([])
        page.request(FakeRequest(f"{ORIGIN}/static/app.js", APP_HEADERS))
        self.assertEqual(src._auth_headers, {})

    def test_same_api_prefix_other_endpoint_is_captured(self):
        if "/api/" not in FEED_PATH:
            self.skipTest("configured feed path has no /api/ segment")
        src, page = self.started([])
        prefix = FEED_PATH[:FEED_PATH.find("/api/") + 5]
        page.request(FakeRequest(f"{ORIGIN}{prefix}other", APP_HEADERS))
        self.assertEqual(src._auth_headers.get("authorization"), TOKEN)

    def test_capture_regex_overridable(self):
        os.environ["GS_AUTH_CAPTURE"] = r"^/static/"
        src, page = self.started([])
        page.request(FakeRequest(f"{ORIGIN}/static/app.js", APP_HEADERS))
        self.assertEqual(src._auth_headers.get("authorization"), TOKEN)


class FeedTest(EnvCase):
    def test_feed_sends_captured_auth_on_first_try(self):
        src, page = self.started([200])
        page.request(rum_req())
        page.request(feed_req())
        self.assertEqual(src.fetch_items(page, 0, 6), [{"id": "a"}])
        self.assertEqual(len(page.calls), 1)
        self.assertEqual(page.calls[0][2], {"authorization": TOKEN, "x-app-version": "v9"})
        self.assertEqual(page.gotos, [])

    def test_raw_headers_read_only_after_request_finished(self):
        src, page = self.started([200])
        req = feed_req(headers={"Accept": "application/json"}, raw=APP_HEADERS)
        page.request(req)
        src.fetch_items(page, 0, 6)
        self.assertEqual(req.all_headers_calls, 1)
        self.assertEqual(page.calls[0][2]["authorization"], TOKEN)

    def test_never_settling_request_is_never_waited_on(self):
        src, page = self.started([401])
        stuck = feed_req(headers={"Accept": "application/json"}, never_settles=True)
        page.request(stuck)
        page.request(rum_req(), finished=False)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaisesRegex(RuntimeError, "feed 401"):
            src.fetch_items(page, 0, 6)
        self.assertEqual(stuck.all_headers_calls, 0)
        self.assertLessEqual(page.clock, 3000)     # bounded wait
        self.assertEqual(len(page.gotos), 1)       # tried to trigger the app's own call once
        self.assertIn("no page auth header seen", out.getvalue())

    def test_no_feed_call_yet_triggers_the_page_and_waits(self):
        src, page = self.started([200], on_goto=[[("request", feed_req())]])
        src.fetch_items(page, 0, 6)
        self.assertEqual(page.gotos, [GS.warm_url()])
        self.assertEqual(page.calls[0][2]["authorization"], TOKEN)

    def test_late_feed_call_is_awaited_without_navigation(self):
        src, page = self.started([200])
        page.scheduled.append((500, "request", feed_req()))
        src.fetch_items(page, 0, 6)
        self.assertEqual(page.gotos, [])
        self.assertEqual(page.calls[0][2]["authorization"], TOKEN)

    def test_expired_token_is_refreshed_once(self):
        fresh = dict(APP_HEADERS, Authorization="Bearer secret-token-2")
        src, page = self.started([401, 200], on_goto=[[("request", feed_req(fresh))]])
        page.request(feed_req())
        with redirect_stdout(io.StringIO()):
            src.fetch_items(page, 0, 6)
        self.assertEqual(page.calls[0][2]["authorization"], TOKEN)
        self.assertEqual(page.calls[1][2]["authorization"], "Bearer secret-token-2")

    def test_non_auth_error_does_not_retry(self):
        src, page = self.started([404])
        page.request(feed_req())
        with self.assertRaises(RuntimeError):
            src.fetch_items(page, 0, 6)
        self.assertEqual(len(page.calls), 1)

    def test_storage_key_is_passed_to_page_not_read_in_python(self):
        os.environ["GS_TOKEN_STORAGE_KEY"] = "app.token"
        src, page = self.started([200])
        page.request(feed_req())
        src.fetch_items(page, 0, 6)
        self.assertEqual(page.calls[0][3], "app.token")

    def test_header_values_never_printed(self):
        src, page = self.started([401, 401], on_goto=[[("request", feed_req())]])
        page.request(feed_req())
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(RuntimeError):
            src.fetch_items(page, 0, 6)
        self.assertIn("authorization", out.getvalue())
        self.assertNotIn("secret-token", out.getvalue())
        self.assertNotIn("v9", out.getvalue())


class PdfTest(EnvCase):
    def test_pdf_tries_bare_then_retries_with_page_header(self):
        src, page = self.started([403, 200])
        page.request(feed_req())
        with redirect_stdout(io.StringIO()):
            body, status = src.fetch_pdf(page, f"{ORIGIN}/r.pdf")
        self.assertEqual((body[:5], status), (b"%PDF-", 200))
        self.assertEqual(page.calls[0][2], {})
        self.assertEqual(page.calls[1][2]["authorization"], TOKEN)
        self.assertEqual(page.gotos, [])            # no navigation during a PDF fetch

    def test_pdf_ok_bare_sends_no_header(self):
        src, page = self.started([200])
        page.request(feed_req())
        src.fetch_pdf(page, f"{ORIGIN}/r.pdf")
        self.assertEqual(page.calls[0][2], {})

    def test_header_never_sent_to_another_origin(self):
        src, page = self.started([401])
        page.request(feed_req())
        src.fetch_pdf(page, f"{OTHER}/r.pdf")
        self.assertEqual(len(page.calls), 1)
        self.assertEqual(page.calls[0][2], {})


class GuardTest(EnvCase):
    def test_guard_off_disables_capture_and_retry(self):
        os.environ["GS_AUTH_REPLAY"] = "off"
        src, page = self.started([401])
        self.assertEqual(page.listeners, {})
        with self.assertRaises(RuntimeError):
            src.fetch_items(page, 0, 6)
        self.assertEqual(len(page.calls), 1)
        self.assertEqual(page.calls[0][2], {})

    def test_base_default_is_off(self):
        class Plain(Source):
            key = "plain"

            def feed_url(self, offset, limit):
                return f"{ORIGIN}/api/feed"

        src, page = Plain(), FakePage([401])
        src.start_auth_capture(page)
        self.assertEqual(page.listeners, {})
        with self.assertRaises(RuntimeError):
            src.fetch_items(page, 0, 6)
        self.assertEqual(len(page.calls), 1)


if __name__ == "__main__":
    unittest.main()
