-- Add formal three-session bullish/bearish candle ratios.
-- A doji is neither bullish nor bearish, but remains in the fixed denominator of three.

ALTER TABLE public.screener_metrics
  ADD COLUMN IF NOT EXISTS bullish_candle_ratio_3d_pct double precision,
  ADD COLUMN IF NOT EXISTS bearish_candle_ratio_3d_pct double precision;
