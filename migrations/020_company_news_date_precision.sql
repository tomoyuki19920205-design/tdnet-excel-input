-- Reader-first Company News publication precision migration.
-- Prepared for review only; Phase1.8h does not execute this against Production.
BEGIN;

ALTER TABLE canonical_news_events
  ADD COLUMN IF NOT EXISTS published_date DATE NULL,
  ADD COLUMN IF NOT EXISTS published_precision TEXT NULL;

-- Existing TIMESTAMPTZ values are exact instants.  Do not derive a calendar
-- date because PostgreSQL no longer retains the source offset used on input.
UPDATE canonical_news_events
SET published_precision = 'datetime'
WHERE published_precision IS NULL AND published_at IS NOT NULL;

ALTER TABLE canonical_news_events
  ALTER COLUMN published_precision SET DEFAULT 'datetime',
  ALTER COLUMN published_precision SET NOT NULL,
  ALTER COLUMN published_at DROP NOT NULL;

ALTER TABLE canonical_news_events
  DROP CONSTRAINT IF EXISTS canonical_news_events_publication_precision_check;

ALTER TABLE canonical_news_events
  ADD CONSTRAINT canonical_news_events_publication_precision_check CHECK (
    (published_precision = 'datetime' AND published_at IS NOT NULL)
    OR
    (published_precision = 'date' AND published_at IS NULL AND published_date IS NOT NULL)
    OR
    (published_precision = 'unknown' AND published_at IS NULL AND published_date IS NULL)
  ) NOT VALID;

ALTER TABLE canonical_news_events
  VALIDATE CONSTRAINT canonical_news_events_publication_precision_check;

CREATE INDEX IF NOT EXISTS ix_news_events_ticker_publication_date
  ON canonical_news_events(ticker, published_date DESC, published_at DESC);

CREATE OR REPLACE VIEW api_latest_news_events AS
SELECT n.event_id, n.ticker, c.name_ja AS company_name, n.headline,
       n.published_at, n.checked_at,
       n.source_type, n.source_name, n.source_url,
       category, direction, importance, earnings_relevance, summary, why_it_matters,
       evidence_excerpt, temporal_status, valid_until, tags, n.created_at,
       CASE importance WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END AS importance_rank,
       n.published_date, n.published_precision
FROM canonical_news_events n
LEFT JOIN companies c ON c.ticker_code = n.ticker;

COMMENT ON COLUMN canonical_news_events.published_at IS
  'Actual publication instant when published_precision=datetime; never a fabricated representative time.';
COMMENT ON COLUMN canonical_news_events.published_date IS
  'Source-supported publication calendar date; null when unavailable.';
COMMENT ON COLUMN canonical_news_events.published_precision IS
  'Publication evidence precision: datetime, date, or unknown.';

COMMIT;
