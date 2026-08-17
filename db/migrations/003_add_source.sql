-- 003_add_source.sql - multi-source support: tag each report with its portal.
-- RUN AS THE SCHEMA OWNER in SQL Developer, as a SCRIPT (F5), after 001_init.sql. Safe to
-- run once (ALTER ... ADD errors if the column already exists - that just means it's applied).
--
-- report_id stays VARCHAR2(64): GS keeps its bare UUID; other portals namespace ids as
-- '<key>:<native>' (the adapter hashes the native id if that would exceed 64 chars).

ALTER TABLE reports ADD (source VARCHAR2(30) DEFAULT 'gs');

-- Existing rows predate multi-source (all from the original 'gs' portal).
UPDATE reports SET source = 'gs' WHERE source IS NULL;

CREATE INDEX ix_reports_source ON reports (source);

COMMIT;
