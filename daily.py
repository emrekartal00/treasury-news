"""daily.py - poll a source's feed and download only newly-published items.

Source-agnostic: pick a portal with --source (default 'gs'); the per-portal specifics
(feed URL, field mapping, content/PDF URLs) live in sources/<key>.py. Dedupes by native id
via state/seen_<key>.json, so it's safe to re-run. Saves each item as HTML + PDF + meta.json
under downloads/<key>/YYYY-MM-DD/.

Run in Spyder (Run file) or:
  python daily.py                       # source 'gs', everything new since last run
  python daily.py --source jpm          # a different portal
  python daily.py --days 3              # only items published in the last 3 days
  python daily.py --max 10             # cap downloads this run
  python daily.py --limit 50           # feed page size to scan
"""
import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import config  # noqa: F401  # loads .env
import sources
from _util import run_in_thread

HERE = Path(__file__).parent
STATE_DIR = HERE / "state"


def seen_path(source_key):
    return STATE_DIR / f"seen_{source_key}.json"


def load_seen(source_key):
    p = seen_path(source_key)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"ids": {}, "lastRun": None}


def save_seen(source_key, seen):
    STATE_DIR.mkdir(exist_ok=True)
    seen_path(source_key).write_text(
        json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def slug(text):
    text = text or "report"
    text = re.sub("[\u2014\u2013]", "-", text)  # em/en dash -> hyphen
    text = re.sub(r"[^\w.-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:90]


def human_pause():
    """Realistic, randomized ~2.5-5s gap between downloads."""
    time.sleep(2.5 + random.random() * 2.5)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.environ.get("SOURCE", "gs"),
                    help=f"portal to scrape ({', '.join(sources.keys())})")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--max", type=int, default=100)
    ap.add_argument("--days", type=int,
                    default=int(os.environ["SCRAPE_DAYS"]) if os.environ.get("SCRAPE_DAYS") else None,
                    help="only items published in the last N days "
                         "(default: SCRAPE_DAYS env if set, else no age limit)")
    ap.add_argument("--login", action="store_true",
                    help="pause for interactive login in the opened browser before scraping "
                         "(for portals whose session does not persist across launches, e.g. barc)")
    args, _ = ap.parse_known_args()  # tolerate Spyder-injected args

    src = sources.get(args.source)   # raises with a helpful message on a bad key
    since_ms = (time.time() * 1000 - args.days * 86400000) if args.days is not None else None
    seen = load_seen(src.key)

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
        print(f"Source: {src.label} ({src.key})")
        print(f"Opening {src.warm_url()} ...")
        src.warm(page, nav_timeout, warm_ms)

        # Scripted login for portals whose session does not persist across launches
        # (e.g. barc): authenticates in THIS session from stored credentials. No-op for
        # cookie-based portals or when credentials are not configured.
        try:
            if src.login(page, nav_timeout):
                print("  logged in via stored credentials.")
        except Exception as exc:
            print("  (login note:", exc, ")")

        print("Reading feed...")
        feed = []
        # First page with retries: on a slow connection the session/feed may not be ready
        # immediately after warming, and can come back empty. With --login we retry for much
        # longer (silently) so you have time to log in in the visible browser window.
        max_attempts = 60 if args.login else 4
        if args.login:
            print("\n>>> Log in in the browser window now. "
                  "Waiting for an authenticated session (up to ~5 min)...")
        batch = []
        for attempt in range(1, max_attempts + 1):
            try:
                batch = src.fetch_items(page, 0, args.limit)
            except Exception as exc:
                batch = []
                if not args.login:
                    print(f"  feed error (try {attempt}): {exc}")
            if batch:
                if args.login:
                    print(f"    session authenticated ({len(batch)} items visible).")
                break
            if attempt < max_attempts:
                if not args.login:
                    print(f"  feed empty; waiting 5s and retrying ({attempt}/{max_attempts})")
                page.wait_for_timeout(5000)
        feed.extend(batch)
        # Paginate further only when a --days window is set (one page is enough otherwise).
        if batch and since_ms:
            offset = args.limit
            while offset < 300:
                try:
                    more = src.fetch_items(page, offset, args.limit)
                except Exception as exc:
                    print("  feed error:", exc)
                    break
                if not more:
                    break
                feed.extend(more)
                oldest = min((src.pubdate_ms(x) or 0) for x in more)
                if oldest < since_ms:
                    break
                offset += args.limit

        cands = [it for it in feed if src.native_id(it) and src.native_id(it) not in seen["ids"]]
        if since_ms:
            cands = [it for it in cands if (src.pubdate_ms(it) or 0) >= since_ms]
        cands.sort(key=lambda it: src.pubdate_ms(it) or 0, reverse=True)
        cands = cands[: args.max]

        window = f" (last {args.days}d)" if since_ms else ""
        tail = "" if cands else " - nothing to do."
        print(f"> Feed items: {len(feed)} | new & unseen: {len(cands)}{window}{tail}")

        done = 0
        for it in cands:
            native = src.native_id(it)
            date = src.date(it)
            out_dir = HERE / "downloads" / src.key / date
            out_dir.mkdir(parents=True, exist_ok=True)
            meta = src.normalize(it)
            title = meta.get("title") or meta.get("distributionHeadline")
            base = out_dir / f"{slug(title)}_{str(native)[:8]}"
            html_url = src.content_url(it)
            pdf_url = src.pdf_url(it)
            print(f"\n- {date}  {title}")

            # HTML: navigate so the SPA authorizes the content route, then save rendered DOM.
            html_bytes = 0
            html_ok = False
            if html_url:
                try:
                    html = src.fetch_html(page, html_url, nav_timeout)
                    Path(f"{base}.html").write_text(html, encoding="utf-8")
                    html_bytes = len(html.encode("utf-8"))
                    html_ok = True  # fetch completed (even if the body was empty)
                    print(f"    ok html ({html_bytes} bytes)")
                except Exception as exc:
                    print(f"    x html failed: {exc}")

            # PDF: in-page fetch (inherits proxy + cookies), returned as base64.
            pdf_info = None
            if pdf_url:
                try:
                    body, status = src.fetch_pdf(page, pdf_url)
                    is_pdf = body[:5] == b"%PDF-"
                    Path(f"{base}." + ("pdf" if is_pdf else "pdf.html")).write_bytes(body)
                    pdf_info = {"status": status, "bytes": len(body), "isPdf": is_pdf}
                    note = "" if is_pdf else ", NOT a real pdf"
                    print(f"    ok pdf ({len(body)} bytes{note})")
                except Exception as exc:
                    print(f"    x pdf failed: {exc}")

            meta.update({
                "date": date,
                "htmlUrl": html_url, "pdfUrl": pdf_url,
                "htmlBytes": html_bytes, "pdf": pdf_info, "fetchedAt": now_iso(),
            })
            Path(f"{base}.meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

            # Mark seen if we captured content, so a slow/failed download retries next run
            # instead of being skipped forever. For sources with mark_seen_on_empty, a
            # completed-but-empty fetch (a text-less item) also counts, so it isn't re-tried.
            captured = html_bytes > 0 or pdf_info is not None
            if captured:
                seen["ids"][native] = {"date": date, "title": title, "fetchedAt": now_iso()}
                save_seen(src.key, seen)
                done += 1
            elif src.mark_seen_on_empty and html_ok:
                seen["ids"][native] = {"date": date, "title": title, "fetchedAt": now_iso()}
                save_seen(src.key, seen)
                print("    (no body content - marked seen, skipped)")
            else:
                print("    (nothing captured - kept unseen, will retry next run)")
            human_pause()

        seen["lastRun"] = now_iso()
        save_seen(src.key, seen)
        print(f"\nDone. Downloaded {done} new item(s) from {src.key}. "
              f"Total tracked: {len(seen['ids'])}.")
        ctx.close()


if __name__ == "__main__":
    run_in_thread(main)
