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


# ---------------------------------------------------------------- text extraction
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
            title=:title, distribution_headline=:headline, publication_ts=:pub_ts,
            publication_date=:pub_date, authors=:authors, report_types=:rtypes,
            source_path=:src, download_path=:dl, total_pages=:pages, synopsis=:syn,
            scraped_at=SYSTIMESTAMP, status='STORED'
        WHEN NOT MATCHED THEN INSERT
            (report_id, title, distribution_headline, publication_ts, publication_date,
             authors, report_types, source_path, download_path, total_pages, synopsis,
             scraped_at, status)
        VALUES (:id, :title, :headline, :pub_ts, :pub_date, :authors, :rtypes, :src, :dl,
                :pages, :syn, SYSTIMESTAMP, 'STORED')
    """, {"id": rid, "title": meta.get("title"), "headline": meta.get("distributionHeadline"),
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
    metas = sorted(glob.glob(str(DOWNLOADS / "*" / "*.meta.json")))
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
            tlen, plen = store(con, meta, html, pdf_bytes)
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
