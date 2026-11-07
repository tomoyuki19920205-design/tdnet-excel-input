-- Original printed business identity is optional and requires all numeric winners
-- in a period to agree, with no duplicate business key. Existing monetary
-- ranking, grouping and columns are unchanged. A single-use CTE chain permits
-- ticker predicates to reach canonical_segments before window aggregation.
-- Project metadata before sorting so source-evidence JSON is not materialized.
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
            cs.flags->>'profit_basis' AS metadata_profit_basis,
            cs.flags->>'period_start' AS metadata_period_start,
            cs.flags->>'period_end' AS metadata_period_end,
            cs.flags->>'profit_label' AS metadata_profit_label,
            cs.flags->>'sales_basis' AS metadata_sales_basis,
            cs.flags->>'sales_label' AS metadata_sales_label,
            cs.flags->>'is_consolidated' AS metadata_is_consolidated,
            cs.flags->>'currency' AS metadata_currency,
            cs.flags->>'business_key' AS metadata_business_key,
            cs.flags->>'business_key_method' AS metadata_business_key_method,
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
            ranked.metadata_profit_basis,
            ranked.metadata_period_start,
            ranked.metadata_period_end,
            ranked.metadata_profit_label,
            ranked.metadata_sales_basis,
            ranked.metadata_sales_label,
            ranked.metadata_is_consolidated,
            ranked.metadata_currency,
            ranked.metadata_business_key,
            ranked.metadata_business_key_method,
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
            max(winners.metadata_profit_basis) FILTER (WHERE winners.metric = 'profit') AS profit_basis,
            max(winners.metadata_period_start) AS period_start,
            max(winners.metadata_period_end) AS period_end,
            max(winners.metadata_profit_label) FILTER (WHERE winners.metric='profit') AS profit_label,
            coalesce(max(winners.metadata_sales_basis) FILTER (WHERE winners.metric='sales'), max(winners.metadata_sales_basis)) AS sales_basis,
            coalesce(max(winners.metadata_sales_label) FILTER (WHERE winners.metric='sales'), max(winners.metadata_sales_label)) AS sales_label,
            max(winners.metadata_is_consolidated) AS accounting_scope,
            max(winners.metadata_currency) AS currency,
            CASE WHEN count(*) FILTER (WHERE winners.metric IN ('sales','profit') AND winners.value IS NOT NULL) > 0
                 AND count(*) FILTER (WHERE winners.metric IN ('sales','profit') AND winners.value IS NOT NULL)
                     = count(*) FILTER (WHERE winners.metric IN ('sales','profit') AND winners.value IS NOT NULL
                         AND winners.metadata_business_key_method='original_business_header_v1'
                         AND nullif(winners.metadata_business_key,'') IS NOT NULL)
                 AND count(DISTINCT winners.metadata_business_key) FILTER (WHERE winners.metric IN ('sales','profit') AND winners.value IS NOT NULL)=1
                 THEN max(winners.metadata_business_key) FILTER (WHERE winners.metric IN ('sales','profit') AND winners.value IS NOT NULL)
                 ELSE NULL END AS business_key
           FROM winners
          GROUP BY winners.ticker, winners.period, winners.quarter, winners.segment_name, winners.segment_key
        ), period_ranked AS (
          SELECT pivoted.*, min(source_priority) OVER (PARTITION BY ticker,period,quarter) AS best_priority
          FROM pivoted
        ), selected AS (
          SELECT * FROM period_ranked WHERE source_priority=best_priority
        ), counted AS (
          SELECT selected.*, count(*) OVER (PARTITION BY ticker,period,quarter,business_key) AS business_key_count
          FROM selected
        ), compared AS (
          SELECT counted.*,
            CASE WHEN bool_and(business_key IS NOT NULL) OVER (PARTITION BY ticker,period,quarter)
                      AND max(business_key_count) OVER (PARTITION BY ticker,period,quarter)=1
                 THEN jsonb_agg(business_key) OVER (PARTITION BY ticker,period,quarter ORDER BY business_key
                      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)::text
                 ELSE NULL END AS comparison_definition
          FROM counted
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
    string_agg(p.segment_key, '|') OVER (
       PARTITION BY p.ticker,p.period,p.quarter,p.source_priority ORDER BY p.segment_key
       ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS segment_definition,
    p.profit_label, p.sales_basis, p.sales_label,
    CASE p.accounting_scope WHEN 'true' THEN 'consolidated' WHEN 'false' THEN 'non_consolidated' ELSE NULL END AS accounting_scope,
    p.currency,
    CASE WHEN p.comparison_definition IS NOT NULL THEN p.business_key ELSE NULL END AS business_key,
    p.comparison_definition
   FROM compared p;
NOTIFY pgrst, 'reload schema';
