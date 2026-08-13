"""probe_feed.py - show exactly what the content feed returns (debugging).

Reuses your saved session, warms the My Content page, then hits the feed and prints the
raw response (status, content-type, length, first chars). Paste the output back.

Run:  python probe_feed.py
"""
from pathlib import Path

import config
from _util import run_in_thread

HERE = Path(__file__).parent


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(HERE / "user-data"), channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("Warming:", config.MYCONTENT)
        try:
            page.goto(config.MYCONTENT, wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:
            print("  goto note:", exc)
        page.wait_for_timeout(6000)

        # What account is this? Look at the page title / any visible name.
        try:
            print("Page title:", page.title())
        except Exception:
            pass

        url = config.feed_url(0, 6)
        print("\nFEED URL:", url)
        r = ctx.request.get(url, timeout=30000)
        body = r.text()
        print("status      :", r.status, "ok:", r.ok)
        print("content-type:", r.headers.get("content-type"))
        print("body length :", len(body))
        print("first 1000 chars:\n" + body[:1000])

        print("\n(If body is '[]' the feed is genuinely empty for THIS account -> check "
              "which user is logged in, top-right of the browser window.)")
        print("Leaving the browser open for 30s so you can read the account name...")
        page.wait_for_timeout(30000)
        ctx.close()


if __name__ == "__main__":
    run_in_thread(main)
