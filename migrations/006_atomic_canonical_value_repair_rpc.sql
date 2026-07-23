-- Atomic exact-predicate repair of a bounded segment_canonical value set.
-- Only sales and profit may change; all identity and provenance fields are
-- immutable guards supplied by the audited repair plan.
CREATE OR REPLACE FUNCTION public.repair_segment_canonical_values_exact(p_repairs jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_item jsonb;
    v_requested integer;
    v_locked integer := 0;
    v_matched integer := 0;
    v_updated integer := 0;
    v_row public.segment_canonical%ROWTYPE;
    v_result_rows jsonb := '[]'::jsonb;
BEGIN
    IF p_repairs IS NULL OR jsonb_typeof(p_repairs) <> 'array' THEN
        RAISE EXCEPTION 'repair payload must be a JSON array' USING ERRCODE = '22023';
    END IF;
    v_requested := jsonb_array_length(p_repairs);
    IF v_requested = 0 OR v_requested > 200 THEN
        RAISE EXCEPTION 'repair payload count must be between 1 and 200' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_repairs) AS item
        WHERE jsonb_typeof(item) <> 'object'
           OR NOT item ?& ARRAY[
               'segment_id','existing_sales','replacement_sales','existing_profit','replacement_profit',
               'ticker','period','quarter','segment_name','unit','segment_key',
               'source','existing_updated_at','repair_evidence','run_id'
           ]
           OR item ?| ARRAY['requested_id','requested_disclosure_no']
           OR NULLIF(btrim(item->>'ticker'), '') IS NULL
           OR NULLIF(btrim(item->>'period'), '') IS NULL
           OR NULLIF(btrim(item->>'quarter'), '') IS NULL
           OR NULLIF(btrim(item->>'segment_name'), '') IS NULL
           OR NULLIF(btrim(item->>'unit'), '') IS NULL
           OR NULLIF(btrim(item->>'source'), '') IS NULL
           OR NULLIF(btrim(item->>'existing_updated_at'), '') IS NULL
           OR NULLIF(btrim(item->>'repair_evidence'), '') IS NULL
           OR NULLIF(btrim(item->>'run_id'), '') IS NULL
           OR (item->>'unit') <> 'millions_jpy'
           OR (
               (item->>'existing_sales')::bigint IS NOT DISTINCT FROM (item->>'replacement_sales')::bigint
               AND (item->>'existing_profit')::bigint IS NOT DISTINCT FROM (item->>'replacement_profit')::bigint
           )
    ) THEN
        RAISE EXCEPTION 'repair payload validation failed' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_repairs) AS item,
             LATERAL jsonb_object_keys(item) AS key
        WHERE key NOT IN (
            'segment_id','existing_sales','replacement_sales','existing_profit','replacement_profit',
            'ticker','period','quarter','segment_name','unit','segment_key',
            'source','existing_updated_at','repair_evidence','run_id'
        )
    ) THEN
        RAISE EXCEPTION 'repair payload contains an unsupported field' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_repairs) AS item
        GROUP BY (item->>'segment_id')::bigint HAVING count(*) <> 1
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_repairs) AS item
        GROUP BY item->>'ticker', (item->>'period')::date, item->>'quarter', item->>'segment_name'
        HAVING count(*) <> 1
    ) THEN
        RAISE EXCEPTION 'repair payload contains a duplicate identity' USING ERRCODE = '22023';
    END IF;

    FOR v_item IN
        SELECT item FROM jsonb_array_elements(p_repairs) AS item
        ORDER BY (item->>'segment_id')::bigint
    LOOP
        SELECT sc.* INTO v_row
        FROM public.segment_canonical AS sc
        WHERE sc.ticker = v_item->>'ticker'
          AND sc.period = (v_item->>'period')::date
          AND sc.quarter = v_item->>'quarter'
          AND sc.segment_name = v_item->>'segment_name'
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'repair target missing for segment_id %', v_item->>'segment_id' USING ERRCODE = 'P0002';
        END IF;
        v_locked := v_locked + 1;
        IF v_row.sales IS DISTINCT FROM (v_item->>'existing_sales')::bigint
           OR v_row.profit IS DISTINCT FROM (v_item->>'existing_profit')::bigint
           OR v_row.segment_key IS DISTINCT FROM v_item->>'segment_key'
           OR v_row.source IS DISTINCT FROM v_item->>'source'
           OR v_row.updated_at IS DISTINCT FROM (v_item->>'existing_updated_at')::timestamptz THEN
            RAISE EXCEPTION 'exact before predicate mismatch for segment_id %', v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;
        v_matched := v_matched + 1;
    END LOOP;

    FOR v_item IN
        SELECT item FROM jsonb_array_elements(p_repairs) AS item
        ORDER BY (item->>'segment_id')::bigint
    LOOP
        UPDATE public.segment_canonical AS sc
        SET sales = (v_item->>'replacement_sales')::bigint,
            profit = (v_item->>'replacement_profit')::bigint
        WHERE sc.ticker = v_item->>'ticker'
          AND sc.period = (v_item->>'period')::date
          AND sc.quarter = v_item->>'quarter'
          AND sc.segment_name = v_item->>'segment_name'
          AND sc.sales IS NOT DISTINCT FROM (v_item->>'existing_sales')::bigint
          AND sc.profit IS NOT DISTINCT FROM (v_item->>'existing_profit')::bigint
          AND sc.segment_key IS NOT DISTINCT FROM v_item->>'segment_key'
          AND sc.source IS NOT DISTINCT FROM v_item->>'source'
          AND sc.updated_at IS NOT DISTINCT FROM (v_item->>'existing_updated_at')::timestamptz;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'guarded update failed for segment_id %', v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;
        v_updated := v_updated + 1;
        SELECT sc.* INTO v_row
        FROM public.segment_canonical AS sc
        WHERE sc.ticker = v_item->>'ticker'
          AND sc.period = (v_item->>'period')::date
          AND sc.quarter = v_item->>'quarter'
          AND sc.segment_name = v_item->>'segment_name';
        IF v_row.sales IS DISTINCT FROM (v_item->>'replacement_sales')::bigint
           OR v_row.profit IS DISTINCT FROM (v_item->>'replacement_profit')::bigint
           OR v_row.segment_key IS DISTINCT FROM v_item->>'segment_key'
           OR v_row.source IS DISTINCT FROM v_item->>'source'
           OR v_row.updated_at IS DISTINCT FROM (v_item->>'existing_updated_at')::timestamptz THEN
            RAISE EXCEPTION 'post-update verification failed for segment_id %', v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;
        v_result_rows := v_result_rows || jsonb_build_array(jsonb_build_object(
            'segment_id', (v_item->>'segment_id')::bigint,
            'before_sales', (v_item->>'existing_sales')::bigint,
            'after_sales', v_row.sales,
            'before_profit', (v_item->>'existing_profit')::bigint,
            'after_profit', v_row.profit,
            'ticker', v_row.ticker, 'period', v_row.period, 'quarter', v_row.quarter,
            'segment_name', v_row.segment_name, 'run_id', v_item->>'run_id'
        ));
    END LOOP;
    IF v_locked <> v_requested OR v_matched <> v_requested OR v_updated <> v_requested THEN
        RAISE EXCEPTION 'repair count mismatch' USING ERRCODE = 'P0001';
    END IF;
    RETURN jsonb_build_object(
        'requested_count', v_requested, 'locked_count', v_locked,
        'matched_count', v_matched, 'updated_count', v_updated,
        'rows', v_result_rows, 'error_count', 0
    );
END;
$function$;

REVOKE ALL ON FUNCTION public.repair_segment_canonical_values_exact(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.repair_segment_canonical_values_exact(jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.repair_segment_canonical_values_exact(jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.repair_segment_canonical_values_exact(jsonb) TO service_role;
