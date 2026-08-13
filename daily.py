"""daily.py — poll the 'My Content' feed and download only newly-published items.

Dedupes by report id via state/seen.json, so it's safe to run repeatedly. Saves each item
as HTML + PDF + meta.json under downloads/YYYY-MM-DD/.

Run in Spyder (Run file) or:
  python daily.py               # everything new since last run
  python daily.py --days 3      # only items published in the last 3 days
  python daily.py --max 10      # cap downloads this run
  python daily.py --limit 50    # feed page size to scan
"""
import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from _util import run_in_thread

HERE = Path(__file__).parent
STATE_DIR = HERE / "state"
SEEN_PATH = STATE_DIR / "seen.json"


def load_seen():
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    return {"ids": {}, "lastRun": None}


def save_seen(seen):
    STATE_DIR.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def slug(text):
    text = text or "report"
    text = re.sub(r"[—–]", "-", text)      # em/en dash -> hyphen
    text = re.sub(r"[^\w.-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:90]


def date_from_path(path):
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", path or "")
    return "-".join(m.groups()) if m else None


def human_pause():
    """Realistic, randomized ~2.5-5s gap between downloads."""
    time.sleep(2.5 + random.random() * 2.5)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--max", type=int, default=100)
    ap.add_argument("--days", type=int, default=None)
    args, _ = ap.parse_known_args()  # tolerate Spyder-injected args

    since_ms = (time.time() * 1000 - args.days * 86400000) if args.days is not None else None
    seen = load_seen()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(HERE / "user-data"),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Tunable for slow connections (milliseconds), overridable via .env.
        nav_timeout = int(os.environ.get("SCRAPE_NAV_TIMEOUT_MS", "90000"))
        warm_ms = int(os.environ.get("SCRAPE_WARM_MS", "6000"))
        print("Opening My Content...")
        try:
            page.goto(config.MYCONTENT, wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception as exc:
            print("  (goto note:", exc, ")")
        page.wait_for_timeout(warm_ms)

        def fetch_page(offset):
            r = ctx.request.get(config.feed_url(offset, args.limit), timeout=30000)
            if not r.ok:
                raise RuntimeError(f"feed {r.status} at offset {offset}")
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("results") or data.get("items") or []

        print("Reading feed...")
        feed = []
        # First page with retries: on a slow connection the session/feed may not be ready
        # immediately after warming, and can come back empty. Retry before giving up.
        batch = []
        for attempt in range(1, 5):
            try:
                batch = fetch_page(0)
            except Exception as exc:
                print(f"  feed error (try {attempt}): {exc}")
                batch = []
            if batch:
                break
            if attempt < 4:
                print(f"  feed empty; waiting 5s and retrying ({attempt}/4)")
                page.wait_for_timeout(5000)
        feed.extend(batch)
        # Paginate further only when a --days window is set (one page is enough otherwise).
        if batch and since_ms:
            offset = args.limit
            while offset < 300:
                try:
                    more = fetch_page(offset)
                except Exception as exc:
                    print("  feed error:", exc)
                    break
                if not more:
                    break
                feed.extend(more)
                oldest = min((x.get("publicationDateTime") or 0) for x in more)
                if oldest < since_ms:
                    break
                offset += args.limit

        cands = [it for it in feed if it.get("id") and it["id"] not in seen["ids"]]
        if since_ms:
            cands = [it for it in cands if (it.get("publicationDateTime") or 0) >= since_ms]
        cands.sort(key=lambda it: it.get("publicationDateTime") or 0, reverse=True)
        cands = cands[: args.max]

        window = f" (last {args.days}d)" if since_ms else ""
        tail = "" if cands else " — nothing to do."
        print(f"▶ Feed items: {len(feed)} | new & unseen: {len(cands)}{window}{tail}")

        done = 0
        for it in cands:
            date = date_from_path(it.get("path")) or datetime.fromtimestamp(
                (it.get("publicationDateTime") or time.time() * 1000) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            out_dir = HERE / "downloads" / date
            out_dir.mkdir(parents=True, exist_ok=True)
            base = out_dir / f"{slug(it.get('title') or it.get('distributionHeadline'))}_{it['id'][:8]}"
            html_url = config.ORIGIN + it["path"]
            pdf_url = config.ORIGIN + it["downloadPath"] if it.get("downloadPath") else None
            print(f"\n• {date}  {it.get('title') or it.get('distributionHeadline')}")

            # HTML: navigate so the SPA authorizes the content route, then save rendered DOM.
            html_bytes = 0
            try:
                page.goto(html_url, wait_until="domcontentloaded", timeout=nav_timeout)
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
                Path(f"{base}.html").write_text(html, encoding="utf-8")
                html_bytes = len(html.encode("utf-8"))
                print(f"    ✓ html ({html_bytes} bytes)")
            except Exception as exc:
                print(f"    ✗ html failed: {exc}")

            # PDF: direct authenticated GET (works with cookies).
            pdf_info = None
            if pdf_url:
                try:
                    r = ctx.request.get(pdf_url, timeout=120000)
                    body = r.body()
                    is_pdf = body[:5] == b"%PDF-"
                    Path(f"{base}." + ("pdf" if is_pdf else "pdf.html")).write_bytes(body)
                    pdf_info = {"status": r.status, "bytes": len(body), "isPdf": is_pdf}
                    note = "" if is_pdf else ", NOT a real pdf"
                    print(f"    ✓ pdf ({len(body)} bytes{note})")
                except Exception as exc:
                    print(f"    ✗ pdf failed: {exc}")

            meta = {
                "id": it["id"], "title": it.get("title"),
                "distributionHeadline": it.get("distributionHeadline"),
                "date": date, "publicationDateTime": it.get("publicationDateTime"),
                "authors": it.get("authors"), "synopsis": it.get("synopsis"),
                "reportTypes": it.get("reportTypes"), "totalPages": it.get("totalPages"),
                "htmlUrl": html_url, "pdfUrl": pdf_url, "htmlBytes": html_bytes,
                "pdf": pdf_info, "fetchedAt": now_iso(),
            }
            Path(f"{base}.meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            # Only mark seen if we actually captured something, so a slow/failed download
            # retries next run instead of being silently skipped forever.
            if html_bytes > 0 or pdf_info is not None:
                seen["ids"][it["id"]] = {"date": date, "title": it.get("title"), "fetchedAt": now_iso()}
                save_seen(seen)
                done += 1
            else:
                print("    (nothing captured - kept unseen, will retry next run)")
            human_pause()

        seen["lastRun"] = now_iso()
        save_seen(seen)
        print(f"\n✅ Done. Downloaded {done} new item(s). Total tracked: {len(seen['ids'])}.")
        ctx.close()


if __name__ == "__main__":
    run_in_thread(main)
