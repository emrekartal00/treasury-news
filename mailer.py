"""mailer.py - compose + send the daily digest email; idempotent via EMAIL_LOG.

Loads DAILY_DIGEST for a date, renders an HTML email (overview + one card per report,
with links to the web app), sends via SMTP, and records the result in EMAIL_LOG. Never
double-sends a day (unless --force).

Run:  python mailer.py [--date YYYY-MM-DD] [--force]
"""
import argparse
import json
import os
import smtplib
import uuid
from email.message import EmailMessage

import oracledb

import db_conn


def db_today(cur):
    cur.execute("SELECT TO_CHAR(SYSDATE, 'YYYY-MM-DD') FROM dual")
    return cur.fetchone()[0]


def already_sent(cur, date_str):
    cur.execute("SELECT COUNT(*) FROM email_log WHERE digest_date = TO_DATE(:d,'YYYY-MM-DD') AND status='SENT'",
                {"d": date_str})
    return cur.fetchone()[0] > 0


def load_digest(cur, date_str):
    cur.execute("SELECT overview_json, report_ids FROM daily_digest WHERE digest_date = TO_DATE(:d,'YYYY-MM-DD')",
                {"d": date_str})
    row = cur.fetchone()
    if not row:
        return None, []
    overview = db_conn.as_json(row[0]) or {}
    report_ids = db_conn.as_json(row[1]) or []
    return overview, report_ids


def load_report(cur, rid):
    cur.execute("""
        SELECT r.title, r.distribution_headline, r.authors, r.source_path, r.download_path, s.summary_json
        FROM reports r LEFT JOIN report_summary s ON s.report_id = r.report_id
        WHERE r.report_id = :id
    """, {"id": rid})
    row = cur.fetchone()
    if not row:
        return None
    title, headline, authors_b, src, dl, sj = row
    return {
        "id": rid, "title": title, "headline": headline,
        "authors": db_conn.as_json(authors_b) or [],
        "source": src, "download": dl,
        "summary": db_conn.as_json(sj) or {},
    }


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(overview, reports, base_url):
    date = overview.get("date", "")
    n = overview.get("counts", {}).get("total", len(reports))
    html = [f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:720px">',
            f"<h2>GS Research Digest - {esc(date)} - {n} report(s)</h2>"]
    text = [f"GS Research Digest - {date} - {n} report(s)", ""]
    for r in reports:
        s = r["summary"]
        link = f"{base_url}/reports/{r['id']}" if base_url else (r["source"] or "#")
        pdf = f"{base_url}/reports/{r['id']}/pdf" if base_url else (r["download"] or "#")
        title = r["title"] or r["headline"] or "(untitled)"
        html.append(f"<h3 style='margin-bottom:2px'><a href='{esc(link)}'>{esc(title)}</a></h3>")
        if r["authors"]:
            html.append(f"<div style='color:#666;font-size:13px'>{esc(', '.join(r['authors']))}</div>")
        if s.get("stance"):
            html.append(f"<div style='font-size:12px;color:#888'>stance: {esc(s.get('stance'))}</div>")
        if s.get("one_paragraph"):
            html.append(f"<p>{esc(s['one_paragraph'])}</p>")
        kps = s.get("key_points") or []
        if kps:
            html.append("<ul>" + "".join(f"<li>{esc(kp.get('point',''))}</li>" for kp in kps[:4]) + "</ul>")
        html.append(f"<div><a href='{esc(link)}'>View report</a> &nbsp;|&nbsp; <a href='{esc(pdf)}'>PDF</a></div><hr>")

        text.append(f"* {title}")
        if s.get("one_paragraph"):
            text.append(f"  {s['one_paragraph']}")
        text.append(f"  {link}")
        text.append("")
    html.append("<div style='color:#999;font-size:11px'>Internal use only.</div></div>")
    return "".join(html), "\n".join(text)


def send(subject, html, text):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or 587)
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    secure = str(os.environ.get("SMTP_SECURE", "")).lower() in ("1", "true", "yes")
    mail_from = os.environ.get("MAIL_FROM")
    to = os.environ.get("MAIL_TO")
    cc = os.environ.get("MAIL_CC")
    if not host or not mail_from or not to:
        raise RuntimeError("Set SMTP_HOST, MAIL_FROM, MAIL_TO in .env")

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    srv = smtplib.SMTP_SSL(host, port, timeout=30) if secure else smtplib.SMTP(host, port, timeout=30)
    try:
        srv.ehlo()
        if not secure:
            try:
                srv.starttls()
                srv.ehlo()
            except Exception:
                pass
        if user:
            srv.login(user, pw or "")
        srv.send_message(msg)
    finally:
        srv.quit()
    return msg.get("Message-ID") or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--force", action="store_true")
    args, _ = ap.parse_known_args()

    con = db_conn.connect()
    cur = con.cursor()
    date_str = args.date or db_today(cur)

    if not args.force and already_sent(cur, date_str):
        print(f"Email already SENT for {date_str}; skipping (use --force).")
        con.close()
        return

    overview, ids = load_digest(cur, date_str)
    if not ids:
        print(f"No digest/reports for {date_str}; nothing to send.")
        con.close()
        return

    reports = [r for r in (load_report(cur, rid) for rid in ids) if r]
    base = os.environ.get("WEBAPP_BASE_URL", "").rstrip("/")
    subject = f"GS Research Digest - {date_str} - {len(reports)} report(s)"
    html, text = render(overview, reports, base)

    log_id = str(uuid.uuid4())
    recipients = os.environ.get("MAIL_TO", "")
    try:
        mid = send(subject, html, text)
        cur.setinputsizes(rcpt=oracledb.DB_TYPE_NCLOB, subj=oracledb.DB_TYPE_NVARCHAR)
        cur.execute("""
            INSERT INTO email_log (id, digest_date, subject, recipients, status, message_id, sent_at)
            VALUES (:id, TO_DATE(:d,'YYYY-MM-DD'), :subj, :rcpt, 'SENT', :mid, SYSTIMESTAMP)
        """, {"id": log_id, "d": date_str, "subj": subject[:500], "rcpt": recipients,
              "mid": (mid or "")[:300]})
        con.commit()
        print(f"Sent digest for {date_str} to {recipients} ({len(reports)} reports).")
    except Exception as exc:
        cur.setinputsizes(err=oracledb.DB_TYPE_NCLOB, rcpt=oracledb.DB_TYPE_NCLOB,
                          subj=oracledb.DB_TYPE_NVARCHAR)
        cur.execute("""
            INSERT INTO email_log (id, digest_date, subject, recipients, status, error, sent_at)
            VALUES (:id, TO_DATE(:d,'YYYY-MM-DD'), :subj, :rcpt, 'FAILED', :err, SYSTIMESTAMP)
        """, {"id": log_id, "d": date_str, "subj": subject[:500], "rcpt": recipients,
              "err": str(exc)[:2000]})
        con.commit()
        print(f"[FAIL] send: {exc}")
        con.close()
        raise
    con.close()


if __name__ == "__main__":
    main()
