from pathlib import Path


SQL = (Path(__file__).parents[1] / "migrations" / "005_atomic_canonical_sales_repair_rpc.sql").read_text(encoding="utf-8")
LOWER = SQL.lower()


def test_rpc_is_bounded_and_atomic():
    assert "repair_segment_canonical_sales_exact" in SQL
    assert "jsonb_array_length(p_repairs)" in SQL
    assert "v_requested = 0 or v_requested > 10" in LOWER
    assert "for update" in LOWER
    assert "raise exception" in LOWER


def test_rpc_updates_sales_only():
    update = LOWER.split("update public.segment_canonical", 1)[1].split("where", 1)[0]
    assert "set sales" in update
    for forbidden in ("profit =", "segment_key =", "source =", "updated_at ="):
        assert forbidden not in update


def test_rpc_uses_complete_destination_identity():
    for field in ("ticker", "period", "quarter", "segment_name"):
        assert f"sc.{field}" in LOWER
    assert "segment_canonical_pkey" not in LOWER


def test_rpc_requires_exact_business_predicates():
    for field in ("existing_sales", "existing_profit", "segment_key", "source", "existing_updated_at"):
        assert field in SQL
    assert "is distinct from" in LOWER


def test_rpc_rejects_empty_duplicate_and_same_value_payloads():
    assert "v_requested = 0" in LOWER
    assert "group by (item->>'segment_id')::bigint" in LOWER
    assert "existing_sales')::bigint = (item->>'replacement_sales')::bigint" in LOWER


def test_rpc_rejects_requested_identity_substitution():
    assert "requested_id" in SQL
    assert "requested_disclosure_no" in SQL
    assert "unsupported field" in LOWER


def test_rpc_has_fixed_unit_contract():
    assert "(item->>'unit') <> 'millions_jpy'" in SQL


def test_rpc_has_no_dynamic_sql_or_arbitrary_table():
    function_body = LOWER.split("as $function$", 1)[1].split("$function$", 1)[0]
    assert "execute " not in function_body
    assert "format(" not in LOWER
    assert "public.segment_canonical" in LOWER
    assert "canonical_segments" not in LOWER


def test_rpc_security_contract():
    assert "security definer" in LOWER
    assert "set search_path = pg_catalog, public" in LOWER
    assert "from public" in LOWER
    assert "from anon" in LOWER
    assert "from authenticated" in LOWER
    assert "to service_role" in LOWER


def test_rpc_returns_complete_counts_and_rows():
    for field in ("requested_count", "locked_count", "matched_count", "updated_count", "error_count"):
        assert field in SQL
    assert "segment_id" in SQL
    assert "before_sales" in SQL
    assert "after_sales" in SQL


def test_rpc_preserves_updated_at_without_trigger_assumptions():
    assert "v_row.updated_at is distinct from" in LOWER
    assert "sc.updated_at is not distinct from" in LOWER


def test_rpc_requires_run_and_evidence():
    assert "repair_evidence" in SQL
    assert "run_id" in SQL


def test_rpc_is_deterministically_lock_ordered():
    assert LOWER.count("order by (item->>'segment_id')::bigint") == 2


def test_rpc_migration_does_not_change_schema_objects_other_than_function():
    for forbidden in ("create table", "alter table", "create trigger", "create index", "create view"):
        assert forbidden not in LOWER
