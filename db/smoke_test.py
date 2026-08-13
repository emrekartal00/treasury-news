"""db/smoke_test.py - first Python <-> Oracle contact. LOCAL ONLY (references schema).

Run on the machine that reaches Oracle, after filling DB_* (incl. DB_SCHEMA) in ../.env:
  python db/smoke_test.py

Verifies: connect (thin mode) -> CURRENT_SCHEMA set -> the 6 tables exist in that schema
-> BLOB write/read -> NVARCHAR2 Unicode round-trip (em-dash + Turkish) -> BLOB-as-JSON.
Cleans up its test rows. ASCII-only output.
"""
import json
import os
import re
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
SCHEMA = (os.environ.get("DB_SCHEMA") or "").strip()
if not all([USER, PW, DSN]):
    print("Set DB_USER, DB_PASSWORD, DB_CONNECT_STRING in .env first.")
    sys.exit(1)

oracledb.defaults.fetch_lobs = False  # BLOB -> bytes, N/CLOB -> str

EXPECTED = ["REPORTS", "REPORT_PDF", "REPORT_TEXT", "REPORT_SUMMARY", "DAILY_DIGEST", "EMAIL_LOG"]
RID = "smoke-" + uuid.uuid4().hex[:8]
# Built from codepoints so THIS file stays pure ASCII (no transfer/encoding risk),
# while the runtime string still exercises em-dash (2014), middot (B7) and Turkish.
TITLE = ("Smoke " + chr(0x2014) + " Carry Trades " + chr(0xB7) + " T" + chr(0xFC)
         + "rk" + chr(0xE7) + "e: " + chr(0x15F) + chr(0x11F) + chr(0x131)
         + chr(0xF6) + chr(0xE7) + chr(0xFC))
AUTHORS = ["Ada Lovelace", chr(0xC9) + "mile"]
PDF = b"%PDF-1.4\nsmoke test\n%%EOF"

_passed = _failed = 0


def ok(msg):
    global _passed
    _passed += 1
    print("  [ok]  ", msg)


def bad(msg):
    global _failed
    _failed += 1
    print("  [FAIL]", msg)


def as_json(value):
    if isinstance(value, (bytes, bytearray)):
        return json.loads(value.decode("utf-8"))
    if isinstance(value, str):
        return json.loads(value)
    return value  # BLOB IS JSON may come back already parsed


con = None
try:
    con = oracledb.connect(user=USER, password=PW, dsn=DSN)
    print("Connected (thin mode).")
    cur = con.cursor()
    if SCHEMA:
        if not re.match(r"^[A-Za-z0-9_$#]+$", SCHEMA):
            raise ValueError(f"unsafe DB_SCHEMA: {SCHEMA!r}")
        cur.execute("ALTER SESSION SET CURRENT_SCHEMA = " + SCHEMA)  # not bindable

    cur.execute("SELECT 1 FROM dual")
    ok("SELECT 1 FROM dual") if cur.fetchone()[0] == 1 else bad("dual query")

    # Tables live in another schema -> look in all_tables by owner, check membership.
    cur.execute("SELECT table_name FROM all_tables WHERE owner = :owner",
                {"owner": (SCHEMA or USER).upper()})
    found = {r[0] for r in cur.fetchall()}
    for name in EXPECTED:
        ok(f"table {name}") if name in found else bad(f"MISSING table {name}")

    # NVARCHAR2 text must bind as national charset, else non-Latin chars are lost.
    cur.setinputsizes(title=oracledb.DB_TYPE_NVARCHAR)
    cur.execute(
        "INSERT INTO reports (report_id, title, authors, status, scraped_at) "
        "VALUES (:id, :title, :authors, 'SMOKE', SYSTIMESTAMP)",
        {"id": RID, "title": TITLE, "authors": json.dumps(AUTHORS).encode("utf-8")},
    )
    cur.setinputsizes(blob=oracledb.DB_TYPE_BLOB)
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
    title, authors_v, pdf_b = cur.fetchone()

    (ok("NVARCHAR2 Unicode round-trip intact (em-dash + Turkish)")
     if title == TITLE else bad(f'Unicode CORRUPTED: got "{title}"'))
    (ok(f"BLOB round-trip intact ({len(pdf_b)} bytes)")
     if pdf_b == PDF else bad("BLOB mismatch"))
    (ok("BLOB-as-JSON (authors IS JSON) round-trip intact")
     if as_json(authors_v) == AUTHORS else bad("JSON mismatch"))
except Exception as exc:
    bad(f"ERROR: {exc}")
    msg = str(exc)
    if "ORA-01017" in msg:
        print("    -> bad username/password")
    if any(code in msg for code in ("ORA-12154", "DPY-6001", "ORA-12514", "DPY-4011")):
        print("    -> connect string / service name wrong or DB unreachable")
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
    print(f"\n{'ALL GOOD' if _failed == 0 else 'FAILURES'} - {_passed} passed, {_failed} failed.")
    sys.exit(0 if _failed == 0 else 1)
