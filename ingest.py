"""ingest.py - read downloaded items, extract text, MERGE into Oracle.

Reads downloads/YYYY-MM-DD/*.meta.json (produced by daily.py), extracts clean text from
the sibling .html, and upserts REPORTS + REPORT_TEXT + REPORT_PDF (BLOB). Idempotent:
already-STORED reports are skipped.

Run:  python ingest.py
"""
import datetime
import glob
import json
import os
from pathlib import Path

import oracledb

import db_conn

HERE = Path(__file__).parent
DOWNLOADS = HERE / "downloads"

# Below this many chars, the HTML is treated as a JS shell (e.g. JPM's /research/content
# returns an app shell, not the article) and we fall back to the PDF for the body text.
MIN_HTML_TEXT_CHARS = 400


# ---------------------------------------------------------------- text extraction
def extract_pdf_text(pdf_bytes):
    """Extract plain text from a PDF - the body-text fallback when the HTML is a JS shell.
    Best-effort: returns '' if pypdf is missing or the PDF can't be parsed."""
    try:
        import io

        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for pg in reader.pages:
            try:
                parts.append(pg.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts)
    except Exception as exc:
        print(f"    (pdf text extraction failed: {exc})")
        return ""
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_text(html):
    """HTML -> clean plain text. Tries selectolax, then bs4, then a crude regex fallback."""
    text = None
    try:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        for node in tree.css("script,style,noscript,nav,header,footer"):
            node.decompose()
        body = tree.body or tree.root
        text = body.text(separator="\n") if body else tree.text()
    except Exception:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for node in soup(["script", "style", "noscript", "nav", "header", "footer"]):
                node.decompose()
            text = soup.get_text("\n")
        except Exception:
            import re
            text = re.sub(r"<[^>]+>", " ", html)
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def build_search_key(meta):
    parts = [
        meta.get("title") or "",
        meta.get("distributionHeadline") or "",
        " ".join(meta.get("authors") or []),
        meta.get("synopsis") or "",
    ]
    return " ".join(parts).lower()[:2000]


def ms_to_dt(ms):
    if not ms:
        return None, None
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return dt, dt.date()


# ---------------------------------------------------------------- storage
def already_stored(cur, rid):
    cur.execute("SELECT status FROM reports WHERE report_id = :id", {"id": rid})
    row = cur.fetchone()
    return row is not None and row[0] in ("STORED", "SUMMARIZED")


def store(con, meta, html, pdf_bytes):
    cur = con.cursor()
    rid = meta["id"]
    pub_ts, pub_date = ms_to_dt(meta.get("publicationDateTime"))
    plain = extract_text(html) if html else ""
    # If the HTML gave us almost nothing (JS shell), use the PDF as the body-text source.
    if len(plain) < MIN_HTML_TEXT_CHARS and pdf_bytes:
        pdf_text = extract_pdf_text(pdf_bytes)
        if len(pdf_text) > len(plain):
            plain = pdf_text
    # Nothing usable (e.g. a text-less MS video/calendar card) - skip so it never reaches
    # the summarizer as an empty report.
    if not plain and not pdf_bytes:
        cur.close()
        return None
    search_key = build_search_key(meta)
    authors_b = json.dumps(meta.get("authors") or [], ensure_ascii=False).encode("utf-8")
    rtypes_b = json.dumps(meta.get("reportTypes") or [], ensure_ascii=False).encode("utf-8")

    # REPORTS - NVARCHAR2 text must bind as national charset (else non-Latin chars are
    # lost in transit through the single-byte DB charset); JSON is BLOB; synopsis NCLOB.
    cur.setinputsizes(title=oracledb.DB_TYPE_NVARCHAR, headline=oracledb.DB_TYPE_NVARCHAR,
                      authors=oracledb.DB_TYPE_BLOB, rtypes=oracledb.DB_TYPE_BLOB,
                      syn=oracledb.DB_TYPE_NCLOB)
    cur.execute("""
        MERGE INTO reports t USING (SELECT :id AS report_id FROM dual) s
        ON (t.report_id = s.report_id)
        WHEN MATCHED THEN UPDATE SET
            source=:source, title=:title, distribution_headline=:headline,
            publication_ts=:pub_ts, publication_date=:pub_date, authors=:authors,
            report_types=:rtypes, source_path=:src, download_path=:dl, total_pages=:pages,
            synopsis=:syn, scraped_at=SYSTIMESTAMP, status='STORED'
        WHEN NOT MATCHED THEN INSERT
            (report_id, source, title, distribution_headline, publication_ts, publication_date,
             authors, report_types, source_path, download_path, total_pages, synopsis,
             scraped_at, status)
        VALUES (:id, :source, :title, :headline, :pub_ts, :pub_date, :authors, :rtypes,
                :src, :dl, :pages, :syn, SYSTIMESTAMP, 'STORED')
    """, {"id": rid, "source": meta.get("source") or "gs",
          "title": meta.get("title"), "headline": meta.get("distributionHeadline"),
          "pub_ts": pub_ts, "pub_date": pub_date, "authors": authors_b, "rtypes": rtypes_b,
          "src": meta.get("htmlUrl"), "dl": meta.get("pdfUrl"),
          "pages": meta.get("totalPages"), "syn": meta.get("synopsis")})

    # REPORT_TEXT - plain_text NCLOB; search_key is NVARCHAR2 (bind as national charset).
    cur.setinputsizes(pt=oracledb.DB_TYPE_NCLOB, sk=oracledb.DB_TYPE_NVARCHAR)
    cur.execute("""
        MERGE INTO report_text t USING (SELECT :id AS report_id FROM dual) s
        ON (t.report_id = s.report_id)
        WHEN MATCHED THEN UPDATE SET plain_text=:pt, search_key=:sk, char_len=:cl,
                                     extracted_at=SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (report_id, plain_text, search_key, char_len, extracted_at)
        VALUES (:id, :pt, :sk, :cl, SYSTIMESTAMP)
    """, {"id": rid, "pt": plain, "sk": search_key, "cl": len(plain)})

    # REPORT_PDF - pdf_blob is a large BLOB.
    if pdf_bytes:
        cur.setinputsizes(blob=oracledb.DB_TYPE_BLOB)
        cur.execute("""
            MERGE INTO report_pdf t USING (SELECT :id AS report_id FROM dual) s
            ON (t.report_id = s.report_id)
            WHEN MATCHED THEN UPDATE SET pdf_blob=:blob, pdf_bytes=:n, stored_at=SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT (report_id, pdf_blob, pdf_bytes, mime, stored_at)
            VALUES (:id, :blob, :n, 'application/pdf', SYSTIMESTAMP)
        """, {"id": rid, "blob": pdf_bytes, "n": len(pdf_bytes)})

    con.commit()
    cur.close()
    return len(plain), (len(pdf_bytes) if pdf_bytes else 0)


def main():
    # Recursive: handles both downloads/<date>/ (legacy) and downloads/<key>/<date>/.
    metas = sorted(glob.glob(str(DOWNLOADS / "**" / "*.meta.json"), recursive=True))
    if not metas:
        print("No downloaded items under downloads/. Run daily.py first.")
        return
    con = db_conn.connect()
    cur = con.cursor()
    stored = skipped = failed = 0
    for mp in metas:
        try:
            meta = json.loads(Path(mp).read_text(encoding="utf-8"))
            rid = meta.get("id")
            if not rid:
                continue
            if already_stored(cur, rid):
                skipped += 1
                continue
            base = mp[: -len(".meta.json")]
            html = Path(base + ".html").read_text(encoding="utf-8") if os.path.exists(base + ".html") else ""
            pdf_path = base + ".pdf"
            pdf_bytes = Path(pdf_path).read_bytes() if os.path.exists(pdf_path) else None
            result = store(con, meta, html, pdf_bytes)
            if result is None:
                skipped += 1
                print(f"  skipped {rid[:8]}  (no text/pdf)  {meta.get('title')}")
                continue
            tlen, plen = result
            stored += 1
            print(f"  stored {rid[:8]}  text={tlen}c pdf={plen}b  {meta.get('title')}")
        except Exception as exc:
            failed += 1
            print(f"  FAILED {os.path.basename(mp)}: {exc}")
    cur.close()
    con.close()
    print(f"\nIngest done. stored={stored} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
