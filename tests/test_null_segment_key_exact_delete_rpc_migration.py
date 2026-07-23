from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "migrations"
    / "008_allow_null_segment_key_exact_delete_rpc.sql"
).read_text(encoding="utf-8")
LOWER = SQL.lower()


def test_replaces_existing_rpc_without_touching_007():
    assert "CREATE OR REPLACE FUNCTION public.delete_segment_canonical_misbound_exact" in SQL
    migration_007 = (
        Path(__file__).parents[1]
        / "migrations"
        / "007_exact_misbound_canonical_delete_rpc.sql"
    ).read_text(encoding="utf-8")
    assert "NULLIF(btrim(item->>'destination_segment_key'), '') IS NULL" in migration_007


def test_segment_key_accepts_only_string_or_json_null():
    assert (
        "jsonb_typeof(item->'misbound_segment_key') NOT IN ('string', 'null')"
        in SQL
    )
    assert (
        "jsonb_typeof(item->'destination_segment_key') NOT IN ('string', 'null')"
        in SQL
    )


def test_null_is_not_coalesced_to_empty_string():
    assert "coalesce(" not in LOWER


def test_empty_misbound_segment_key_is_rejected():
    assert (
        "jsonb_typeof(item->'misbound_segment_key') = 'string'\n"
        "               AND btrim(item->>'misbound_segment_key') = ''"
    ) in SQL


def test_empty_destination_segment_key_is_rejected():
    assert (
        "jsonb_typeof(item->'destination_segment_key') = 'string'\n"
        "               AND btrim(item->>'destination_segment_key') = ''"
    ) in SQL


def test_current_null_expected_null_uses_null_safe_match():
    assert (
        "v_misbound.segment_key IS DISTINCT FROM v_item->>'misbound_segment_key'"
        in SQL
    )
    assert (
        "v_destination.segment_key IS DISTINCT FROM\n"
        "                v_item->>'destination_segment_key'"
    ) in SQL


def test_delete_predicate_is_null_safe():
    assert (
        "sc.segment_key IS NOT DISTINCT FROM v_item->>'misbound_segment_key'"
        in SQL
    )


def test_current_null_expected_nonnull_mismatches():
    assert "IS DISTINCT FROM v_item->>'misbound_segment_key'" in SQL


def test_current_nonnull_expected_null_mismatches():
    assert "IS DISTINCT FROM\n                v_item->>'destination_segment_key'" in SQL


def test_nonnull_exact_match_is_retained():
    assert "segment_key IS DISTINCT FROM" in SQL
    assert "segment_key IS NOT DISTINCT FROM" in SQL


def test_profit_null_contract_is_retained():
    assert "(v_item->>'misbound_profit')::bigint" in SQL
    assert "(v_item->>'destination_profit')::bigint" in SQL
    assert "IS DISTINCT FROM" in SQL


def test_three_null_destination_ids_are_covered_by_general_contract():
    # IDs remain data, not an RPC allowlist. The general NULL-safe predicate
    # must apply without hard-coding any canary identifier.
    for segment_id in ("221837", "221838", "221839"):
        assert segment_id not in SQL


def test_remaining_five_nonnull_items_keep_existing_path():
    assert SQL.count("FOR UPDATE;") == 2
    assert "DELETE FROM public.segment_canonical" in SQL


def test_all_eight_are_one_atomic_array():
    assert "p_deletes jsonb" in SQL
    assert "v_requested" in SQL
    assert "v_deleted <> v_requested" in SQL


def test_one_mismatch_raises_before_delete_loop():
    first_raise = LOWER.index("exact misbound predicate mismatch")
    delete_statement = LOWER.index("delete from public.segment_canonical")
    assert first_raise < delete_statement


def test_destination_is_locked_and_reverified():
    assert "canonical destination missing" in SQL
    assert "destination changed during delete" in SQL


def test_no_update_or_insert_path():
    assert "update public.segment_canonical" not in LOWER
    assert "insert into public.segment_canonical" not in LOWER


def test_canonical_segments_is_not_mutated():
    assert "delete from public.canonical_segments" not in LOWER
    assert "update public.canonical_segments" not in LOWER
    assert "insert into public.canonical_segments" not in LOWER


def test_security_contract_is_unchanged():
    assert "SECURITY DEFINER" in SQL
    assert "SET search_path = pg_catalog, public" in SQL
    assert "FROM anon" in SQL
    assert "FROM authenticated" in SQL
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.delete_segment_canonical_misbound_exact(jsonb) TO service_role"
    ) in SQL


def test_no_dynamic_sql_or_network():
    assert "execute format" not in LOWER
    assert "http" not in LOWER
    assert "net." not in LOWER


def test_requested_identity_substitution_remains_forbidden():
    assert "'requested_id','requested_disclosure_no','filing_id'" in SQL


def test_second_plan_requires_zero_pending_not_second_rpc():
    assert "second rpc" not in LOWER
    assert "RETURN jsonb_build_object" in SQL
