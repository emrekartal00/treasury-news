"""diag.py - office-PC environment probe.  Run:  python diag.py

Confirms what this (locked-down) machine can do, so the pipeline is built against reality.
SAFE: no DB writes, no email sent, unless you explicitly pass a flag:
    python diag.py                 # all read-only checks
    python diag.py --send-mail     # also send ONE test email to MAIL_TO
Every section SKIPs cleanly if its env vars (.env) are not set yet.
Output is ASCII-only on purpose (no encoding surprises on a Turkish-Windows box).
"""
import importlib
import os
import platform
import sys

# config.py loads .env and exposes the portal URLs; tolerate its absence.
try:
    import config  # noqa
    _HAVE_CONFIG = True
except Exception as exc:  # pragma: no cover
    _HAVE_CONFIG = False
    print("WARN: could not import config.py:", exc)

_P, _F, _S = [], [], []


def res(status, name, detail=""):
    print(f"  [{status:4}] {name}" + (f"  - {detail}" if detail else ""))
    {"OK": _P, "FAIL": _F, "SKIP": _S}[status].append(name)


def section(title):
    print("\n=== " + title + " ===")


def env(*names):
    return {n: os.environ.get(n) for n in names}


# ---------------------------------------------------------------- 1. Python
section("Python / platform")
print("  python", sys.version.split()[0], "|", platform.platform())
if sys.version_info < (3, 7):
    res("FAIL", "python >= 3.7", f"found {sys.version.split()[0]}; project needs 3.7+ (f-strings)")
else:
    res("OK", "python >= 3.7")

# ---------------------------------------------------------------- 2. Packages
section("Python packages (pip install <name> if missing)")
for mod in ("playwright", "oracledb", "requests"):
    try:
        importlib.import_module(mod)
        res("OK", f"import {mod}")
    except Exception as exc:
        res("FAIL", f"import {mod}", f"{exc.__class__.__name__}: pip install {mod}")

# HTML parser: need at least ONE. selectolax (fast, needs a wheel) or bs4 (pure-python).
_parsers = []
for mod, pip in (("selectolax", "selectolax"), ("bs4", "beautifulsoup4")):
    try:
        importlib.import_module(mod)
        res("OK", f"import {mod}")
        _parsers.append(mod)
    except Exception:
        res("SKIP", f"import {mod}", f"optional HTML parser: pip install {pip}")
if not _parsers:
    res("FAIL", "html parser", "need selectolax OR beautifulsoup4 for text extraction")

# ---------------------------------------------------------------- 3. Browser + portal
section("Browser / portal reachability (Playwright)")
if "playwright" not in sys.modules and importlib.util.find_spec("playwright") is None:
    res("SKIP", "browser launch", "playwright not installed")
else:
    try:
        from _util import run_in_thread
    except Exception:
        def run_in_thread(fn, *a, **k):
            return fn(*a, **k)

    def _browser():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            for label, launch in (("system Chrome", lambda: p.chromium.launch(channel="chrome", headless=True)),
                                  ("bundled Chromium", lambda: p.chromium.launch(headless=True))):
                try:
                    b = launch(); b.new_page().goto("about:blank"); b.close()
                    return label
                except Exception as e:
                    print(f"         {label} failed: {e}")
            return None

    try:
        label = run_in_thread(_browser)
        res("OK", "browser launch", label) if label else res("FAIL", "browser launch", "no browser channel worked")
    except Exception as exc:
        res("FAIL", "browser launch", str(exc))

    origin = getattr(config, "ORIGIN", "") if _HAVE_CONFIG else ""
    if not origin or "REDACTED" in origin:
        res("SKIP", "portal reach", "set TARGET_ORIGIN in .env")
    else:
        def _reach():
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                try:
                    b = p.chromium.launch(channel="chrome", headless=True)
                except Exception:
                    b = p.chromium.launch(headless=True)
                try:
                    r = b.new_page().goto(origin, timeout=20000)
                    return r.status if r else None
                finally:
                    b.close()
        try:
            status = run_in_thread(_reach)
            res("OK", "portal reach", f"{origin} -> HTTP {status}")
        except Exception as exc:
            res("FAIL", "portal reach", f"{origin}: {exc}")

# ---------------------------------------------------------------- 4. Oracle
section("OracleDB (read-only)")
e = env("DB_USER", "DB_PASSWORD", "DB_CONNECT_STRING", "DB_SCHEMA")
if not all(e[k] for k in ("DB_USER", "DB_PASSWORD", "DB_CONNECT_STRING")):
    res("SKIP", "oracle connect", "set DB_USER / DB_PASSWORD / DB_CONNECT_STRING in .env")
else:
    try:
        import oracledb
        con = oracledb.connect(user=e["DB_USER"], password=e["DB_PASSWORD"], dsn=e["DB_CONNECT_STRING"])
        res("OK", "oracle connect", "thin mode")
        cur = con.cursor()
        schema = (e["DB_SCHEMA"] or "").strip()
        if schema:
            import re
            if re.match(r"^[A-Za-z0-9_$#]+$", schema):
                cur.execute("ALTER SESSION SET CURRENT_SCHEMA = " + schema)
                res("OK", "set current_schema", schema)
                cur.execute("SELECT table_name FROM all_tables WHERE owner = :o ORDER BY table_name",
                            {"o": schema.upper()})
                names = [r[0] for r in cur.fetchall()]
                res("OK" if names else "FAIL", "tables visible in schema",
                    f"{len(names)}: {', '.join(names) if names else 'none (missing grants?)'}")
            else:
                res("FAIL", "set current_schema", f"unsafe identifier: {schema!r}")
        else:
            res("SKIP", "set current_schema", "set DB_SCHEMA in .env")
        con.close()
    except Exception as exc:
        res("FAIL", "oracle", str(exc))
        m = str(exc)
        if "ORA-01017" in m:
            print("         -> bad username/password")
        if any(c in m for c in ("ORA-12154", "ORA-12514", "DPY-6001", "DPY-4011")):
            print("         -> connect string / service name wrong or DB unreachable")

# ---------------------------------------------------------------- 5. AI endpoint
section("AI endpoint (Qwen chat)")
e = env("LLM_API_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_CA_BUNDLE", "LLM_VERIFY_SSL_DISABLE")
if not e["LLM_API_URL"]:
    res("SKIP", "ai chat", "set LLM_API_URL in .env")
else:
    try:
        import requests
        headers = {"Content-Type": "application/json"}
        if e["LLM_API_KEY"]:
            headers["Authorization"] = "Bearer " + e["LLM_API_KEY"]
        body = {"messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "temperature": 0, "max_tokens": 8, "stream": False}
        if e["LLM_MODEL"]:               # omit for the GGUF backend; include for public providers
            body["model"] = e["LLM_MODEL"]
        verify = True
        if e["LLM_CA_BUNDLE"]:
            verify = e["LLM_CA_BUNDLE"]
        if str(e["LLM_VERIFY_SSL_DISABLE"]).lower() in ("1", "true", "yes"):
            verify = False
        r = requests.post(e["LLM_API_URL"], json=body, headers=headers, verify=verify, timeout=60)
        snippet = ""
        try:
            snippet = r.json()["choices"][0]["message"]["content"].strip()[:60]
        except Exception:
            snippet = r.text[:80].replace("\n", " ")
        res("OK" if r.status_code == 200 else "FAIL", "ai chat",
            f"HTTP {r.status_code} | reply: {snippet!r}")
        if r.status_code == 400 and e["LLM_MODEL"]:
            print("         -> 400 with a model field set: GGUF backend? try unsetting LLM_MODEL")
    except Exception as exc:
        res("FAIL", "ai chat", str(exc))

# ---------------------------------------------------------------- 6. SMTP
section("SMTP relay")
e = env("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_SECURE", "MAIL_FROM", "MAIL_TO")
if not e["SMTP_HOST"]:
    res("SKIP", "smtp", "set SMTP_HOST (+ PORT/USER/PASS) in .env")
else:
    try:
        import smtplib
        port = int(e["SMTP_PORT"] or 587)
        secure = str(e["SMTP_SECURE"]).lower() in ("1", "true", "yes")
        srv = smtplib.SMTP_SSL(e["SMTP_HOST"], port, timeout=30) if secure else smtplib.SMTP(e["SMTP_HOST"], port, timeout=30)
        srv.ehlo()
        if not secure:
            try:
                srv.starttls(); srv.ehlo()
            except Exception:
                pass
        if e["SMTP_USER"]:
            srv.login(e["SMTP_USER"], e["SMTP_PASS"] or "")
            res("OK", "smtp login", f"{e['SMTP_HOST']}:{port}")
        else:
            res("OK", "smtp connect", f"{e['SMTP_HOST']}:{port} (no auth)")
        if "--send-mail" in sys.argv:
            if e["MAIL_FROM"] and e["MAIL_TO"]:
                from email.message import EmailMessage
                msg = EmailMessage()
                msg["From"] = e["MAIL_FROM"]; msg["To"] = e["MAIL_TO"]
                msg["Subject"] = "treasury-news diag test"
                msg.set_content("SMTP test from diag.py - if you see this, mail works.")
                srv.send_message(msg)
                res("OK", "smtp send-test", f"sent to {e['MAIL_TO']}")
            else:
                res("SKIP", "smtp send-test", "set MAIL_FROM and MAIL_TO")
        srv.quit()
    except Exception as exc:
        res("FAIL", "smtp", str(exc))

# ---------------------------------------------------------------- summary
section("SUMMARY")
print(f"  PASS: {len(_P)}   FAIL: {len(_F)}   SKIP: {len(_S)}")
if _F:
    print("  Failed:", ", ".join(_F))
if _S:
    print("  Skipped (env not set / optional):", ", ".join(_S))
print("\nPaste this whole output back so the build targets your real environment.")
sys.exit(1 if _F else 0)
