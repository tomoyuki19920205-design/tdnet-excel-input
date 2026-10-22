-- Keep the original exact-date close used by ratios and add provider OHLC for charts.
-- NULL means J-Quants did not publish that field; it must never be inferred.
ALTER TABLE public.margin_index_daily
  ADD COLUMN IF NOT EXISTS open numeric(18,6),
  ADD COLUMN IF NOT EXISTS high numeric(18,6),
  ADD COLUMN IF NOT EXISTS low numeric(18,6);

ALTER TABLE public.margin_index_daily
  ADD CONSTRAINT margin_index_daily_ohlc_positive
  CHECK ((open IS NULL OR open > 0) AND
         (high IS NULL OR high > 0) AND
         (low IS NULL OR low > 0));

COMMENT ON TABLE public.margin_index_daily IS
  'J-Quants non-dividend TSE sector price-index daily OHLC; retained with margin aggregates.';
