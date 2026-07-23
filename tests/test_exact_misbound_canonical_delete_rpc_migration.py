from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "migrations"
    / "007_exact_misbound_canonical_delete_rpc.sql"
).read_text(encoding="utf-8")
LOWER = SQL.lower()


def test_function_name_and_jsonb_contract():
    assert "delete_segment_canonical_misbound_exact(p_deletes jsonb)" in SQL


def test_security_definer_and_fixed_search_path():
    assert "SECURITY DEFINER" in SQL
    assert "SET search_path = pg_catalog, public" in SQL


def test_bounded_batch():
    assert "v_requested = 0 OR v_requested > 50" in SQL


def test_requires_exact_misbound_identity():
    for field in (
        "misbound_ticker",
        "misbound_period",
        "misbound_quarter",
        "misbound_segment_name",
    ):
        assert field in SQL


def test_requires_exact_destination_identity():
    for field in (
        "destination_ticker",
        "destination_period",
        "destination_quarter",
        "destination_segment_name",
    ):
        assert field in SQL


def test_guards_all_business_values():
    for field in (
        "misbound_sales",
        "misbound_profit",
        "destination_sales",
        "destination_profit",
    ):
        assert field in SQL


def test_guards_provenance_and_updated_at():
    for field in (
        "misbound_segment_key",
        "misbound_source",
        "misbound_updated_at",
        "destination_segment_key",
        "destination_source",
        "destination_updated_at",
    ):
        assert field in SQL


def test_requires_millions_jpy_unit():
    assert "item->>'unit' <> 'millions_jpy'" in SQL


def test_rejects_requested_identity_substitution():
    assert "'requested_id','requested_disclosure_no','filing_id'" in SQL


def test_rejects_same_source_and_destination_identity():
    assert "misbound identity" not in LOWER
    assert "item->>'misbound_ticker'," in SQL
    assert ") = (" in SQL


def test_rejects_unsupported_fields():
    assert "delete payload contains an unsupported field" in SQL


def test_rejects_duplicate_segment_ids_and_identities():
    assert "GROUP BY (item->>'segment_id')::bigint" in SQL
    assert "delete payload contains a duplicate identity" in SQL


def test_locks_misbound_and_destination_before_delete():
    assert SQL.count("FOR UPDATE;") == 2
    assert LOWER.index("for update;") < LOWER.index("delete from public.segment_canonical")


def test_only_mutating_statement_is_exact_delete():
    assert LOWER.count("delete from public.segment_canonical") == 1
    assert "update public.segment_canonical" not in LOWER
    assert "insert into public.segment_canonical" not in LOWER


def test_delete_has_full_exact_predicate():
    delete_block = LOWER.split("delete from public.segment_canonical", 1)[1]
    for token in (
        "misbound_ticker",
        "misbound_period",
        "misbound_quarter",
        "misbound_segment_name",
        "misbound_sales",
        "misbound_profit",
        "misbound_segment_key",
        "misbound_source",
        "misbound_updated_at",
    ):
        assert token in delete_block


def test_post_delete_absence_is_verified():
    assert "misbound row remained after delete" in SQL


def test_destination_is_reverified_unchanged():
    assert "destination changed during delete" in SQL


def test_all_counts_must_match():
    assert "v_locked <> v_requested * 2" in SQL
    assert "v_matched <> v_requested" in SQL
    assert "v_deleted <> v_requested" in SQL


def test_no_dynamic_sql_or_network_surface():
    assert "execute format" not in LOWER
    assert "http" not in LOWER
    assert "net." not in LOWER


def test_privileges_are_service_role_only():
    assert "REVOKE ALL" in SQL
    assert "FROM anon" in SQL
    assert "FROM authenticated" in SQL
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.delete_segment_canonical_misbound_exact(jsonb) TO service_role"
    ) in SQL
