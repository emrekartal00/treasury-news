"""recon.py - one-time login + traffic capture (also the per-portal DISCOVERY tool).

Opens Chrome with a PERSISTENT profile (./user-data) so the login session is reused by
daily.py. Log in, reproduce your normal steps to reach the research LIST and open ONE
report (so its PDF loads), then CLOSE the window.

For DISCOVERY of a new portal, pass a source key or a start URL:
  python recon.py                 # default: the gs 'My Content' page
  python recon.py jpm             # a registered source's warm URL
  python recon.py https://...     # any URL (portal not yet registered)

It logs to recon/traffic.jsonl:
  - request lines: method + url + (POST body, capped) for interesting endpoints
  - response lines: status + url + content-type, and for JSON responses the BODY (capped)
So you can spot the feed/list endpoint and see its shape. Paste the relevant lines back and
I'll turn them into sources/<key>.py. Downloads (e.g. a clicked PDF) land in downloads/.
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import config  # noqa: F401  # loads .env
import sources
from _util import run_in_thread

HERE = Path(__file__).parent
INTERESTING = re.compile(r"pdf|document|research|publication|content|feed|stream|/api/|\.json", re.I)
BODY_CAP = 6000  # chars of a JSON body to record (enough to see the item shape)


def start_url_from_arg(arg):
    # For a source KEY, open the portal's ORIGIN ROOT - that reliably triggers the login/SSO
    # redirect for a logged-out session. (A deep authenticated path like JPM's /mcp-home 404s
    # when you're not logged in, so we can't use the adapter's warm_url here.)
    key = arg or "gs"
    if key in sources.keys():
        p = urlparse(sources.get(key).warm_url())
        return f"{p.scheme}://{p.netloc}"
    return arg  # a full URL passed through as-is


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    start_url = start_url_from_arg(arg)
    recon_dir = HERE / "recon"
    recon_dir.mkdir(exist_ok=True)
    (HERE / "downloads").mkdir(exist_ok=True)
    log_path = recon_dir / "traffic.jsonl"

    def log(obj):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    print("\n> Opening Chrome (persistent profile ./user-data)")
    print(f"> Start URL: {start_url}")
    print("  Log in if needed, open the research LIST and ONE report (let its PDF load), "
          "then CLOSE the window.\n")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(HERE / "user-data"),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
        )

        def on_request(req):
            try:
                if not INTERESTING.search(req.url):
                    return
                entry = {"t": int(time.time() * 1000), "kind": "request",
                         "method": req.method, "url": req.url}
                if req.method in ("POST", "PUT"):
                    body = req.post_data
                    if body:
                        entry["postData"] = body[:BODY_CAP]
                log(entry)
            except Exception:
                pass

        def on_response(res):
            try:
                ct = res.headers.get("content-type", "")
                if not (INTERESTING.search(res.url) or re.search(r"pdf|json", ct, re.I)):
                    return
                entry = {"t": int(time.time() * 1000), "kind": "response",
                         "status": res.status, "url": res.url, "contentType": ct}
                if re.search(r"json", ct, re.I):
                    try:
                        entry["body"] = res.text()[:BODY_CAP]
                    except Exception:
                        pass
                elif re.search(r"application/pdf", ct, re.I):
                    print(f"  [pdf] {res.status}: {res.url}")
                log(entry)
            except Exception:
                pass

        ctx.on("request", on_request)
        ctx.on("response", on_response)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_download(dl):
            name = dl.suggested_filename or f"download-{int(time.time() * 1000)}.pdf"
            dl.save_as(str(HERE / "downloads" / name))
            print(f"  [saved] {name}")
            log({"t": int(time.time() * 1000), "kind": "download", "filename": name, "url": dl.url})

        page.on("download", on_download)

        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print("  (navigation note:", exc, ")")

        # Block until the user closes the browser (any call throws once it's gone).
        try:
            while True:
                page.wait_for_timeout(1000)
        except Exception:
            pass

    print("\nDone. Review recon/traffic.jsonl (look for the list/feed JSON and the PDF URL).")


if __name__ == "__main__":
    run_in_thread(main)
