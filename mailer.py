"""mailer.py - compose + send the daily digest email; idempotent via EMAIL_LOG.

Loads DAILY_DIGEST for a date, renders an HTML email (overview + one card per report,
with links to the web app), sends it through the LOCAL OUTLOOK desktop app via COM
(pywin32) - no SMTP, sends as the logged-in Outlook/Exchange account - and records the
result in EMAIL_LOG. Never double-sends a day (unless --force).

Run:  python mailer.py [--date YYYY-MM-DD] [--force] [--preview]
  --preview opens the mail in Outlook for review instead of sending.
"""
import argparse
import json
import os
import uuid

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


# --- email styling (editorial research note; email-safe: tables + inline CSS + web-safe fonts) ---
_INK = "#1c1c1a"
_MUTED = "#6f6b64"
_BODY = "#2b2a27"
_HAIR = "#e6e3dc"
_ACCENT = "#0b5f57"
_ACCENT_DK = "#083f3a"
_CANVAS = "#f7f6f2"
_CARD = "#ffffff"
_SERIF = "Georgia,'Times New Roman',serif"
_SANS = "Arial,Helvetica,sans-serif"
_STANCE = {
    "bullish": ("Bullish", "#1c6b45"),
    "bearish": ("Bearish", "#9c2b2b"),
    "neutral": ("Neutral", "#6f6b64"),
    "mixed": ("Mixed", "#9a6a1c"),
    "n/a": ("N/A", "#8a857c"),
}


def _badge(stance):
    label, color = _STANCE.get((stance or "n/a").lower(), _STANCE["n/a"])
    return (f'<span style="font-family:{_SANS};font-size:11px;font-weight:bold;'
            f'letter-spacing:.08em;text-transform:uppercase;color:{color};'
            f'border:1px solid {color};border-radius:3px;padding:2px 8px;white-space:nowrap;">'
            f'{label}</span>')


def _buttons(read_href, pdf_href):
    # Padded blocks with a darker bottom edge -> read as pressable buttons even in Outlook
    # (which ignores border-radius but does render the border).
    cells = [f'<td bgcolor="{_ACCENT}" style="border-radius:6px;border-bottom:3px solid {_ACCENT_DK};'
             f'padding:13px 26px;text-align:center;mso-padding-alt:13px 26px;">'
             f'<a href="{esc(read_href)}" style="font-family:{_SANS};font-size:13px;font-weight:bold;'
             f'color:#ffffff;text-decoration:none;letter-spacing:.06em;text-transform:uppercase;">'
             f'Read report</a></td>']
    if pdf_href:
        cells.append('<td width="12" style="font-size:0;line-height:0;">&nbsp;</td>')
        cells.append(f'<td bgcolor="{_CARD}" style="border:1px solid {_ACCENT};'
                     f'border-bottom:3px solid {_ACCENT};border-radius:6px;padding:13px 22px;'
                     f'text-align:center;mso-padding-alt:13px 22px;">'
                     f'<a href="{esc(pdf_href)}" style="font-family:{_SANS};font-size:13px;'
                     f'font-weight:bold;color:{_ACCENT};text-decoration:none;letter-spacing:.06em;'
                     f'text-transform:uppercase;">PDF</a></td>')
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
            f'<tr>{"".join(cells)}</tr></table>')


def _key_points(kps):
    rows = []
    for kp in (kps or [])[:4]:
        pt = esc(kp.get("point", "") if isinstance(kp, dict) else str(kp))
        if not pt:
            continue
        rows.append(
            f'<tr><td valign="top" width="18" style="font-family:{_SERIF};font-size:15px;'
            f'line-height:1.55;color:{_ACCENT};">&#8212;</td>'
            f'<td style="font-family:{_SANS};font-size:14px;line-height:1.55;color:{_BODY};'
            f'padding-bottom:5px;">{pt}</td></tr>')
    if not rows:
        return ""
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            'style="margin-top:8px;">' + "".join(rows) + "</table>")


def _card(r, base_url):
    s = r.get("summary") or {}
    rid = r["id"]
    read = f"{base_url}/reports/{rid}" if base_url else (r.get("source") or "#")
    if base_url:
        pdf = f"{base_url}/reports/{rid}/pdf"
    else:
        pdf = r.get("download") or ""
    title = esc(r.get("title") or r.get("headline") or "(untitled)")
    authors = esc(", ".join(r["authors"])) if r.get("authors") else ""
    badge = _badge(s.get("stance")) if s.get("stance") else ""

    inner = [f'<a href="{esc(read)}" style="font-family:{_SANS};font-size:18px;line-height:1.35;'
             f'font-weight:bold;color:{_INK};text-decoration:none;">{title}</a>']
    if authors or badge:
        left = (f'<span style="font-family:{_SANS};font-size:12px;color:{_MUTED};">{authors}</span>'
                if authors else "")
        inner.append(
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            'style="margin:8px 0 12px;"><tr>'
            f'<td valign="middle">{left}</td>'
            f'<td valign="middle" align="right">{badge}</td></tr></table>')
    if s.get("one_paragraph"):
        inner.append(f'<div style="font-family:{_SANS};font-size:15px;line-height:1.6;'
                     f'color:{_BODY};">{esc(s["one_paragraph"])}</div>')
    elif not s:
        inner.append(f'<div style="font-family:{_SANS};font-size:13px;color:{_MUTED};'
                     f'font-style:italic;">Summary unavailable.</div>')
    kp_html = _key_points(s.get("key_points"))
    if kp_html:
        inner.append(kp_html)
    inner.append(f'<div style="margin-top:16px;">{_buttons(read, pdf)}</div>')

    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'bgcolor="{_CARD}" style="background:{_CARD};border:1px solid {_HAIR};'
            f'border-radius:6px;"><tr><td style="padding:24px 28px;">'
            f'{"".join(inner)}</td></tr></table>')


def render(overview, reports, base_url):
    date = esc(overview.get("date", ""))
    n = overview.get("counts", {}).get("total", len(reports))
    by = overview.get("counts", {}).get("by_stance", {}) or {}

    summ = []
    for st in ("bullish", "bearish", "neutral", "mixed"):
        if by.get(st):
            label, color = _STANCE[st]
            summ.append(f'<span style="color:{color};">{by[st]} {label.lower()}</span>')
    summ_html = (" &nbsp;&middot;&nbsp; " + " &nbsp; ".join(summ)) if summ else ""

    masthead = (
        f'<div style="font-family:{_SANS};font-size:11px;font-weight:bold;letter-spacing:.22em;'
        f'text-transform:uppercase;color:{_ACCENT};">Research Digest</div>'
        f'<div style="font-family:{_SERIF};font-size:26px;line-height:1.15;color:{_INK};'
        f'margin:6px 0 3px;">{date}</div>'
        f'<div style="font-family:{_SANS};font-size:13px;color:{_MUTED};">'
        f'{n} report{"s" if n != 1 else ""}{summ_html}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="margin:14px 0 22px;"><tr>'
        f'<td height="2" bgcolor="{_ACCENT}" style="font-size:0;line-height:0;">&nbsp;</td>'
        f'</tr></table>')

    cards = []
    for r in reports:
        cards.append(_card(r, base_url))
        cards.append('<div style="height:16px;line-height:16px;font-size:0;">&nbsp;</div>')

    digest_link = ""
    if base_url:
        digest_link = (f' &nbsp;&middot;&nbsp; <a href="{esc(base_url)}/digest/{date}" '
                       f'style="color:{_MUTED};text-decoration:underline;">View full digest</a>')
    footer = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="margin-top:8px;"><tr>'
        f'<td height="1" bgcolor="{_HAIR}" style="font-size:0;line-height:0;">&nbsp;</td></tr>'
        f'<tr><td style="padding:14px 0;font-family:{_SANS};font-size:11px;line-height:1.6;'
        f'color:{_MUTED};">Internal use only.{digest_link}</td></tr></table>')

    pre = f"{n} new GS research report{'s' if n != 1 else ''} for {overview.get('date','')}."
    preheader = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
                 f'font-size:1px;line-height:1px;color:{_CANVAS};">{esc(pre)}'
                 + "&nbsp;&zwnj;" * 30 + '</div>')

    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only">'
        '</head>'
        f'<body style="margin:0;padding:0;background:{_CANVAS};">'
        + preheader
        + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{_CANVAS}" style="background:{_CANVAS};"><tr>'
        '<td align="center" style="padding:30px 12px 44px;">'
        '<!--[if mso]><table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="640"><tr><td><![endif]-->'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" '
        'style="width:640px;max-width:640px;">'
        f'<tr><td style="padding:0 8px;">{masthead}{"".join(cards)}{footer}</td></tr></table>'
        '<!--[if mso]></td></tr></table><![endif]-->'
        '</td></tr></table></body></html>')

    # plain-text fallback (Outlook uses HTMLBody; kept for completeness)
    text = [f"GS Research Digest - {overview.get('date','')} - {n} report(s)", ""]
    for r in reports:
        s = r.get("summary") or {}
        text.append("* " + (r.get("title") or r.get("headline") or "(untitled)"))
        if s.get("one_paragraph"):
            text.append("  " + s["one_paragraph"])
        link = f"{base_url}/reports/{r['id']}" if base_url else (r.get("source") or "")
        if link:
            text.append("  " + link)
        text.append("")
    return html, "\n".join(text)


def send(subject, html, preview=False):
    """Send via the local Outlook desktop app (COM / pywin32). Windows + Outlook only.
    Sends as the logged-in Outlook/Exchange account - no SMTP credentials needed.
    MAIL_FROM (optional) sends on behalf of a shared mailbox you have permission for."""
    to = os.environ.get("MAIL_TO")
    cc = os.environ.get("MAIL_CC")
    sender = os.environ.get("MAIL_FROM")
    if not to:
        raise RuntimeError("Set MAIL_TO in .env")

    try:
        import pythoncom
        pythoncom.CoInitialize()  # safe if already initialized; needed in some contexts
    except Exception:
        pass
    import win32com.client  # pywin32

    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = to.replace(",", ";")   # Outlook separates recipients with ';'
    if cc:
        mail.CC = cc.replace(",", ";")
    if sender:
        mail.SentOnBehalfOfName = sender
    mail.Subject = subject
    mail.HTMLBody = html
    if preview:
        mail.Display(True)   # open in Outlook for review; does NOT send
        return "displayed"
    mail.Send()
    return "outlook"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--preview", action="store_true", help="open in Outlook instead of sending")
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
        result = send(subject, html, preview=args.preview)
        if args.preview:
            print(f"Opened digest for {date_str} in Outlook for review (not sent).")
            con.close()
            return
        cur.setinputsizes(rcpt=oracledb.DB_TYPE_NCLOB, subj=oracledb.DB_TYPE_NVARCHAR)
        cur.execute("""
            INSERT INTO email_log (id, digest_date, subject, recipients, status, message_id, sent_at)
            VALUES (:id, TO_DATE(:d,'YYYY-MM-DD'), :subj, :rcpt, 'SENT', :mid, SYSTIMESTAMP)
        """, {"id": log_id, "d": date_str, "subj": subject[:500], "rcpt": recipients,
              "mid": (result or "")[:300]})
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
