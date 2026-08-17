-- 002_grants.sql — grant DML/SELECT from the schema OWNER to the app accounts.
-- RUN AS THE SCHEMA OWNER, after 001_init.sql.
-- Replace &PIPELINE_USER and &WEBAPP_USER with the real usernames before running
-- (or set them in SQL Developer when prompted).
--
-- If you don't have separate app users yet, you can skip this for now and let the
-- pipeline connect AS the owner during early development — then tighten later.

-- Pipeline (local box): reads + writes.
GRANT SELECT, INSERT, UPDATE ON reports        TO &PIPELINE_USER;
GRANT SELECT, INSERT, UPDATE ON report_pdf     TO &PIPELINE_USER;
GRANT SELECT, INSERT, UPDATE ON report_text    TO &PIPELINE_USER;
GRANT SELECT, INSERT, UPDATE ON report_summary TO &PIPELINE_USER;
GRANT SELECT, INSERT, UPDATE ON daily_digest   TO &PIPELINE_USER;
GRANT SELECT, INSERT, UPDATE ON email_log      TO &PIPELINE_USER;

-- Web app (OpenShift): SELECT-only (internet-adjacent tier, never writes).
GRANT SELECT ON reports        TO &WEBAPP_USER;
GRANT SELECT ON report_pdf     TO &WEBAPP_USER;
GRANT SELECT ON report_text    TO &WEBAPP_USER;
GRANT SELECT ON report_summary TO &WEBAPP_USER;
GRANT SELECT ON daily_digest   TO &WEBAPP_USER;
GRANT SELECT ON email_log      TO &WEBAPP_USER;

-- The app users then reference tables as OWNER.reports, or create synonyms:
-- CREATE OR REPLACE SYNONYM reports FOR <owner>.reports;   (run as each app user)
