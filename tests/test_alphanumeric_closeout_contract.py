from pathlib import Path

from tools.audit_legacy_alpha_mapping import classify


ROOT = Path(__file__).resolve().parent.parent
MIGRATION = (
    ROOT / "migrations" / "010_repair_alphanumeric_pl_collision_v1.sql"
).read_text(encoding="utf-8")


def test_value_match_alone_is_deferred_lineage_review():
    assert classify(1, 1) == "SUSPECTED_REQUIRES_LINEAGE"


def test_idempotency_guard_precedes_every_destructive_statement():
    guard = MIGRATION.index("IF EXISTS(SELECT 1 FROM public.alphanumeric_pl_collision_runs_v1")
    first_delete = MIGRATION.index("DELETE FROM public.canonical_financials")
    assert guard < first_delete
    assert "'status','ALREADY_APPLIED','changed',0" in MIGRATION[guard:first_delete]


def test_preview_exception_rolls_back_before_public_replacement():
    preview = MIGRATION.index("IF p_preview THEN RAISE EXCEPTION 'preview rollback'")
    first_delete = MIGRATION.index("DELETE FROM public.canonical_financials")
    assert preview < first_delete


def test_rpc_scope_is_bounded_to_alpha_tickers_and_fiscal_months():
    assert "x->>'ticker' NOT IN ('418A','472A')" in MIGRATION
    assert "x->>'ticker'='418A' AND x->>'period' NOT LIKE '%-11-30'" in MIGRATION
    assert "x->>'ticker'='472A' AND x->>'period' NOT LIKE '%-12-31'" in MIGRATION


def test_rpc_execute_is_revoked_from_public_application_roles():
    signature = (
        "public.repair_alphanumeric_pl_collision_v1(jsonb,jsonb,boolean)"
    )
    assert (
        "REVOKE ALL ON FUNCTION "
        + signature
        + " FROM PUBLIC,anon,authenticated"
    ) in MIGRATION
    assert "GRANT EXECUTE ON FUNCTION " + signature + " TO service_role" in MIGRATION
