"""webapp/app.py - read-only Flask viewer for the research archive.

Routes: catalog, LIKE search, report detail + summary, PDF stream from BLOB, day digest,
health/readiness. It only reads Oracle; never writes, never calls the AI or SMTP.
"""
import re

from flask import Flask, Response, abort, render_template, request

import db

app = Flask(__name__)
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


@app.route("/")
def index():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per = 25
    rows = db.recent(per, (page - 1) * per)
    date = db.latest_digest_date()
    digest = db.get_digest(date) if date else None
    return render_template("index.html", rows=rows, page=page, has_next=len(rows) == per,
                           digest=digest, digest_date=date)


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    rows = db.search(q) if q else []
    return render_template("search.html", q=q, rows=rows)


@app.route("/reports/<rid>")
def report(rid):
    if not _UUID.match(rid):
        abort(404)
    r = db.get_report(rid)
    if not r:
        abort(404)
    return render_template("report.html", r=r)


@app.route("/reports/<rid>/pdf")
def report_pdf(rid):
    if not _UUID.match(rid):
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
    app.run(host="0.0.0.0", port=8080, debug=True)
