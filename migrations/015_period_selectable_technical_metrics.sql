-- Add the missing period-selectable technical screening metrics.
-- Existing return and period-specific columns remain unchanged for compatibility.

ALTER TABLE public.screener_metrics
  ADD COLUMN IF NOT EXISTS bullish_candle_ratio_1d_pct double precision,
  ADD COLUMN IF NOT EXISTS bearish_candle_ratio_1d_pct double precision,
  ADD COLUMN IF NOT EXISTS psychological_line_1d_pct double precision,
  ADD COLUMN IF NOT EXISTS psychological_line_3d_pct double precision,
  ADD COLUMN IF NOT EXISTS new_ytd_high_last_1d boolean,
  ADD COLUMN IF NOT EXISTS new_ytd_high_last_3d boolean,
  ADD COLUMN IF NOT EXISTS new_ytd_high_last_10d boolean,
  ADD COLUMN IF NOT EXISTS return_1d_pct double precision,
  ADD COLUMN IF NOT EXISTS rise_rate_1d_pct double precision,
  ADD COLUMN IF NOT EXISTS rise_rate_5d_pct double precision,
  ADD COLUMN IF NOT EXISTS rise_rate_20d_pct double precision,
  ADD COLUMN IF NOT EXISTS rise_rate_60d_pct double precision,
  ADD COLUMN IF NOT EXISTS decline_rate_1d_pct double precision,
  ADD COLUMN IF NOT EXISTS decline_rate_5d_pct double precision,
  ADD COLUMN IF NOT EXISTS decline_rate_20d_pct double precision,
  ADD COLUMN IF NOT EXISTS decline_rate_60d_pct double precision;

CREATE OR REPLACE VIEW public.screener_metrics_current
WITH (security_invoker = true)
AS
SELECT m.*
FROM public.screener_metrics m
JOIN public.screener_current_batch c ON c.batch_id = m.batch_id;

GRANT SELECT ON public.screener_metrics_current TO authenticated;
