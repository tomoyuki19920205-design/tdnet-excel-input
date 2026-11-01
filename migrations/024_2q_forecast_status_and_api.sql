-- Additive status and provenance for the compact PL guidance display.
CREATE TABLE IF NOT EXISTS public.company_2q_forecast_status (
    ticker text NOT NULL,
    period text NOT NULL,
    status text NOT NULL CHECK (status IN ('not_disclosed','undecided','withdrawn','unresolved')),
    disclosure_datetime timestamptz,
    source_url text,
    source_sha256 text,
    reason text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, period)
);
GRANT SELECT ON public.company_2q_forecast_status TO anon, authenticated;

-- Verified against the 2026-08-03 official 1Q filing. The issuer explicitly
-- omits an upper-half forecast while managing guidance for the full year.
INSERT INTO public.company_2q_forecast_status
  (ticker,period,status,disclosure_datetime,source_url,reason)
VALUES ('1787','2027-03-31','not_disclosed','2026-08-03T16:30:00+09:00',
  'https://www2.jpx.co.jp/disc/17870/140120260730503690.pdf',
  'Official first-quarter financial statement omits 2Q guidance')
ON CONFLICT (ticker,period) DO UPDATE SET
  status=EXCLUDED.status, disclosure_datetime=EXCLUDED.disclosure_datetime,
  source_url=EXCLUDED.source_url, reason=EXCLUDED.reason, updated_at=now();

CREATE OR REPLACE VIEW public.api_latest_financials_canonical_forecast AS
WITH ranked AS (
  SELECT cf.*,
    row_number() OVER (
      PARTITION BY cf.ticker, cf.period, cf.quarter, cf.metric
      ORDER BY cf.disclosure_datetime DESC NULLS LAST,
        cf.revision_sequence DESC, cf.correction_flag DESC,
        (cf.document_type = 'forecast_revision') DESC NULLS LAST,
        cf.recency_key DESC, cf.source_priority, cf.id DESC
    ) AS rn
  FROM public.canonical_financials cf
  WHERE cf.source = ANY (ARRAY[
    'jquants_nxf','jquants_forecast_fy','jquants_forecast_next_fy',
    'jquants_forecast','tdnet_forecast'
  ])
), filtered AS (SELECT * FROM ranked WHERE rn = 1)
SELECT ticker, period, quarter,
  max(value) FILTER (WHERE metric = 'sales') AS sales,
  max(value) FILTER (WHERE metric = 'gross_profit') AS gross_profit,
  max(value) FILTER (WHERE metric = 'operating_profit') AS operating_profit,
  max(value) FILTER (WHERE metric = 'ordinary_profit') AS ordinary_profit,
  max(value) FILTER (WHERE metric = 'net_income') AS net_income,
  max(value) FILTER (WHERE metric = 'eps') AS eps,
  coalesce(max(source) FILTER (WHERE metric = 'sales'), max(source)) AS source,
  max(updated_at) AS updated_at,
  max(value) FILTER (WHERE metric = 'profit_before_tax') AS profit_before_tax,
  max(disclosure_datetime) AS disclosure_datetime,
  coalesce(max(source_url) FILTER (WHERE metric = 'sales'), max(source_url)) AS source_url,
  coalesce(max(accounting_scope) FILTER (WHERE metric = 'sales'), max(accounting_scope)) AS accounting_scope,
  max(accounting_scope) FILTER (WHERE metric = 'sales') AS sales_accounting_scope,
  max(accounting_scope) FILTER (WHERE metric = 'operating_profit') AS operating_profit_accounting_scope
FROM filtered
GROUP BY ticker, period, quarter;
