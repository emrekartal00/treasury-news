"""webapp/app.py - read-only Flask viewer for the research archive.

Routes: catalog, LIKE search, report detail + summary, PDF stream from BLOB, day digest,
health/readiness. It only reads Oracle; never writes, never calls the AI or SMTP.
"""
import re

from flask import Flask, Response, abort, render_template, request

import db

app = Flask(__name__)
# Accept a bare GS UUID, or a namespaced multi-source id like "jpm:1a2b..." / "citi:ABC-1".
_RID = re.compile(r"^(?:[0-9a-fA-F-]{36}|[a-z0-9]{1,20}:[A-Za-z0-9._:-]{1,120})$")

# Source key -> display name (used by the `source_name` template filter).
SOURCE_NAMES = {
    "gs": "Goldman Sachs",
    "jpm": "J.P. Morgan",
    "citi": "Citi",
    "barc": "Barclays",
    "ms": "Morgan Stanley",
    "db": "Deutsche Bank",
}


@app.template_filter("source_name")
def source_name(key):
    key = (key or "").lower()
    return SOURCE_NAMES.get(key, key.upper())


@app.route("/")
def index():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per = 25
    rows = db.recent(per, (page - 1) * per)
    today = db.todays_by_source() if page == 1 else {}  # today's block only on the first page
    return render_template("index.html", rows=rows, page=page,
                           has_next=len(rows) == per, today=today)


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    rows = db.search(q) if q else []
    return render_template("search.html", q=q, rows=rows)


@app.route("/reports/<rid>")
def report(rid):
    if not _RID.match(rid):
        abort(404)
    r = db.get_report(rid)
    if not r:
        abort(404)
    return render_template("report.html", r=r)


@app.route("/reports/<rid>/pdf")
def report_pdf(rid):
    if not _RID.match(rid):
        abort(404)
    data = db.get_pdf(rid)
    if not data:
        abort(404)
    disposition = "attachment" if request.args.get("download") else "inline"
    return Response(data, mimetype="application/pdf", headers={
        "Content-Disposition": f'{disposition}; filename="{rid}.pdf"',
        "Cache-Control": "private, max-age=86400",
    })


@app.route("/digest/<date>")
def digest(date):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        abort(404)
    d = db.get_digest(date)
    if not d:
        abort(404)
    return render_template("digest.html", digest=d, digest_date=date)


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/readyz")
def readyz():
    try:
        db.ping()
        return "ready", 200
    except Exception as exc:  # pragma: no cover
        return f"not ready: {exc}", 503


if __name__ == "__main__":
    import os
    # Dev server bound to the LAN. debug is OFF (the debugger would let anyone on the
    # network run code). For a sturdier server use waitress (see webapp/README.md).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
