-- Add nullable provenance for source-verified cumulative upper-half forecasts.
-- Existing PL rows and views remain compatible.
BEGIN;

ALTER TABLE public.canonical_financials
    ADD COLUMN IF NOT EXISTS accounting_scope text,
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS source_sha256 text;

CREATE INDEX IF NOT EXISTS ix_cf_2q_scope_latest
    ON public.canonical_financials (ticker, period, accounting_scope, disclosure_datetime DESC)
    WHERE quarter = '2Q' AND source = 'tdnet_forecast';

COMMIT;
