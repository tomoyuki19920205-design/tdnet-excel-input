-- Atomically repair a bounded set of canonical_segments EAV values.
-- Every target is identified by its immutable row identity and provenance.
-- The only business column this function may update is value.
CREATE OR REPLACE FUNCTION public.repair_canonical_segment_eav_values_exact(
    p_repairs jsonb
)
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
    v_row public.canonical_segments%ROWTYPE;
    v_result_rows jsonb := '[]'::jsonb;
BEGIN
    IF p_repairs IS NULL OR jsonb_typeof(p_repairs) <> 'array' THEN
        RAISE EXCEPTION 'repair payload must be a JSON array'
            USING ERRCODE = '22023';
    END IF;

    v_requested := jsonb_array_length(p_repairs);
    IF v_requested = 0 OR v_requested > 50 THEN
        RAISE EXCEPTION 'repair payload count must be between 1 and 50'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_repairs) AS item
        WHERE jsonb_typeof(item) <> 'object'
           OR NOT item ?& ARRAY[
               'canonical_segments_id', 'segment_id', 'source_row_key',
               'metric', 'old_value', 'new_value', 'unit', 'filing_id',
               'ticker', 'period', 'quarter', 'segment_name', 'segment_key',
               'source', 'existing_updated_at', 'evidence_digest', 'run_id'
           ]
           OR item ?| ARRAY['requested_id', 'requested_disclosure_no']
           OR NULLIF(btrim(item->>'canonical_segments_id'), '') IS NULL
           OR NULLIF(btrim(item->>'segment_id'), '') IS NULL
           OR NULLIF(btrim(item->>'source_row_key'), '') IS NULL
           OR NULLIF(btrim(item->>'metric'), '') IS NULL
           OR jsonb_typeof(item->'old_value') <> 'number'
           OR jsonb_typeof(item->'new_value') <> 'number'
           OR (item->>'old_value') !~ '^-?(0|[1-9][0-9]*)$'
           OR (item->>'new_value') !~ '^-?(0|[1-9][0-9]*)$'
           OR (item->>'old_value')::numeric < -9223372036854775808
           OR (item->>'old_value')::numeric > 9223372036854775807
           OR (item->>'new_value')::numeric < -9223372036854775808
           OR (item->>'new_value')::numeric > 9223372036854775807
           OR (item->>'old_value')::bigint = (item->>'new_value')::bigint
           OR NULLIF(btrim(item->>'unit'), '') IS NULL
           OR NULLIF(btrim(item->>'filing_id'), '') IS NULL
           OR NULLIF(btrim(item->>'ticker'), '') IS NULL
           OR NULLIF(btrim(item->>'period'), '') IS NULL
           OR NULLIF(btrim(item->>'quarter'), '') IS NULL
           OR NULLIF(btrim(item->>'segment_name'), '') IS NULL
           OR jsonb_typeof(item->'segment_key') NOT IN ('string', 'null')
           OR (
               jsonb_typeof(item->'segment_key') = 'string'
               AND btrim(item->>'segment_key') = ''
           )
           OR NULLIF(btrim(item->>'source'), '') IS NULL
           OR NULLIF(btrim(item->>'existing_updated_at'), '') IS NULL
           OR NULLIF(btrim(item->>'evidence_digest'), '') IS NULL
           OR NULLIF(btrim(item->>'run_id'), '') IS NULL
           OR item->>'metric' <> 'sales'
           OR item->>'unit' <> 'millions_jpy'
    ) THEN
        RAISE EXCEPTION 'repair payload validation failed'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_repairs) AS item,
             LATERAL jsonb_object_keys(item) AS key
        WHERE key NOT IN (
            'canonical_segments_id', 'segment_id', 'source_row_key',
            'metric', 'old_value', 'new_value', 'unit', 'filing_id',
            'ticker', 'period', 'quarter', 'segment_name', 'segment_key',
            'source', 'existing_updated_at', 'evidence_digest', 'run_id'
        )
    ) THEN
        RAISE EXCEPTION 'repair payload contains an unsupported field'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_repairs) AS item
        GROUP BY item->>'source_row_key'
        HAVING count(*) <> 1
    ) OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_repairs) AS item
        GROUP BY (item->>'canonical_segments_id')::bigint
        HAVING count(*) <> 1
    ) THEN
        RAISE EXCEPTION 'repair payload contains a duplicate identity'
            USING ERRCODE = '22023';
    END IF;

    FOR v_item IN
        SELECT item
        FROM jsonb_array_elements(p_repairs) AS item
        ORDER BY item->>'source_row_key'
    LOOP
        SELECT cs.* INTO v_row
        FROM public.canonical_segments AS cs
        WHERE cs.source_row_key = v_item->>'source_row_key'
        FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'repair target missing for source_row_key %',
                v_item->>'source_row_key' USING ERRCODE = 'P0002';
        END IF;

        v_locked := v_locked + 1;
        IF v_row.id IS DISTINCT FROM
                (v_item->>'canonical_segments_id')::bigint
           OR v_row.metric IS DISTINCT FROM v_item->>'metric'
           OR v_row.value IS DISTINCT FROM (v_item->>'old_value')::bigint
           OR v_row.unit IS DISTINCT FROM v_item->>'unit'
           OR v_row.filing_id IS DISTINCT FROM v_item->>'filing_id'
           OR v_row.ticker IS DISTINCT FROM v_item->>'ticker'
           OR v_row.period IS DISTINCT FROM v_item->>'period'
           OR v_row.quarter IS DISTINCT FROM v_item->>'quarter'
           OR v_row.segment_name IS DISTINCT FROM v_item->>'segment_name'
           OR v_row.segment_key IS DISTINCT FROM v_item->>'segment_key'
           OR v_row.source IS DISTINCT FROM v_item->>'source'
           OR v_row.updated_at IS DISTINCT FROM
                (v_item->>'existing_updated_at')::timestamptz THEN
            RAISE EXCEPTION 'exact before predicate mismatch for source_row_key %',
                v_item->>'source_row_key' USING ERRCODE = 'P0001';
        END IF;
        v_matched := v_matched + 1;
    END LOOP;

    FOR v_item IN
        SELECT item
        FROM jsonb_array_elements(p_repairs) AS item
        ORDER BY item->>'source_row_key'
    LOOP
        UPDATE public.canonical_segments AS cs
        SET value = (v_item->>'new_value')::bigint
        WHERE cs.source_row_key = v_item->>'source_row_key'
          AND cs.id IS NOT DISTINCT FROM
                (v_item->>'canonical_segments_id')::bigint
          AND cs.metric IS NOT DISTINCT FROM v_item->>'metric'
          AND cs.value IS NOT DISTINCT FROM (v_item->>'old_value')::bigint
          AND cs.unit IS NOT DISTINCT FROM v_item->>'unit'
          AND cs.filing_id IS NOT DISTINCT FROM v_item->>'filing_id'
          AND cs.ticker IS NOT DISTINCT FROM v_item->>'ticker'
          AND cs.period IS NOT DISTINCT FROM v_item->>'period'
          AND cs.quarter IS NOT DISTINCT FROM v_item->>'quarter'
          AND cs.segment_name IS NOT DISTINCT FROM v_item->>'segment_name'
          AND cs.segment_key IS NOT DISTINCT FROM v_item->>'segment_key'
          AND cs.source IS NOT DISTINCT FROM v_item->>'source'
          AND cs.updated_at IS NOT DISTINCT FROM
                (v_item->>'existing_updated_at')::timestamptz;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'guarded update failed for source_row_key %',
                v_item->>'source_row_key' USING ERRCODE = 'P0001';
        END IF;
        v_updated := v_updated + 1;

        SELECT cs.* INTO v_row
        FROM public.canonical_segments AS cs
        WHERE cs.source_row_key = v_item->>'source_row_key';

        IF NOT FOUND
           OR v_row.id IS DISTINCT FROM
                (v_item->>'canonical_segments_id')::bigint
           OR v_row.metric IS DISTINCT FROM v_item->>'metric'
           OR v_row.value IS DISTINCT FROM (v_item->>'new_value')::bigint
           OR v_row.unit IS DISTINCT FROM v_item->>'unit'
           OR v_row.filing_id IS DISTINCT FROM v_item->>'filing_id'
           OR v_row.ticker IS DISTINCT FROM v_item->>'ticker'
           OR v_row.period IS DISTINCT FROM v_item->>'period'
           OR v_row.quarter IS DISTINCT FROM v_item->>'quarter'
           OR v_row.segment_name IS DISTINCT FROM v_item->>'segment_name'
           OR v_row.segment_key IS DISTINCT FROM v_item->>'segment_key'
           OR v_row.source IS DISTINCT FROM v_item->>'source'
           OR v_row.updated_at IS DISTINCT FROM
                (v_item->>'existing_updated_at')::timestamptz THEN
            RAISE EXCEPTION 'post-update verification failed for source_row_key %',
                v_item->>'source_row_key' USING ERRCODE = 'P0001';
        END IF;

        v_result_rows := v_result_rows || jsonb_build_array(
            jsonb_build_object(
                'canonical_segments_id', v_row.id,
                'segment_id', (v_item->>'segment_id')::bigint,
                'source_row_key', v_row.source_row_key,
                'metric', v_row.metric,
                'before_value', (v_item->>'old_value')::bigint,
                'after_value', v_row.value,
                'unit', v_row.unit,
                'filing_id', v_row.filing_id,
                'run_id', v_item->>'run_id'
            )
        );
    END LOOP;

    IF v_locked <> v_requested
       OR v_matched <> v_requested
       OR v_updated <> v_requested THEN
        RAISE EXCEPTION 'repair count mismatch' USING ERRCODE = 'P0001';
    END IF;

    RETURN jsonb_build_object(
        'requested_count', v_requested,
        'locked_count', v_locked,
        'matched_count', v_matched,
        'updated_count', v_updated,
        'rows', v_result_rows,
        'error_count', 0
    );
END;
$function$;

REVOKE ALL ON FUNCTION
    public.repair_canonical_segment_eav_values_exact(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.repair_canonical_segment_eav_values_exact(jsonb) FROM anon;
REVOKE ALL ON FUNCTION
    public.repair_canonical_segment_eav_values_exact(jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION
    public.repair_canonical_segment_eav_values_exact(jsonb) TO service_role;
