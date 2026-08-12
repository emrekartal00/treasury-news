"""db/smoke_test.py — first Python <-> Oracle contact. LOCAL ONLY (references schema).

Run on the machine that reaches Oracle, after filling DB_* in ../.env:
  python db/smoke_test.py

Verifies: connect (thin mode) -> the 6 tables exist -> BLOB write/read round-trip ->
NVARCHAR2 Unicode round-trip (em-dash + Turkish must survive) -> BLOB-as-JSON. Cleans up.
"""
import json
import os
import sys
import uuid
from pathlib import Path

import oracledb

# --- load ../.env (no external dep) ---
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

USER = os.environ.get("DB_USER")
PW = os.environ.get("DB_PASSWORD")
DSN = os.environ.get("DB_CONNECT_STRING")
if not all([USER, PW, DSN]):
    print("✗ Set DB_USER, DB_PASSWORD, DB_CONNECT_STRING in .env first.")
    sys.exit(1)

oracledb.defaults.fetch_lobs = False  # BLOB -> bytes, N/CLOB -> str, directly

EXPECTED = ["REPORTS", "REPORT_PDF", "REPORT_TEXT", "REPORT_SUMMARY", "DAILY_DIGEST", "EMAIL_LOG"]
RID = "smoke-" + uuid.uuid4().hex[:8]
TITLE = "Smoke — Carry Trades · Türkçe: şğıöçü"   # em-dash + Turkish
AUTHORS = ["Ada Lovelace", "Émile"]
PDF = b"%PDF-1.4\nsmoke test\n%%EOF"

_passed = _failed = 0


def ok(msg):
    global _passed
    _passed += 1
    print("  ✓", msg)


def bad(msg):
    global _failed
    _failed += 1
    print("  ✗", msg)


con = None
try:
    con = oracledb.connect(user=USER, password=PW, dsn=DSN)
    print("Connected (thin mode).")
    cur = con.cursor()

    cur.execute("SELECT 1 FROM dual")
    ok("SELECT 1 FROM dual") if cur.fetchone()[0] == 1 else bad("dual query")

    binds = {f"t{i}": name for i, name in enumerate(EXPECTED)}
    cur.execute(
        "SELECT table_name FROM user_tables WHERE table_name IN (%s)"
        % ",".join(":" + k for k in binds),
        binds,
    )
    found = {r[0] for r in cur.fetchall()}
    for name in EXPECTED:
        ok(f"table {name}") if name in found else bad(f"MISSING table {name}")

    cur.execute(
        "INSERT INTO reports (report_id, title, authors, status, scraped_at) "
        "VALUES (:id, :title, :authors, 'SMOKE', SYSTIMESTAMP)",
        {"id": RID, "title": TITLE, "authors": json.dumps(AUTHORS).encode("utf-8")},
    )
    cur.execute(
        "INSERT INTO report_pdf (report_id, pdf_blob, pdf_bytes, stored_at) "
        "VALUES (:id, :blob, :n, SYSTIMESTAMP)",
        {"id": RID, "blob": PDF, "n": len(PDF)},
    )
    con.commit()

    cur.execute(
        "SELECT r.title, r.authors, p.pdf_blob FROM reports r "
        "JOIN report_pdf p ON p.report_id = r.report_id WHERE r.report_id = :id",
        {"id": RID},
    )
    title, authors_b, pdf_b = cur.fetchone()

    (ok("NVARCHAR2 Unicode round-trip intact (em-dash + Turkish)")
     if title == TITLE else bad(f'Unicode CORRUPTED: got "{title}"'))
    (ok(f"BLOB round-trip intact ({len(pdf_b)} bytes)")
     if pdf_b == PDF else bad("BLOB mismatch"))
    (ok("BLOB-as-JSON (authors IS JSON) round-trip intact")
     if json.loads(authors_b.decode("utf-8")) == AUTHORS else bad("JSON mismatch"))
except Exception as exc:
    bad(f"ERROR: {exc}")
    msg = str(exc)
    if "ORA-01017" in msg:
        print("    → bad username/password")
    if any(code in msg for code in ("ORA-12154", "DPY-6001", "ORA-12514", "DPY-4011")):
        print("    → connect string / service name wrong or DB unreachable")
finally:
    if con is not None:
        try:
            cur.execute("DELETE FROM report_pdf WHERE report_id = :id", {"id": RID})
            cur.execute("DELETE FROM reports WHERE report_id = :id", {"id": RID})
            con.commit()
            print("Cleaned up test rows.")
        except Exception:
            pass
        con.close()
    print(f"\n{'✅ ALL GOOD' if _failed == 0 else '❌ FAILURES'} — {_passed} passed, {_failed} failed.")
    sys.exit(0 if _failed == 0 else 1)
