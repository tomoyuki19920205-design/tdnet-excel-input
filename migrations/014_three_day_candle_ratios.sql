-- Add formal three-session bullish/bearish candle ratios.
-- A doji is neither bullish nor bearish, but remains in the fixed denominator of three.

ALTER TABLE public.screener_metrics
  ADD COLUMN IF NOT EXISTS bullish_candle_ratio_3d_pct double precision,
  ADD COLUMN IF NOT EXISTS bearish_candle_ratio_3d_pct double precision;

-- PostgreSQL expands m.* when a view is created, so recreate the view to expose
-- newly appended table columns through the authenticated screening API.
CREATE OR REPLACE VIEW public.screener_metrics_current
WITH (security_invoker = true)
AS
SELECT m.*
FROM public.screener_metrics m
JOIN public.screener_current_batch c ON c.batch_id = m.batch_id;

GRANT SELECT ON public.screener_metrics_current TO authenticated;
