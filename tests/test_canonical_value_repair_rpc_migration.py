from pathlib import Path


SQL = (Path(__file__).parents[1] / "migrations" / "006_atomic_canonical_value_repair_rpc.sql").read_text(encoding="utf-8")
LOWER = SQL.lower()


def test_rpc_is_bounded_atomic_and_deterministically_locked():
    assert "repair_segment_canonical_values_exact" in SQL
    assert "v_requested = 0 or v_requested > 200" in LOWER
    assert "for update" in LOWER
    assert LOWER.count("order by (item->>'segment_id')::bigint") == 2


def test_rpc_updates_only_sales_and_profit():
    update = LOWER.split("update public.segment_canonical", 1)[1].split("where", 1)[0]
    assert "set sales" in update
    assert "profit =" in update
    for forbidden in ("segment_key =", "source =", "updated_at ="):
        assert forbidden not in update


def test_rpc_guards_complete_identity_values_and_provenance():
    for field in (
        "ticker", "period", "quarter", "segment_name", "existing_sales",
        "existing_profit", "segment_key", "source", "existing_updated_at",
    ):
        assert field in SQL
    assert "is distinct from" in LOWER
    assert "is not distinct from" in LOWER


def test_rpc_allows_nullable_existing_segment_key_but_still_guards_it():
    assert "nullif(btrim(item->>'segment_key'), '') is null" not in LOWER
    assert "v_row.segment_key is distinct from" in LOWER
    assert "sc.segment_key is not distinct from" in LOWER


def test_rpc_rejects_empty_duplicate_noop_and_substitution_fields():
    assert "v_requested = 0" in LOWER
    assert "group by (item->>'segment_id')::bigint" in LOWER
    assert "existing_sales')::bigint is not distinct from" in LOWER
    assert "existing_profit')::bigint is not distinct from" in LOWER
    assert "requested_id" in SQL and "requested_disclosure_no" in SQL
    assert "unsupported field" in LOWER


def test_rpc_fixed_unit_and_evidence_contract():
    assert "(item->>'unit') <> 'millions_jpy'" in SQL
    assert "repair_evidence" in SQL and "run_id" in SQL


def test_rpc_has_no_dynamic_sql_or_other_data_mutation():
    body = LOWER.split("as $function$", 1)[1].split("$function$", 1)[0]
    assert "execute " not in body
    assert "format(" not in body
    assert "canonical_segments" not in body
    for forbidden in ("insert into", "delete from", "create table", "alter table", "create trigger"):
        assert forbidden not in body


def test_rpc_security_contract_is_service_role_only():
    assert "security definer" in LOWER
    assert "set search_path = pg_catalog, public" in LOWER
    assert "from public" in LOWER
    assert "from anon" in LOWER
    assert "from authenticated" in LOWER
    assert "to service_role" in LOWER


def test_rpc_returns_before_after_values_and_complete_counts():
    for field in (
        "requested_count", "locked_count", "matched_count", "updated_count", "error_count",
        "before_sales", "after_sales", "before_profit", "after_profit",
    ):
        assert field in SQL


def test_rpc_preserves_updated_at_as_an_exact_guard():
    assert "v_row.updated_at is distinct from" in LOWER
    assert "sc.updated_at is not distinct from" in LOWER
