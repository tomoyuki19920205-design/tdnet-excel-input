-- J-Quants V2 TSE sector price-index closes, keyed by balance date and index code.
-- This table is intentionally outside individual-security retention procedures.
CREATE TABLE IF NOT EXISTS public.margin_index_daily (
  date date NOT NULL,
  code text NOT NULL CHECK (code ~ '^(004[0-9A-F]|005[0-9A-F]|0060)$'),
  close numeric(18,6) NOT NULL CHECK (close > 0),
  fetched_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (date, code)
);

ALTER TABLE public.margin_index_daily ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.margin_index_daily TO authenticated;
REVOKE ALL ON public.margin_index_daily FROM anon;
CREATE POLICY margin_index_daily_authenticated_read ON public.margin_index_daily
  FOR SELECT TO authenticated USING (true);
