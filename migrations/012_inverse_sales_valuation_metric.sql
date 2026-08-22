-- Replace forecast_sales_growth_per_forward_per with its inverse metric.
-- Other screener columns and historical batch metadata are unchanged.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'screener_metrics'
      AND column_name = 'forecast_sales_growth_per_forward_per'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'screener_metrics'
      AND column_name = 'forward_per_per_forecast_sales_growth'
  ) THEN
    ALTER TABLE public.screener_metrics
      RENAME COLUMN forecast_sales_growth_per_forward_per
      TO forward_per_per_forecast_sales_growth;
  END IF;
END;
$$;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'screener_metrics_current'
      AND column_name = 'forecast_sales_growth_per_forward_per'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'screener_metrics_current'
      AND column_name = 'forward_per_per_forecast_sales_growth'
  ) THEN
    ALTER VIEW public.screener_metrics_current
      RENAME COLUMN forecast_sales_growth_per_forward_per
      TO forward_per_per_forecast_sales_growth;
  END IF;
END;
$$;

-- A renamed historical value still contains the old ratio and must never be
-- exposed as the inverse metric.  The next atomic snapshot publish fills only
-- values calculated under the new definition.
UPDATE public.screener_metrics
SET forward_per_per_forecast_sales_growth = NULL
WHERE forward_per_per_forecast_sales_growth IS NOT NULL;

-- Historical coverage JSON describes the retired formula.  Remove that key
-- instead of relabelling its old counts as the new inverse definition.
UPDATE public.screener_batches
SET coverage = jsonb_set(
  jsonb_set(
    coverage,
    '{metrics}',
    COALESCE(coverage->'metrics', '{}'::jsonb) - 'forecast_sales_growth_per_forward_per'
  ),
  '{null_reasons}',
  COALESCE(coverage->'null_reasons', '{}'::jsonb) - 'forecast_sales_growth_per_forward_per'
)
WHERE (coverage->'metrics') ? 'forecast_sales_growth_per_forward_per'
   OR (coverage->'null_reasons') ? 'forecast_sales_growth_per_forward_per';
