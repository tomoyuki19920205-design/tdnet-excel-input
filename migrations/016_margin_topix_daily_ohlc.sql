-- TOPIX daily candles share the retained date/code index store with sector candles.
ALTER TABLE public.margin_index_daily
  DROP CONSTRAINT IF EXISTS margin_index_daily_code_check;
ALTER TABLE public.margin_index_daily
  ADD CONSTRAINT margin_index_daily_code_check
  CHECK (code = 'TOPIX' OR code ~ '^(004[0-9A-F]|005[0-9A-F]|0060)$');

COMMENT ON TABLE public.margin_index_daily IS
  'J-Quants non-dividend TOPIX and TSE sector price-index daily OHLC; retained with margin aggregates.';
