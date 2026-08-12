"""check_env.py — RUN THIS FIRST on the target PC (in Spyder).

Verifies the locked-down machine can actually run this project before you invest in setup:
  1. imports: playwright, oracledb
  2. a browser can launch (system Chrome, else bundled Chromium)
  3. the portal is reachable (needs TARGET_* in .env)
  4. Oracle connects (needs DB_* in .env; skipped if unset)

No secrets, safe to keep public. Exit code 0 if all critical checks pass.
"""
import os
import sys

import config
from _util import run_in_thread


def check_imports() -> bool:
    ok = True
    for mod in ("playwright", "oracledb"):
        try:
            __import__(mod)
            print(f"  ok   import {mod}")
        except Exception as exc:
            ok = False
            print(f"  FAIL import {mod}: {exc}")
    return ok


def check_browser() -> bool:
    from playwright.sync_api import sync_playwright

    def _run():
        with sync_playwright() as p:
            attempts = (
                ("system Chrome", lambda: p.chromium.launch(channel="chrome", headless=True)),
                ("bundled Chromium", lambda: p.chromium.launch(headless=True)),
            )
            for name, launch in attempts:
                try:
                    browser = launch()
                    page = browser.new_page()
                    page.goto("about:blank")
                    browser.close()
                    print(f"  ok   browser launch ({name})")
                    return True
                except Exception as exc:
                    print(f"       {name} failed: {exc}")
            return False

    try:
        return run_in_thread(_run)
    except Exception as exc:
        print(f"  FAIL browser: {exc}")
        return False


def check_reach() -> bool:
    from playwright.sync_api import sync_playwright

    def _run():
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(headless=True)
            try:
                resp = browser.new_page().goto(config.ORIGIN, timeout=20000)
                return resp.status if resp else None
            finally:
                browser.close()

    if "REDACTED" in config.ORIGIN:
        print("  skip portal reachability (TARGET_ORIGIN not set in .env)")
        return True
    try:
        status = run_in_thread(_run)
        print(f"  ok   portal reachable: {config.ORIGIN} -> HTTP {status}")
        return status is not None
    except Exception as exc:
        print(f"  FAIL reach portal {config.ORIGIN}: {exc}")
        return False


def check_oracle() -> bool:
    user, pw, dsn = (os.environ.get(k) for k in ("DB_USER", "DB_PASSWORD", "DB_CONNECT_STRING"))
    if not all([user, pw, dsn]):
        print("  skip Oracle (DB_* not set in .env)")
        return True
    try:
        import oracledb

        con = oracledb.connect(user=user, password=pw, dsn=dsn)
        con.close()
        print("  ok   Oracle connect")
        return True
    except Exception as exc:
        print(f"  FAIL Oracle: {exc}")
        return False


def main() -> int:
    print(f"Python {sys.version.split()[0]}")
    results = {
        "imports": check_imports(),
        "browser": check_browser(),
        "portal": check_reach(),
        "oracle": check_oracle(),
    }
    print()
    critical = results["imports"] and results["browser"]
    if critical and results["portal"]:
        print("✅ Core capabilities present — this PC can run the pipeline.")
    elif critical:
        print("⚠️  Runs, but portal not reachable yet (check network / VPN / TARGET_* in .env).")
    else:
        print("❌ Blocked: Python can't launch a browser here. Scraping on this PC won't work"
              " — we may need to keep scraping on a machine that can (see LOCAL_SETUP.md).")
    return 0 if critical else 1


if __name__ == "__main__":
    sys.exit(main())
