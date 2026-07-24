from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "migrations"
    / "009_exact_canonical_eav_value_repair_rpc.sql"
).read_text(encoding="utf-8")
LOWER = SQL.lower()
BODY = LOWER.split("as $function$", 1)[1].split("$function$", 1)[0]


def test_01_function_has_dedicated_eav_name_and_jsonb_contract():
    assert "repair_canonical_segment_eav_values_exact(" in SQL
    assert "p_repairs jsonb" in SQL


def test_02_security_definer_has_fixed_search_path():
    assert "SECURITY DEFINER" in SQL
    assert "SET search_path = pg_catalog, public" in SQL


def test_03_privileges_are_service_role_only():
    assert "FROM PUBLIC" in SQL
    assert "FROM anon" in SQL
    assert "FROM authenticated" in SQL
    assert "TO service_role" in SQL


def test_04_empty_and_oversized_batches_are_rejected():
    assert "v_requested = 0 OR v_requested > 50" in SQL


def test_05_duplicate_source_row_keys_are_rejected():
    assert "GROUP BY item->>'source_row_key'" in SQL
    assert "duplicate identity" in LOWER


def test_06_requested_identity_substitution_is_rejected():
    assert "'requested_id', 'requested_disclosure_no'" in SQL


def test_07_metric_is_fixed_to_sales():
    assert "item->>'metric' <> 'sales'" in SQL


def test_08_unit_is_fixed_to_millions_jpy():
    assert "item->>'unit' <> 'millions_jpy'" in SQL


def test_09_numeric_payload_is_integer_and_bigint_bounded():
    assert "jsonb_typeof(item->'old_value') <> 'number'" in SQL
    assert "jsonb_typeof(item->'new_value') <> 'number'" in SQL
    assert "9223372036854775807" in SQL
    assert "-9223372036854775808" in SQL
    assert "!~ '^-?(0|[1-9][0-9]*)$'" in SQL


def test_10_noop_value_change_is_rejected():
    assert "(item->>'old_value')::bigint = (item->>'new_value')::bigint" in SQL


def test_11_nullable_segment_key_is_exact_not_wildcard():
    assert "jsonb_typeof(item->'segment_key') NOT IN ('string', 'null')" in SQL
    assert "v_row.segment_key IS DISTINCT FROM v_item->>'segment_key'" in SQL
    assert "cs.segment_key IS NOT DISTINCT FROM v_item->>'segment_key'" in SQL


def test_12_empty_string_segment_key_is_rejected():
    assert "btrim(item->>'segment_key') = ''" in SQL


def test_13_rows_are_locked_in_deterministic_order():
    assert LOWER.count("order by item->>'source_row_key'") == 2
    assert "FOR UPDATE;" in SQL
    assert LOWER.index("for update;") < LOWER.index("update public.canonical_segments")


def test_14_complete_exact_identity_and_provenance_are_guarded():
    for field in (
        "canonical_segments_id",
        "segment_id",
        "source_row_key",
        "metric",
        "old_value",
        "unit",
        "filing_id",
        "ticker",
        "period",
        "quarter",
        "segment_name",
        "segment_key",
        "source",
        "existing_updated_at",
        "evidence_digest",
        "run_id",
    ):
        assert field in SQL


def test_15_only_value_is_mutated():
    update_set = LOWER.split("update public.canonical_segments", 1)[1].split("where", 1)[0]
    assert "set value =" in update_set
    for forbidden in (
        "source_row_key =",
        "segment_id =",
        "metric =",
        "unit =",
        "filing_id =",
        "ticker =",
        "period =",
        "quarter =",
        "segment_name =",
        "segment_key =",
        "source =",
        "updated_at =",
    ):
        assert forbidden not in update_set


def test_16_profit_eav_and_wide_table_have_no_mutation_path():
    assert "segment_canonical" not in BODY
    assert "metric = 'profit'" not in BODY


def test_17_no_insert_or_delete_path_exists():
    assert "insert into" not in BODY
    assert "delete from" not in BODY


def test_18_no_dynamic_sql_or_network_surface_exists():
    assert "execute " not in BODY
    assert "format(" not in BODY
    assert "http" not in BODY
    assert "net." not in BODY


def test_19_old_value_mismatch_rolls_back_before_update():
    assert "v_row.value IS DISTINCT FROM (v_item->>'old_value')::bigint" in SQL
    assert "exact before predicate mismatch" in SQL


def test_20_filing_and_source_row_key_are_exact_guards():
    assert "v_row.filing_id IS DISTINCT FROM v_item->>'filing_id'" in SQL
    assert "WHERE cs.source_row_key = v_item->>'source_row_key'" in SQL


def test_21_post_update_reloads_and_verifies_exact_row():
    assert "post-update verification failed" in SQL
    assert "v_row.value IS DISTINCT FROM (v_item->>'new_value')::bigint" in SQL


def test_22_all_counts_must_match():
    assert "v_locked <> v_requested" in SQL
    assert "v_matched <> v_requested" in SQL
    assert "v_updated <> v_requested" in SQL


def test_23_result_contains_before_after_and_identity():
    for field in (
        "requested_count",
        "locked_count",
        "matched_count",
        "updated_count",
        "before_value",
        "after_value",
        "source_row_key",
        "filing_id",
    ):
        assert field in SQL


def test_24_fixed_canary_segment_ids_are_not_hardcoded():
    for segment_id in ("221631", "221632", "221808"):
        assert segment_id not in SQL


def test_25_local_segment_id_is_audit_correlation_not_remote_column():
    assert "v_row.segment_id" not in SQL
    assert "cs.segment_id" not in SQL
    assert "'segment_id', (v_item->>'segment_id')::bigint" in SQL
