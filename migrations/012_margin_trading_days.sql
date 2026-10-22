-- Exact TSE trading dates from the J-Quants market calendar.
-- Used to identify weekly final dates and the immediate prior trading day.
-- Unlike individual security balances, these dates are retained indefinitely.
CREATE TABLE IF NOT EXISTS public.margin_trading_day (
  date date PRIMARY KEY,
  fetched_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.margin_trading_day ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.margin_trading_day TO authenticated;
REVOKE ALL ON public.margin_trading_day FROM anon;
CREATE POLICY margin_trading_day_authenticated_read ON public.margin_trading_day
  FOR SELECT TO authenticated USING (true);
