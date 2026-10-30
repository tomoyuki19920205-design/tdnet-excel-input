-- Extend existing Viewer API views without changing their current columns.
-- The verified source rows carry per-cell PDF URL/page in filing_id.
CREATE OR REPLACE VIEW public.api_latest_financials_canonical AS
WITH ranked AS (
    SELECT cf.*,
           row_number() OVER (
               PARTITION BY cf.ticker, cf.period, cf.quarter, cf.metric
               ORDER BY cf.source_priority, cf.recency_key DESC
           ) AS rn
    FROM public.canonical_financials cf
    WHERE cf.value IS NOT NULL
      AND (cf.quarter NOT IN ('FY', '4Q')
           OR cf.period <= left(coalesce(cf.disclosure_datetime, cf.created_at)::text, 10))
      AND cf.unit IN ('millions_jpy', 'million_yen', '百万円', 'JPY', 'yen_per_share')
      AND NOT (cf.quarter IN ('FY', '4Q') AND EXISTS (
          SELECT 1 FROM public.canonical_financials q
          WHERE q.ticker = cf.ticker AND q.period = cf.period
            AND q.quarter = '1Q' AND q.metric = cf.metric AND q.value <> 0
            AND (cf.value = q.value OR cf.value = q.value * 1000000)
      ))
      AND NOT (cf.source = 'tdnet' AND cf.filing_id IS NULL AND cf.disclosure_datetime IS NULL)
      AND cf.source NOT IN (
          'jquants_nxf', 'jquants_forecast_fy', 'jquants_forecast_next_fy',
          'jquants_forecast', 'tdnet_forecast'
      )
), filtered AS (
    SELECT * FROM ranked WHERE rn = 1
)
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
       max(value) FILTER (WHERE metric = 'cost_of_sales') AS cost_of_sales,
       max(value) FILTER (WHERE metric = 'sga') AS sga,
       max(value) FILTER (WHERE metric = 'non_operating_income') AS non_operating_income,
       max(value) FILTER (WHERE metric = 'non_operating_expenses') AS non_operating_expenses,
       max(value) FILTER (WHERE metric = 'extraordinary_income') AS extraordinary_income,
       max(value) FILTER (WHERE metric = 'extraordinary_losses') AS extraordinary_losses,
       max(value) FILTER (WHERE metric = 'income_taxes') AS income_taxes
FROM filtered
GROUP BY ticker, period, quarter;

CREATE OR REPLACE VIEW public.api_latest_segments AS
WITH ranked AS (
    SELECT cs.*,
           row_number() OVER (
               PARTITION BY cs.ticker, cs.period, cs.quarter, cs.segment_key, cs.metric
               ORDER BY cs.source_priority, cs.recency_key DESC,
                        cs.updated_at DESC NULLS LAST, cs.id DESC
           ) AS rn
    FROM public.canonical_segments cs
    WHERE cs.ticker IS NOT NULL AND cs.ticker <> ''
      AND cs.period ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
      AND cs.segment_key <> 'total of reportable segments and others member'
), winners AS (
    SELECT * FROM ranked WHERE rn = 1
), pivoted AS (
    SELECT ticker, period, quarter, segment_name, segment_key,
           max(value) FILTER (WHERE metric = 'sales') AS sales,
           max(value) FILTER (WHERE metric = 'revenue') AS revenue,
           max(value) FILTER (WHERE metric = 'profit') AS profit,
           max(value) FILTER (WHERE metric = 'operating_profit') AS operating_profit,
           max(value) FILTER (WHERE metric = 'segment_profit') AS segment_profit,
           max(value) FILTER (WHERE metric = 'assets') AS assets,
           min(source) AS source,
           min(source_priority) AS source_priority,
           max(value) FILTER (WHERE metric = 'sales_with_internal') AS sales_with_internal
    FROM winners
    GROUP BY ticker, period, quarter, segment_name, segment_key
), period_best AS (
    SELECT ticker, period, quarter, min(source_priority) AS source_priority
    FROM pivoted
    GROUP BY ticker, period, quarter
)
SELECT p.ticker, p.period, p.quarter, p.segment_name, p.segment_key,
       p.sales, p.revenue, p.profit, p.operating_profit, p.segment_profit,
       p.assets, p.source, p.source_priority, p.sales_with_internal
FROM pivoted p
JOIN period_best b USING (ticker, period, quarter)
WHERE p.source_priority = b.source_priority;
