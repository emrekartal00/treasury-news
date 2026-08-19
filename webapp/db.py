"""webapp/db.py - read-only Oracle access for the viewer (python-oracledb thin mode).

Uses a small connection pool; a session callback sets CURRENT_SCHEMA (from DB_SCHEMA) so
queries stay unqualified. The web-app DB user only needs SELECT on the tables.
"""
import json
import os
import re
from collections import OrderedDict
from pathlib import Path

import oracledb

# Local hosting: load the repo-root .env (DB_USER/PASSWORD/CONNECT_STRING/DB_SCHEMA) if
# present. In a container the platform supplies env instead, and this simply no-ops.
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

oracledb.defaults.fetch_lobs = False  # BLOB -> bytes, N/CLOB -> str
_IDENT = re.compile(r"^[A-Za-z0-9_$#]+$")
_pool = None


def as_json(value):
    """Decode a JSON column whether it comes back as bytes, str, or already parsed."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return json.loads(value.decode("utf-8"))
    if isinstance(value, str):
        return json.loads(value)
    return value


def _schema():
    s = (os.environ.get("DB_SCHEMA") or "").strip()
    return s if s and _IDENT.match(s) else None


def _on_session(con, requested_tag):
    s = _schema()
    if s:
        cur = con.cursor()
        cur.execute("ALTER SESSION SET CURRENT_SCHEMA = " + s)  # not bindable
        cur.close()


def pool():
    global _pool
    if _pool is None:
        _pool = oracledb.create_pool(
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            dsn=os.environ["DB_CONNECT_STRING"],
            min=1, max=4, increment=1,
            session_callback=_on_session,
        )
    return _pool


def _rows(sql, binds=None):
    con = pool().acquire()
    try:
        cur = con.cursor()
        cur.execute(sql, binds or {})
        return cur.fetchall()
    finally:
        con.close()


def ping():
    _rows("SELECT 1 FROM dual")


def recent(limit=25, offset=0):
    """Earlier reports (excludes today's — those are shown grouped by source up top).
    Shows the report's own heading (title / distribution_headline), not the AI summary line."""
    rows = _rows("""
        SELECT r.report_id, r.title, r.distribution_headline,
               TO_CHAR(r.publication_date,'YYYY-MM-DD'),
               CASE WHEN p.report_id IS NOT NULL THEN 1 ELSE 0 END, r.source
        FROM reports r
        LEFT JOIN report_pdf p ON p.report_id = r.report_id
        WHERE r.publication_date < TRUNC(SYSDATE) OR r.publication_date IS NULL
        ORDER BY r.publication_ts DESC NULLS LAST
        OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY
    """, {"off": offset, "lim": limit})
    return [{"id": a, "title": b, "dhead": c, "date": d, "has_pdf": bool(e), "source": f}
            for a, b, c, d, e, f in rows]


def todays_by_source():
    """Today's reports (the DB's current date), grouped by source, newest first in each group.
    Shows the report's own heading, not the AI summary line."""
    rows = _rows("""
        SELECT r.source, r.report_id, TO_CHAR(r.publication_date,'YYYY-MM-DD'),
               r.title, r.distribution_headline,
               CASE WHEN p.report_id IS NOT NULL THEN 1 ELSE 0 END
        FROM reports r
        LEFT JOIN report_pdf p ON p.report_id = r.report_id
        WHERE r.publication_date = TRUNC(SYSDATE)
        ORDER BY r.source, r.publication_ts DESC NULLS LAST
    """)
    groups = OrderedDict()
    for src, rid, date, title, dhead, has_pdf in rows:
        groups.setdefault(src or "gs", []).append(
            {"id": rid, "date": date, "title": title, "dhead": dhead, "has_pdf": bool(has_pdf)})
    return groups


def search(term, limit=50):
    esc = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = _rows("""
        SELECT r.report_id, r.title, TO_CHAR(r.publication_date,'YYYY-MM-DD'), s.headline, r.source
        FROM reports r
        JOIN report_text rt ON rt.report_id = r.report_id
        LEFT JOIN report_summary s ON s.report_id = r.report_id
        WHERE LOWER(rt.search_key) LIKE :q ESCAPE '\\'
        ORDER BY r.publication_ts DESC NULLS LAST
        FETCH FIRST :lim ROWS ONLY
    """, {"q": "%" + esc + "%", "lim": limit})
    return [{"id": a, "title": b, "date": c, "headline": d, "source": e} for a, b, c, d, e in rows]


def get_report(rid):
    rows = _rows("""
        SELECT r.title, r.distribution_headline, r.authors,
               TO_CHAR(r.publication_date,'YYYY-MM-DD'), r.total_pages,
               r.source_path, r.download_path, s.summary_json,
               CASE WHEN p.report_id IS NOT NULL THEN 1 ELSE 0 END, r.source
        FROM reports r
        LEFT JOIN report_summary s ON s.report_id = r.report_id
        LEFT JOIN report_pdf p ON p.report_id = r.report_id
        WHERE r.report_id = :id
    """, {"id": rid})
    if not rows:
        return None
    title, headline, authors_b, date, pages, src, dl, sj, has_pdf, source = rows[0]
    return {
        "id": rid, "title": title, "headline": headline,
        "authors": as_json(authors_b) or [],
        "date": date, "pages": pages, "source_path": src, "download": dl,
        "source": source,
        "summary": as_json(sj),
        "has_pdf": bool(has_pdf),
    }


def get_pdf(rid):
    rows = _rows("SELECT pdf_blob FROM report_pdf WHERE report_id = :id", {"id": rid})
    return rows[0][0] if rows and rows[0][0] else None


def latest_digest_date():
    rows = _rows("SELECT TO_CHAR(MAX(digest_date),'YYYY-MM-DD') FROM daily_digest")
    return rows[0][0] if rows else None


def get_digest(date_str):
    rows = _rows("SELECT overview_json FROM daily_digest WHERE digest_date = TO_DATE(:d,'YYYY-MM-DD')",
                 {"d": date_str})
    if not rows or not rows[0][0]:
        return None
    return as_json(rows[0][0])
