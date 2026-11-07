-- Same filing and source-row key: omitted metadata from a legacy sync must not
-- erase independently verified definitions. An explicit JSON null still clears a key.
CREATE OR REPLACE FUNCTION public.preserve_segment_metadata() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_row_key=OLD.source_row_key AND NEW.filing_id IS NOT DISTINCT FROM OLD.filing_id THEN
        IF NEW.flags->>'profit_basis'='company_defined_unknown'
           AND NEW.flags->>'profit_tag' IS NULL AND NEW.value IS NOT DISTINCT FROM OLD.value
           AND OLD.flags->>'profit_basis' IS NOT NULL THEN
            NEW.flags = NEW.flags - 'profit_basis';
        END IF;
        NEW.flags = coalesce(OLD.flags,'{}'::jsonb) || coalesce(NEW.flags,'{}'::jsonb);
    END IF;
    RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS preserve_segment_metadata_before_update ON public.canonical_segments;
CREATE TRIGGER preserve_segment_metadata_before_update BEFORE UPDATE ON public.canonical_segments
FOR EACH ROW EXECUTE FUNCTION public.preserve_segment_metadata();

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
            max(winners.flags ->> 'period_end') AS period_end,
            max(winners.flags ->> 'profit_label') FILTER (WHERE winners.metric='profit') AS profit_label,
            coalesce(max(winners.flags ->> 'sales_basis') FILTER (WHERE winners.metric='sales'), max(winners.flags ->> 'sales_basis')) AS sales_basis,
            coalesce(max(winners.flags ->> 'sales_label') FILTER (WHERE winners.metric='sales'), max(winners.flags ->> 'sales_label')) AS sales_label,
            max(winners.flags ->> 'is_consolidated') AS accounting_scope,
            max(winners.flags ->> 'currency') AS currency
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
    string_agg(p.segment_key, '|') OVER (
       PARTITION BY p.ticker,p.period,p.quarter,p.source_priority ORDER BY p.segment_key
       ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS segment_definition,
    p.profit_label, p.sales_basis, p.sales_label,
    CASE p.accounting_scope WHEN 'true' THEN 'consolidated' WHEN 'false' THEN 'non_consolidated' ELSE NULL END AS accounting_scope,
    p.currency
   FROM pivoted p
     JOIN period_best b USING (ticker, period, quarter)
  WHERE p.source_priority = b.source_priority;
NOTIFY pgrst, 'reload schema';
