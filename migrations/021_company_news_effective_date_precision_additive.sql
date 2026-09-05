-- Reader-compatible additive phase for Company News effective-date precision.
-- Prepared for review only; Phase1.8i does not execute this against Production.
-- Apply after migration 020. Keep legacy writers running until they emit the
-- new fields; migration 022 is the separate post-writer strengthening phase.
BEGIN;

ALTER TABLE canonical_news_events
  ADD COLUMN IF NOT EXISTS valid_from_date DATE NULL,
  ADD COLUMN IF NOT EXISTS valid_from_precision TEXT NULL,
  ADD COLUMN IF NOT EXISTS valid_until_date DATE NULL,
  ADD COLUMN IF NOT EXISTS valid_until_precision TEXT NULL;

-- Existing TIMESTAMPTZ values remain byte-for-byte instants. PostgreSQL does
-- not retain enough source evidence to classify midnight as an original
-- instant or a promoted calendar date, so no DATE backfill is attempted.
UPDATE canonical_news_events
SET valid_from_precision = CASE
      WHEN valid_from IS NOT NULL THEN 'datetime' ELSE 'unknown' END
WHERE valid_from_precision IS NULL;

UPDATE canonical_news_events
SET valid_until_precision = CASE
      WHEN valid_until IS NOT NULL THEN 'datetime' ELSE 'unknown' END
WHERE valid_until_precision IS NULL;

ALTER TABLE canonical_news_events
  DROP CONSTRAINT IF EXISTS canonical_news_events_valid_from_precision_compat_check,
  DROP CONSTRAINT IF EXISTS canonical_news_events_valid_until_precision_compat_check;

-- NULL precision temporarily admits legacy writers during the reader-first
-- rollout. Non-NULL precision is always checked. Migration 022 removes this
-- compatibility state after the new writer is deployed and audited.
ALTER TABLE canonical_news_events
  ADD CONSTRAINT canonical_news_events_valid_from_precision_compat_check CHECK (
    valid_from_precision IS NULL OR
    (valid_from_precision = 'datetime' AND valid_from IS NOT NULL) OR
    (valid_from_precision = 'date' AND valid_from IS NULL AND valid_from_date IS NOT NULL) OR
    (valid_from_precision = 'unknown' AND valid_from IS NULL AND valid_from_date IS NULL)
  ) NOT VALID,
  ADD CONSTRAINT canonical_news_events_valid_until_precision_compat_check CHECK (
    valid_until_precision IS NULL OR
    (valid_until_precision = 'datetime' AND valid_until IS NOT NULL) OR
    (valid_until_precision = 'date' AND valid_until IS NULL AND valid_until_date IS NOT NULL) OR
    (valid_until_precision = 'unknown' AND valid_until IS NULL AND valid_until_date IS NULL)
  ) NOT VALID;

ALTER TABLE canonical_news_events
  VALIDATE CONSTRAINT canonical_news_events_valid_from_precision_compat_check;
ALTER TABLE canonical_news_events
  VALIDATE CONSTRAINT canonical_news_events_valid_until_precision_compat_check;

CREATE INDEX IF NOT EXISTS ix_news_events_valid_from_precision
  ON canonical_news_events(valid_from_date DESC, valid_from DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_valid_until_precision
  ON canonical_news_events(valid_until_date DESC, valid_until DESC);

-- Preserve every existing API column in its original position and append the
-- precision-aware reader fields.
CREATE OR REPLACE VIEW api_latest_news_events AS
SELECT n.event_id, n.ticker, c.name_ja AS company_name, n.headline,
       n.published_at, n.checked_at,
       n.source_type, n.source_name, n.source_url,
       category, direction, importance, earnings_relevance, summary, why_it_matters,
       evidence_excerpt, temporal_status, valid_until, tags, n.created_at,
       CASE importance WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END AS importance_rank,
       n.published_date, n.published_precision,
       n.valid_from_date, n.valid_from_precision,
       n.valid_until_date, n.valid_until_precision
FROM canonical_news_events n
LEFT JOIN companies c ON c.ticker_code = n.ticker;

COMMENT ON COLUMN canonical_news_events.valid_from IS
  'Exact effective instant only; null for date-only or unknown effective dates.';
COMMENT ON COLUMN canonical_news_events.valid_from_date IS
  'Source-supported calendar effective start date; never derived by inventing a time.';
COMMENT ON COLUMN canonical_news_events.valid_from_precision IS
  'Effective start evidence precision: datetime, date, or unknown.';
COMMENT ON COLUMN canonical_news_events.valid_until IS
  'Exact effective end instant only; null for date-only or unknown effective dates.';
COMMENT ON COLUMN canonical_news_events.valid_until_date IS
  'Source-supported calendar effective end date; never derived by inventing a time.';
COMMENT ON COLUMN canonical_news_events.valid_until_precision IS
  'Effective end evidence precision: datetime, date, or unknown.';

COMMIT;
