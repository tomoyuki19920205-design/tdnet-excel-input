-- Preserve existing ranking and expose disclosed profit basis from existing flags.
CREATE OR REPLACE VIEW public.api_latest_segments AS
 WITH ranked AS (
         SELECT cs.id,
            cs.ticker,
            cs.period,
            cs.quarter,
            cs.segment_name,
            cs.segment_key,
            cs.metric,
            cs.value,
            cs.unit,
            cs.source,
            cs.source_priority,
            cs.filing_id,
            cs.source_row_key,
            cs.disclosure_datetime,
            cs.correction_flag,
            cs.recency_key,
            cs.extracted_at,
            cs.created_at,
            cs.updated_at,
            cs.source_system,
            cs.segment_type,
            cs.derivation_method,
            cs.sales_ytd,
            cs.profit_ytd,
            cs.sales_qtd,
            cs.profit_qtd,
            cs.data_basis,
            cs.source_disclosure_period,
            cs.source_disclosure_date,
            cs.source_doc_id,
            cs.flags,
            row_number() OVER (PARTITION BY cs.ticker, cs.period, cs.quarter, cs.segment_key, cs.metric ORDER BY cs.source_priority, cs.recency_key DESC, cs.updated_at DESC NULLS LAST, cs.id DESC) AS rn
           FROM canonical_segments cs
          WHERE cs.ticker IS NOT NULL AND cs.ticker <> ''::text AND cs.period ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text AND cs.segment_key <> 'total of reportable segments and others member'::text
        ), winners AS (
         SELECT ranked.id,
            ranked.ticker,
            ranked.period,
            ranked.quarter,
            ranked.segment_name,
            ranked.segment_key,
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
            ranked.source_system,
            ranked.segment_type,
            ranked.derivation_method,
            ranked.sales_ytd,
            ranked.profit_ytd,
            ranked.sales_qtd,
            ranked.profit_qtd,
            ranked.data_basis,
            ranked.source_disclosure_period,
            ranked.source_disclosure_date,
            ranked.source_doc_id,
            ranked.flags,
            ranked.rn
           FROM ranked
          WHERE ranked.rn = 1
        ), pivoted AS (
         SELECT winners.ticker,
            winners.period,
            winners.quarter,
            winners.segment_name,
            winners.segment_key,
            max(winners.value) FILTER (WHERE winners.metric = 'sales'::text) AS sales,
            max(winners.value) FILTER (WHERE winners.metric = 'revenue'::text) AS revenue,
            max(winners.value) FILTER (WHERE winners.metric = 'profit'::text) AS profit,
            max(winners.value) FILTER (WHERE winners.metric = 'operating_profit'::text) AS operating_profit,
            max(winners.value) FILTER (WHERE winners.metric = 'segment_profit'::text) AS segment_profit,
            max(winners.value) FILTER (WHERE winners.metric = 'assets'::text) AS assets,
            min(winners.source) AS source,
            min(winners.source_priority) AS source_priority,
            max(winners.value) FILTER (WHERE winners.metric = 'sales_with_internal'::text) AS sales_with_internal,
            max(winners.flags ->> 'profit_basis') FILTER (WHERE winners.metric = 'profit') AS profit_basis,
            max(winners.flags ->> 'period_start') AS period_start,
            max(winners.flags ->> 'period_end') AS period_end
           FROM winners
          GROUP BY winners.ticker, winners.period, winners.quarter, winners.segment_name, winners.segment_key
        ), period_best AS (
         SELECT pivoted.ticker,
            pivoted.period,
            pivoted.quarter,
            min(pivoted.source_priority) AS source_priority
           FROM pivoted
          GROUP BY pivoted.ticker, pivoted.period, pivoted.quarter
        )
 SELECT p.ticker,
    p.period,
    p.quarter,
    p.segment_name,
    p.segment_key,
    p.sales,
    p.revenue,
    p.profit,
    p.operating_profit,
    p.segment_profit,
    p.assets,
    p.source,
    p.source_priority,
    p.sales_with_internal,
    p.profit_basis,
    p.period_start,
    p.period_end,
    (SELECT string_agg(p2.segment_key, '|' ORDER BY p2.segment_key)
       FROM pivoted p2 WHERE p2.ticker=p.ticker AND p2.period=p.period
         AND p2.quarter=p.quarter AND p2.source_priority=p.source_priority) AS segment_definition
   FROM pivoted p
     JOIN period_best b USING (ticker, period, quarter)
  WHERE p.source_priority = b.source_priority;
NOTIFY pgrst, 'reload schema';
