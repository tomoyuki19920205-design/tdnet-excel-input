-- Preserve provenance and separate forecast revisions from actual observations.
ALTER TABLE canonical_financials ADD COLUMN IF NOT EXISTS revision_sequence integer NOT NULL DEFAULT 0;
ALTER TABLE canonical_financials ADD COLUMN IF NOT EXISTS document_type text;
ALTER TABLE canonical_financials ADD COLUMN IF NOT EXISTS period_start date;
ALTER TABLE canonical_financials ADD COLUMN IF NOT EXISTS period_end date;

CREATE OR REPLACE VIEW api_latest_financials_canonical AS
 WITH ranked AS (
         SELECT cf.id,
            cf.ticker,
            cf.period,
            cf.quarter,
            cf.metric,
            cf.value,
            cf.unit,
            cf.source,
            cf.source_priority,
            cf.filing_id,
            cf.source_row_key,
            cf.disclosure_datetime,
            cf.correction_flag,
            cf.recency_key,
            cf.extracted_at,
            cf.created_at,
            cf.updated_at,
            row_number() OVER (PARTITION BY cf.ticker, cf.period, cf.quarter, cf.metric ORDER BY cf.source_priority, cf.recency_key DESC) AS rn
           FROM canonical_financials cf
          WHERE cf.value IS NOT NULL
            AND (cf.quarter NOT IN ('FY','4Q') OR
                 cf.period <= left(coalesce(cf.disclosure_datetime, cf.created_at)::text,10))
            AND cf.unit IN ('millions_jpy','million_yen','百万円','JPY','yen_per_share')
            AND NOT (cf.quarter IN ('FY','4Q') AND EXISTS (
              SELECT 1 FROM canonical_financials q
              WHERE q.ticker=cf.ticker AND q.period=cf.period AND q.quarter='1Q'
                AND q.metric=cf.metric AND q.value<>0
                AND (cf.value=q.value OR cf.value=q.value*1000000)
            ))
            AND ((NOT ((cf.source = 'tdnet'::text) AND (cf.filing_id IS NULL) AND (cf.disclosure_datetime IS NULL))) AND (cf.source <> ALL (ARRAY['jquants_nxf'::text, 'jquants_forecast_fy'::text, 'jquants_forecast_next_fy'::text, 'jquants_forecast'::text, 'tdnet_forecast'::text])))
        ), filtered AS (
         SELECT ranked.id,
            ranked.ticker,
            ranked.period,
            ranked.quarter,
            ranked.metric,
            ranked.value,
            ranked.unit,
            ranked.source,
            ranked.source_priority,
            ranked.filing_id,
            ranked.source_row_key,
            ranked.disclosure_datetime,
            ranked.correction_flag,
            ranked.recency_key,
            ranked.extracted_at,
            ranked.created_at,
            ranked.updated_at,
            ranked.rn
           FROM ranked
          WHERE (ranked.rn = 1)
        )
 SELECT ticker,
    period,
    quarter,
    max(value) FILTER (WHERE (metric = 'sales'::text)) AS sales,
    max(value) FILTER (WHERE (metric = 'gross_profit'::text)) AS gross_profit,
    max(value) FILTER (WHERE (metric = 'operating_profit'::text)) AS operating_profit,
    max(value) FILTER (WHERE (metric = 'ordinary_profit'::text)) AS ordinary_profit,
    max(value) FILTER (WHERE (metric = 'net_income'::text)) AS net_income,
    max(value) FILTER (WHERE (metric = 'eps'::text)) AS eps,
    COALESCE(max(source) FILTER (WHERE (metric = 'sales'::text)), max(source)) AS source,
    max(updated_at) AS updated_at,
    max(value) FILTER (WHERE (metric = 'profit_before_tax'::text)) AS profit_before_tax
   FROM filtered
  GROUP BY ticker, period, quarter;

CREATE OR REPLACE VIEW api_latest_financials_canonical_forecast AS
 WITH ranked AS (
         SELECT cf.id,
            cf.ticker,
            cf.period,
            cf.quarter,
            cf.metric,
            cf.value,
            cf.unit,
            cf.source,
            cf.source_priority,
            cf.filing_id,
            cf.source_row_key,
            cf.disclosure_datetime,
            cf.correction_flag,
            cf.recency_key,
            cf.extracted_at,
            cf.created_at,
            cf.updated_at,
            row_number() OVER (PARTITION BY cf.ticker, cf.period, cf.quarter, cf.metric ORDER BY cf.disclosure_datetime DESC NULLS LAST, cf.revision_sequence DESC, cf.correction_flag DESC, (cf.document_type = 'forecast_revision') DESC NULLS LAST, cf.recency_key DESC, cf.source_priority, cf.id DESC) AS rn
           FROM canonical_financials cf
          WHERE (cf.source = ANY (ARRAY['jquants_nxf'::text, 'jquants_forecast_fy'::text, 'jquants_forecast_next_fy'::text, 'jquants_forecast'::text, 'tdnet_forecast'::text]))
        ), filtered AS (
         SELECT ranked.id,
            ranked.ticker,
            ranked.period,
            ranked.quarter,
            ranked.metric,
            ranked.value,
            ranked.unit,
            ranked.source,
            ranked.source_priority,
            ranked.filing_id,
            ranked.source_row_key,
            ranked.disclosure_datetime,
            ranked.correction_flag,
            ranked.recency_key,
            ranked.extracted_at,
            ranked.created_at,
            ranked.updated_at,
            ranked.rn
           FROM ranked
          WHERE (ranked.rn = 1)
        )
 SELECT ticker,
    period,
    quarter,
    max(value) FILTER (WHERE (metric = 'sales'::text)) AS sales,
    max(value) FILTER (WHERE (metric = 'gross_profit'::text)) AS gross_profit,
    max(value) FILTER (WHERE (metric = 'operating_profit'::text)) AS operating_profit,
    max(value) FILTER (WHERE (metric = 'ordinary_profit'::text)) AS ordinary_profit,
    max(value) FILTER (WHERE (metric = 'net_income'::text)) AS net_income,
    max(value) FILTER (WHERE (metric = 'eps'::text)) AS eps,
    COALESCE(max(source) FILTER (WHERE (metric = 'sales'::text)), max(source)) AS source,
    max(updated_at) AS updated_at,
    max(value) FILTER (WHERE (metric = 'profit_before_tax'::text)) AS profit_before_tax
   FROM filtered
  GROUP BY ticker, period, quarter;
