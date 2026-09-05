-- Post-writer strengthening phase for Company News effective-date precision.
-- Prepared for review only; run only after every writer emits precision fields
-- and a read-only audit confirms there are no NULL precision rows.
BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM canonical_news_events
    WHERE valid_from_precision IS NULL OR valid_until_precision IS NULL
  ) THEN
    RAISE EXCEPTION 'effective-date precision backfill/writer rollout incomplete';
  END IF;
END $$;

ALTER TABLE canonical_news_events
  DROP CONSTRAINT IF EXISTS canonical_news_events_valid_from_precision_compat_check,
  DROP CONSTRAINT IF EXISTS canonical_news_events_valid_until_precision_compat_check;

ALTER TABLE canonical_news_events
  ADD CONSTRAINT canonical_news_events_valid_from_precision_check CHECK (
    (valid_from_precision = 'datetime' AND valid_from IS NOT NULL) OR
    (valid_from_precision = 'date' AND valid_from IS NULL AND valid_from_date IS NOT NULL) OR
    (valid_from_precision = 'unknown' AND valid_from IS NULL AND valid_from_date IS NULL)
  ) NOT VALID,
  ADD CONSTRAINT canonical_news_events_valid_until_precision_check CHECK (
    (valid_until_precision = 'datetime' AND valid_until IS NOT NULL) OR
    (valid_until_precision = 'date' AND valid_until IS NULL AND valid_until_date IS NOT NULL) OR
    (valid_until_precision = 'unknown' AND valid_until IS NULL AND valid_until_date IS NULL)
  ) NOT VALID;

ALTER TABLE canonical_news_events
  VALIDATE CONSTRAINT canonical_news_events_valid_from_precision_check;
ALTER TABLE canonical_news_events
  VALIDATE CONSTRAINT canonical_news_events_valid_until_precision_check;

ALTER TABLE canonical_news_events
  ALTER COLUMN valid_from_precision SET NOT NULL,
  ALTER COLUMN valid_until_precision SET NOT NULL;

COMMIT;
