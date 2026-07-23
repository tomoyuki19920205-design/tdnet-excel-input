-- Replace the exact misbound-row cleanup RPC with a NULL-safe segment_key
-- contract.  JSON null is a valid expected segment_key, while an empty string
-- remains invalid.  All identity, business-value, provenance, locking,
-- atomicity, privilege, and count contracts from migration 007 are retained.
CREATE OR REPLACE FUNCTION public.delete_segment_canonical_misbound_exact(p_deletes jsonb)
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
    v_deleted integer := 0;
    v_misbound public.segment_canonical%ROWTYPE;
    v_destination public.segment_canonical%ROWTYPE;
    v_result_rows jsonb := '[]'::jsonb;
BEGIN
    IF p_deletes IS NULL OR jsonb_typeof(p_deletes) <> 'array' THEN
        RAISE EXCEPTION 'delete payload must be a JSON array' USING ERRCODE = '22023';
    END IF;
    v_requested := jsonb_array_length(p_deletes);
    IF v_requested = 0 OR v_requested > 50 THEN
        RAISE EXCEPTION 'delete payload count must be between 1 and 50' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_deletes) AS item
        WHERE jsonb_typeof(item) <> 'object'
           OR NOT item ?& ARRAY[
               'segment_id','misbound_ticker','misbound_period','misbound_quarter',
               'misbound_segment_name','misbound_sales','misbound_profit',
               'misbound_segment_key','misbound_source','misbound_updated_at',
               'destination_ticker','destination_period','destination_quarter',
               'destination_segment_name','destination_sales','destination_profit',
               'destination_segment_key','destination_source','destination_updated_at',
               'unit','evidence_digest','run_id'
           ]
           OR item ?| ARRAY['requested_id','requested_disclosure_no','filing_id']
           OR NULLIF(btrim(item->>'segment_id'), '') IS NULL
           OR NULLIF(btrim(item->>'misbound_ticker'), '') IS NULL
           OR NULLIF(btrim(item->>'misbound_period'), '') IS NULL
           OR NULLIF(btrim(item->>'misbound_quarter'), '') IS NULL
           OR NULLIF(btrim(item->>'misbound_segment_name'), '') IS NULL
           OR jsonb_typeof(item->'misbound_segment_key') NOT IN ('string', 'null')
           OR (
               jsonb_typeof(item->'misbound_segment_key') = 'string'
               AND btrim(item->>'misbound_segment_key') = ''
           )
           OR NULLIF(btrim(item->>'misbound_source'), '') IS NULL
           OR NULLIF(btrim(item->>'misbound_updated_at'), '') IS NULL
           OR NULLIF(btrim(item->>'destination_ticker'), '') IS NULL
           OR NULLIF(btrim(item->>'destination_period'), '') IS NULL
           OR NULLIF(btrim(item->>'destination_quarter'), '') IS NULL
           OR NULLIF(btrim(item->>'destination_segment_name'), '') IS NULL
           OR jsonb_typeof(item->'destination_segment_key') NOT IN ('string', 'null')
           OR (
               jsonb_typeof(item->'destination_segment_key') = 'string'
               AND btrim(item->>'destination_segment_key') = ''
           )
           OR NULLIF(btrim(item->>'destination_source'), '') IS NULL
           OR NULLIF(btrim(item->>'destination_updated_at'), '') IS NULL
           OR NULLIF(btrim(item->>'unit'), '') IS NULL
           OR NULLIF(btrim(item->>'evidence_digest'), '') IS NULL
           OR NULLIF(btrim(item->>'run_id'), '') IS NULL
           OR item->>'unit' <> 'millions_jpy'
           OR (
               item->>'misbound_ticker',
               (item->>'misbound_period')::date,
               item->>'misbound_quarter',
               item->>'misbound_segment_name'
           ) = (
               item->>'destination_ticker',
               (item->>'destination_period')::date,
               item->>'destination_quarter',
               item->>'destination_segment_name'
           )
    ) THEN
        RAISE EXCEPTION 'delete payload validation failed' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_deletes) AS item,
             LATERAL jsonb_object_keys(item) AS key
        WHERE key NOT IN (
            'segment_id','misbound_ticker','misbound_period','misbound_quarter',
            'misbound_segment_name','misbound_sales','misbound_profit',
            'misbound_segment_key','misbound_source','misbound_updated_at',
            'destination_ticker','destination_period','destination_quarter',
            'destination_segment_name','destination_sales','destination_profit',
            'destination_segment_key','destination_source','destination_updated_at',
            'unit','evidence_digest','run_id'
        )
    ) THEN
        RAISE EXCEPTION 'delete payload contains an unsupported field' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_deletes) AS item
        GROUP BY (item->>'segment_id')::bigint HAVING count(*) <> 1
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_deletes) AS item
        GROUP BY item->>'misbound_ticker', (item->>'misbound_period')::date,
                 item->>'misbound_quarter', item->>'misbound_segment_name'
        HAVING count(*) <> 1
    ) THEN
        RAISE EXCEPTION 'delete payload contains a duplicate identity' USING ERRCODE = '22023';
    END IF;

    FOR v_item IN
        SELECT item FROM jsonb_array_elements(p_deletes) AS item
        ORDER BY (item->>'segment_id')::bigint
    LOOP
        SELECT sc.* INTO v_misbound
        FROM public.segment_canonical AS sc
        WHERE sc.ticker = v_item->>'misbound_ticker'
          AND sc.period = (v_item->>'misbound_period')::date
          AND sc.quarter = v_item->>'misbound_quarter'
          AND sc.segment_name = v_item->>'misbound_segment_name'
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'misbound target missing for segment_id %',
                v_item->>'segment_id' USING ERRCODE = 'P0002';
        END IF;
        IF v_misbound.sales IS DISTINCT FROM (v_item->>'misbound_sales')::bigint
           OR v_misbound.profit IS DISTINCT FROM (v_item->>'misbound_profit')::bigint
           OR v_misbound.segment_key IS DISTINCT FROM v_item->>'misbound_segment_key'
           OR v_misbound.source IS DISTINCT FROM v_item->>'misbound_source'
           OR v_misbound.updated_at IS DISTINCT FROM
                (v_item->>'misbound_updated_at')::timestamptz THEN
            RAISE EXCEPTION 'exact misbound predicate mismatch for segment_id %',
                v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;

        SELECT sc.* INTO v_destination
        FROM public.segment_canonical AS sc
        WHERE sc.ticker = v_item->>'destination_ticker'
          AND sc.period = (v_item->>'destination_period')::date
          AND sc.quarter = v_item->>'destination_quarter'
          AND sc.segment_name = v_item->>'destination_segment_name'
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'canonical destination missing for segment_id %',
                v_item->>'segment_id' USING ERRCODE = 'P0002';
        END IF;
        IF v_destination.sales IS DISTINCT FROM
                (v_item->>'destination_sales')::bigint
           OR v_destination.profit IS DISTINCT FROM
                (v_item->>'destination_profit')::bigint
           OR v_destination.segment_key IS DISTINCT FROM
                v_item->>'destination_segment_key'
           OR v_destination.source IS DISTINCT FROM
                v_item->>'destination_source'
           OR v_destination.updated_at IS DISTINCT FROM
                (v_item->>'destination_updated_at')::timestamptz THEN
            RAISE EXCEPTION 'exact destination predicate mismatch for segment_id %',
                v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;
        v_locked := v_locked + 2;
        v_matched := v_matched + 1;
    END LOOP;

    FOR v_item IN
        SELECT item FROM jsonb_array_elements(p_deletes) AS item
        ORDER BY (item->>'segment_id')::bigint
    LOOP
        DELETE FROM public.segment_canonical AS sc
        WHERE sc.ticker = v_item->>'misbound_ticker'
          AND sc.period = (v_item->>'misbound_period')::date
          AND sc.quarter = v_item->>'misbound_quarter'
          AND sc.segment_name = v_item->>'misbound_segment_name'
          AND sc.sales IS NOT DISTINCT FROM (v_item->>'misbound_sales')::bigint
          AND sc.profit IS NOT DISTINCT FROM (v_item->>'misbound_profit')::bigint
          AND sc.segment_key IS NOT DISTINCT FROM v_item->>'misbound_segment_key'
          AND sc.source IS NOT DISTINCT FROM v_item->>'misbound_source'
          AND sc.updated_at IS NOT DISTINCT FROM
                (v_item->>'misbound_updated_at')::timestamptz;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'guarded delete failed for segment_id %',
                v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;
        v_deleted := v_deleted + 1;

        IF EXISTS (
            SELECT 1 FROM public.segment_canonical AS sc
            WHERE sc.ticker = v_item->>'misbound_ticker'
              AND sc.period = (v_item->>'misbound_period')::date
              AND sc.quarter = v_item->>'misbound_quarter'
              AND sc.segment_name = v_item->>'misbound_segment_name'
        ) THEN
            RAISE EXCEPTION 'misbound row remained after delete for segment_id %',
                v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;
        SELECT sc.* INTO v_destination
        FROM public.segment_canonical AS sc
        WHERE sc.ticker = v_item->>'destination_ticker'
          AND sc.period = (v_item->>'destination_period')::date
          AND sc.quarter = v_item->>'destination_quarter'
          AND sc.segment_name = v_item->>'destination_segment_name';
        IF NOT FOUND
           OR v_destination.sales IS DISTINCT FROM
                (v_item->>'destination_sales')::bigint
           OR v_destination.profit IS DISTINCT FROM
                (v_item->>'destination_profit')::bigint
           OR v_destination.segment_key IS DISTINCT FROM
                v_item->>'destination_segment_key'
           OR v_destination.source IS DISTINCT FROM
                v_item->>'destination_source'
           OR v_destination.updated_at IS DISTINCT FROM
                (v_item->>'destination_updated_at')::timestamptz THEN
            RAISE EXCEPTION 'destination changed during delete for segment_id %',
                v_item->>'segment_id' USING ERRCODE = 'P0001';
        END IF;
        v_result_rows := v_result_rows || jsonb_build_array(jsonb_build_object(
            'segment_id', (v_item->>'segment_id')::bigint,
            'misbound_ticker', v_item->>'misbound_ticker',
            'misbound_period', (v_item->>'misbound_period')::date,
            'misbound_quarter', v_item->>'misbound_quarter',
            'misbound_segment_name', v_item->>'misbound_segment_name',
            'destination_ticker', v_destination.ticker,
            'destination_period', v_destination.period,
            'destination_quarter', v_destination.quarter,
            'destination_segment_name', v_destination.segment_name,
            'run_id', v_item->>'run_id'
        ));
    END LOOP;
    IF v_locked <> v_requested * 2
       OR v_matched <> v_requested
       OR v_deleted <> v_requested THEN
        RAISE EXCEPTION 'delete count mismatch' USING ERRCODE = 'P0001';
    END IF;
    RETURN jsonb_build_object(
        'requested_count', v_requested,
        'locked_count', v_locked,
        'matched_count', v_matched,
        'deleted_count', v_deleted,
        'rows', v_result_rows,
        'error_count', 0
    );
END;
$function$;

REVOKE ALL ON FUNCTION public.delete_segment_canonical_misbound_exact(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.delete_segment_canonical_misbound_exact(jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.delete_segment_canonical_misbound_exact(jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.delete_segment_canonical_misbound_exact(jsonb) TO service_role;
