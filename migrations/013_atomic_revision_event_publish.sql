-- Stage forecast revision events by screener batch and replace them in the same
-- transaction that advances screener_current_batch.

CREATE TABLE IF NOT EXISTS public.forecast_revision_events_staging (
  batch_id text NOT NULL REFERENCES public.screener_batches(batch_id) ON DELETE CASCADE,
  ticker text NOT NULL,
  disclosure_id text NOT NULL,
  disclosed_at date NOT NULL,
  target_fiscal_year date NOT NULL,
  metric text NOT NULL,
  previous_value double precision,
  revised_value double precision NOT NULL,
  direction text NOT NULL CHECK (direction IN ('initial','upward','downward')),
  is_correction boolean NOT NULL DEFAULT false,
  is_split_only_change boolean NOT NULL DEFAULT false,
  source text NOT NULL,
  PRIMARY KEY (batch_id, ticker, disclosure_id, target_fiscal_year, metric, source)
);

ALTER TABLE public.forecast_revision_events_staging ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.forecast_revision_events_staging FROM PUBLIC, anon, authenticated;

DROP FUNCTION IF EXISTS public.publish_screener_batch(text, integer);

CREATE OR REPLACE FUNCTION public.publish_screener_batch(
  p_batch_id text,
  p_expected_rows integer,
  p_expected_revision_events integer
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  actual_rows integer;
  actual_revision_events integer;
BEGIN
  SELECT count(*) INTO actual_rows
  FROM public.screener_metrics
  WHERE batch_id = p_batch_id;

  SELECT count(*) INTO actual_revision_events
  FROM public.forecast_revision_events_staging
  WHERE batch_id = p_batch_id;

  IF actual_rows <> p_expected_rows THEN
    RAISE EXCEPTION 'screener batch row count mismatch: expected %, actual %',
      p_expected_rows, actual_rows;
  END IF;

  IF actual_revision_events <> p_expected_revision_events THEN
    RAISE EXCEPTION 'revision event count mismatch: expected %, actual %',
      p_expected_revision_events, actual_revision_events;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM public.screener_batches
    WHERE batch_id = p_batch_id
      AND status = 'building'
      AND expected_row_count = p_expected_rows
      AND revision_event_count = p_expected_revision_events
  ) THEN
    RAISE EXCEPTION 'screener batch is absent, not building, or manifest differs: %',
      p_batch_id;
  END IF;

  DELETE FROM public.forecast_revision_events WHERE source = 'jquants';
  INSERT INTO public.forecast_revision_events(
    ticker, disclosure_id, disclosed_at, target_fiscal_year, metric,
    previous_value, revised_value, direction, is_correction,
    is_split_only_change, source, updated_at
  )
  SELECT
    ticker, disclosure_id, disclosed_at, target_fiscal_year, metric,
    previous_value, revised_value, direction, is_correction,
    is_split_only_change, source, now()
  FROM public.forecast_revision_events_staging
  WHERE batch_id = p_batch_id;

  UPDATE public.screener_batches
  SET status = 'superseded'
  WHERE status = 'published' AND batch_id <> p_batch_id;

  UPDATE public.screener_batches
  SET status = 'published', row_count = actual_rows, published_at = now()
  WHERE batch_id = p_batch_id;

  INSERT INTO public.screener_current_batch(singleton, batch_id, updated_at)
  VALUES (true, p_batch_id, now())
  ON CONFLICT (singleton) DO UPDATE
  SET batch_id = EXCLUDED.batch_id, updated_at = EXCLUDED.updated_at;

  DELETE FROM public.forecast_revision_events_staging
  WHERE batch_id = p_batch_id;
END;
$$;

REVOKE ALL ON FUNCTION public.publish_screener_batch(text, integer, integer)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_screener_batch(text, integer, integer)
  TO service_role;
