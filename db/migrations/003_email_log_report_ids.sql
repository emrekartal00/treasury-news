-- 003_email_log_report_ids.sql — remember WHICH reports each email carried, in email order.
-- RUN AS THE SCHEMA OWNER, once, after 001/002. Safe to run before or after the new
-- mailer.py: mailer.py only writes the column when it exists.
--
-- Why: curate.py picks and orders the reports that go into the email, but that list was
-- never stored (DAILY_DIGEST.report_ids is the full, uncurated list). The PRISMA library
-- (treasury-report customs/arastirma) shows "the latest email" at the top of its list
-- from this column. Past days cannot be backfilled (curation is not repeatable); for
-- those the library falls back to DAILY_DIGEST and says so.
--
-- JSON array of report_id strings, e.g. ["citi:123","jpm:456"]. BLOB + IS JSON, same
-- pattern as the other JSON columns (single-byte database character set).
ALTER TABLE email_log ADD (
  report_ids BLOB CONSTRAINT email_log_rids_json CHECK (report_ids IS JSON)
);

-- No new grant needed: 002 already grants SELECT/INSERT on email_log. The PRISMA
-- reader account needs SELECT on email_log too (same as the other five tables).
