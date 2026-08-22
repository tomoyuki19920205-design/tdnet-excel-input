-- Snapshot-backed Company Viewer stock screener.
-- Additive only: no existing table or column changes meaning.

CREATE TABLE IF NOT EXISTS public.screener_batches (
  batch_id text PRIMARY KEY,
  universe_date date NOT NULL,
  status text NOT NULL DEFAULT 'building'
    CHECK (status IN ('building', 'published', 'failed', 'superseded')),
  expected_row_count integer NOT NULL CHECK (expected_row_count > 0),
  row_count integer,
  revision_event_count integer,
  coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
  failure_reason text,
  calculated_at timestamptz NOT NULL,
  published_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.screener_metrics (
  batch_id text NOT NULL REFERENCES public.screener_batches(batch_id) ON DELETE CASCADE,
  ticker text NOT NULL,
  company_name text,
  universe_date date NOT NULL,
  price_as_of date,
  financial_as_of date,
  forecast_as_of date,
  master_as_of timestamptz,
  calculated_at timestamptz NOT NULL,
  price_status text NOT NULL CHECK (price_status IN ('current','no_trade','stale_unknown','source_ineligible','missing')),
  price_stale_sessions integer,
  price_stale_calendar_days integer,
  latest_valid_price double precision,
  market_cap double precision,
  market_code text,
  market_name text,
  sector17_code text,
  sector17_name text,
  sector33_code text,
  sector33_name text,
  accounting_standard text,
  forward_per double precision,
  actual_per double precision,
  actual_dividend_yield_pct double precision,
  forecast_dividend_yield_pct double precision,
  actual_sales_growth_yoy_pct double precision,
  forecast_sales_growth_yoy_pct double precision,
  equity_ratio_pct double precision,
  bullish_candle_ratio_5d_pct double precision,
  bullish_candle_ratio_10d_pct double precision,
  bearish_candle_ratio_5d_pct double precision,
  bearish_candle_ratio_10d_pct double precision,
  new_ytd_high_last_5d boolean,
  return_5d_pct double precision,
  return_20d_pct double precision,
  return_60d_pct double precision,
  sales_growth_beat_pp double precision,
  operating_profit_growth_beat_pp double precision,
  op_upward_revision_count_3y integer,
  any_earnings_upward_revision_event_count_3y integer,
  psychological_line_5d_pct double precision,
  psychological_line_10d_pct double precision,
  forecast_sales_growth_per_forward_per double precision,
  forecast_eps_growth_yoy_pct double precision,
  forward_peg double precision,
  peg_denominator_small boolean NOT NULL DEFAULT false,
  fiscal_period_changed boolean NOT NULL DEFAULT false,
  growth_rate_not_meaningful boolean NOT NULL DEFAULT false,
  turnaround boolean NOT NULL DEFAULT false,
  loss_expansion boolean NOT NULL DEFAULT false,
  profit_to_loss boolean NOT NULL DEFAULT false,
  forecast_missing boolean NOT NULL DEFAULT false,
  price_missing boolean NOT NULL DEFAULT false,
  insufficient_price_history boolean NOT NULL DEFAULT false,
  metric_reasons jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (batch_id, ticker)
);

CREATE TABLE IF NOT EXISTS public.forecast_revision_events (
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
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ticker, disclosure_id, target_fiscal_year, metric, source)
);

CREATE TABLE IF NOT EXISTS public.screener_current_batch (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  batch_id text NOT NULL REFERENCES public.screener_batches(batch_id),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_screener_metrics_market_sector
  ON public.screener_metrics(batch_id, market_code, sector33_code, sector17_code);
CREATE INDEX IF NOT EXISTS ix_screener_metrics_forward_per
  ON public.screener_metrics(batch_id, forward_per);
CREATE INDEX IF NOT EXISTS ix_screener_metrics_market_cap
  ON public.screener_metrics(batch_id, market_cap);
CREATE INDEX IF NOT EXISTS ix_screener_metrics_composites
  ON public.screener_metrics(batch_id, forecast_sales_growth_per_forward_per, forward_peg);
CREATE INDEX IF NOT EXISTS ix_screener_metrics_revisions
  ON public.screener_metrics(batch_id, op_upward_revision_count_3y, any_earnings_upward_revision_event_count_3y);
CREATE INDEX IF NOT EXISTS ix_forecast_revision_events_ticker_date
  ON public.forecast_revision_events(ticker, disclosed_at DESC);

CREATE OR REPLACE VIEW public.screener_metrics_current
WITH (security_invoker = true)
AS
SELECT m.*
FROM public.screener_metrics m
JOIN public.screener_current_batch c ON c.batch_id = m.batch_id;

CREATE OR REPLACE FUNCTION public.publish_screener_batch(p_batch_id text, p_expected_rows integer)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  actual_rows integer;
BEGIN
  SELECT count(*) INTO actual_rows
  FROM public.screener_metrics
  WHERE batch_id = p_batch_id;

  IF actual_rows <> p_expected_rows THEN
    RAISE EXCEPTION 'screener batch row count mismatch: expected %, actual %', p_expected_rows, actual_rows;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM public.screener_batches
    WHERE batch_id = p_batch_id AND status = 'building'
      AND expected_row_count = p_expected_rows
  ) THEN
    RAISE EXCEPTION 'screener batch is absent, not building, or expected count differs: %', p_batch_id;
  END IF;

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
END;
$$;

ALTER TABLE public.screener_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.screener_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.forecast_revision_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.screener_current_batch ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS screener_batches_authenticated_read ON public.screener_batches;
CREATE POLICY screener_batches_authenticated_read ON public.screener_batches
  FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS screener_metrics_authenticated_read ON public.screener_metrics;
CREATE POLICY screener_metrics_authenticated_read ON public.screener_metrics
  FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS forecast_revision_events_authenticated_read ON public.forecast_revision_events;
CREATE POLICY forecast_revision_events_authenticated_read ON public.forecast_revision_events
  FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS screener_current_batch_authenticated_read ON public.screener_current_batch;
CREATE POLICY screener_current_batch_authenticated_read ON public.screener_current_batch
  FOR SELECT TO authenticated USING (true);

GRANT SELECT ON public.screener_metrics_current TO authenticated;
GRANT SELECT ON public.forecast_revision_events TO authenticated;
REVOKE ALL ON FUNCTION public.publish_screener_batch(text, integer) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_screener_batch(text, integer) TO service_role;
