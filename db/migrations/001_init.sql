-- 001_init.sql — treasury-news schema (Oracle 19c, non-Unicode DB)
-- RUN AS THE SCHEMA OWNER in SQL Developer, as a SCRIPT (F5).
-- Owner needs: CREATE TABLE, CREATE INDEX, CREATE SEQUENCE, and tablespace QUOTA.
--
-- Unicode safety (DB charset is single-byte WE8ISO8859):
--   * report text  -> NVARCHAR2 / NCLOB  (Unicode via AL16UTF16)
--   * JSON         -> BLOB (UTF-8 bytes) + IS JSON   (NCLOB can't take IS JSON on 19c)
-- No Oracle Text here (unavailable) — search is LIKE over report_text.search_key.

------------------------------------------------------------------- reports
CREATE TABLE reports (
  report_id             VARCHAR2(64)  CONSTRAINT pk_reports PRIMARY KEY,
  title                 NVARCHAR2(1000),
  distribution_headline NVARCHAR2(2000),
  publication_ts        TIMESTAMP WITH TIME ZONE,
  publication_date      DATE,
  authors               BLOB CONSTRAINT ck_reports_authors CHECK (authors IS JSON),
  report_types          BLOB CONSTRAINT ck_reports_rtypes  CHECK (report_types IS JSON),
  source_path           VARCHAR2(500),
  download_path         VARCHAR2(500),
  total_pages           NUMBER,
  synopsis              NCLOB,
  scraped_at            TIMESTAMP WITH TIME ZONE,
  status                VARCHAR2(20) DEFAULT 'NEW'
);
CREATE INDEX ix_reports_pubdate ON reports (publication_date);
CREATE INDEX ix_reports_status  ON reports (status);

------------------------------------------------------------------- report_pdf (BLOB archive)
CREATE TABLE report_pdf (
  report_id  VARCHAR2(64) CONSTRAINT pk_report_pdf PRIMARY KEY
                          CONSTRAINT fk_report_pdf REFERENCES reports(report_id),
  pdf_blob   BLOB,
  pdf_bytes  NUMBER,
  mime       VARCHAR2(60) DEFAULT 'application/pdf',
  stored_at  TIMESTAMP WITH TIME ZONE
);

------------------------------------------------------------------- report_text (AI input + search)
CREATE TABLE report_text (
  report_id    VARCHAR2(64) CONSTRAINT pk_report_text PRIMARY KEY
                            CONSTRAINT fk_report_text REFERENCES reports(report_id),
  plain_text   NCLOB,                 -- cleaned body: AI input + optional full-body search
  html_clob    NCLOB,                 -- optional full HTML archive (Decision Q7)
  search_key   NVARCHAR2(2000),       -- lowercased title+headline+authors+synopsis (LIKE search); NVARCHAR2 max is 2000 chars (=4000 bytes). Truncate when building.
  char_len     NUMBER,
  extracted_at TIMESTAMP WITH TIME ZONE
);

------------------------------------------------------------------- report_summary (validated JSON)
CREATE TABLE report_summary (
  report_id     VARCHAR2(64) CONSTRAINT pk_report_summary PRIMARY KEY
                             CONSTRAINT fk_report_summary REFERENCES reports(report_id),
  summary_json  BLOB CONSTRAINT ck_summary_json CHECK (summary_json IS JSON),
  headline      NVARCHAR2(2000),
  model         VARCHAR2(60),
  prompt_ver    VARCHAR2(20),
  input_tokens  NUMBER,
  generated_at  TIMESTAMP WITH TIME ZONE,
  status        VARCHAR2(20) DEFAULT 'OK'
);

------------------------------------------------------------------- daily_digest
CREATE TABLE daily_digest (
  digest_date   DATE CONSTRAINT pk_daily_digest PRIMARY KEY,
  overview_json BLOB CONSTRAINT ck_digest_overview CHECK (overview_json IS JSON),
  report_ids    BLOB CONSTRAINT ck_digest_reportids CHECK (report_ids IS JSON),
  generated_at  TIMESTAMP WITH TIME ZONE
);

------------------------------------------------------------------- email_log (idempotent send)
CREATE TABLE email_log (
  id           VARCHAR2(36) CONSTRAINT pk_email_log PRIMARY KEY,  -- app-generated UUID (uuid4 / crypto.randomUUID) — no sequence, avoids CREATE SEQUENCE priv
  digest_date  DATE,
  subject      NVARCHAR2(500),
  recipients   NCLOB,
  status       VARCHAR2(20),
  message_id   VARCHAR2(300),
  error        NCLOB,
  sent_at      TIMESTAMP WITH TIME ZONE
);
-- "One SENT email per day" is enforced in APPLICATION code (SELECT ... WHERE
-- digest_date=:d AND status='SENT' before sending), not by a DB index. Single daily job
-- on one box → no concurrency, so no DB-level unique constraint is needed here.

COMMIT;
