"""recon.py — one-time login + traffic capture.

Opens Chrome with a PERSISTENT profile (./user-data) so the login session is reused by
daily.py. Log in, reproduce your normal steps to reach a report, then CLOSE the window.
Traffic is logged to recon/traffic.jsonl; any downloads land in downloads/.

Run in Spyder (Run file) or:  python recon.py
"""
import json
import re
import sys
import time
from pathlib import Path

import config
from _util import run_in_thread

HERE = Path(__file__).parent
INTERESTING = re.compile(r"pdf|document|research|publication|content|/api/|\.json", re.I)


def main() -> None:
    start_url = sys.argv[1] if len(sys.argv) > 1 else config.HOMEPAGE
    recon_dir = HERE / "recon"
    recon_dir.mkdir(exist_ok=True)
    (HERE / "downloads").mkdir(exist_ok=True)
    log_path = recon_dir / "traffic.jsonl"

    def log(obj):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    print(f"\n▶ Opening Chrome (persistent profile ./user-data)")
    print(f"▶ Start URL: {start_url}")
    print("  Log in if needed, reproduce your steps to reach a report, then CLOSE the window.\n")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(HERE / "user-data"),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
        )

        def on_response(res):
            ct = res.headers.get("content-type", "")
            if INTERESTING.search(res.url) or re.search(r"pdf|json", ct, re.I):
                log({"t": int(time.time() * 1000), "kind": "response",
                     "status": res.status, "url": res.url, "contentType": ct})
                if re.search(r"application/pdf", ct, re.I):
                    print(f"  📄 PDF response {res.status}: {res.url}")

        ctx.on("response", on_response)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_download(dl):
            name = dl.suggested_filename or f"download-{int(time.time() * 1000)}.pdf"
            dl.save_as(str(HERE / "downloads" / name))
            print(f"  ⬇️  saved download: {name}")
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

    print("\n✅ Browser closed. Review recon/traffic.jsonl")


if __name__ == "__main__":
    run_in_thread(main)
