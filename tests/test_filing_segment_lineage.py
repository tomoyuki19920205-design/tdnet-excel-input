import sqlite3

import pytest

from lib.backfill.filing_segment_lineage import (
    FilingLineageRecord,
    ObservationRecord,
    build_route_map,
    ensure_filing_segment_lineage,
    insert_lineage,
    insert_observation,
    pending_plan,
    row_digest,
    segment_table_digest,
)


def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE segment_financials (
      id INTEGER PRIMARY KEY, company_code TEXT, fiscal_year_end TEXT,
      quarter TEXT, segment_name TEXT, segment_sales REAL, segment_profit REAL,
      tdnet_doc_id TEXT, disclosure_date TEXT)""")
    con.execute("INSERT INTO segment_financials VALUES (7,'1234','2026-03-31','FY','Cloud',10,2,'doc-old','2026-05-01')")
    ensure_filing_segment_lineage(con)
    return con


def observation(**changes):
    values = dict(
        filing_id="f1", requested_id="requested-1", tdnet_doc_id="doc-old",
        company_code="1234", fiscal_year_end="2026-03-31", quarter="FY",
        segment_name="Cloud", observed_sales=10, observed_profit=2,
        source_zip_sha256="a" * 64, disclosure_date="2026-05-01",
        source_context={"current_or_previous": "current"},
        source_concept={"sales": "Sales", "profit": "Profit"},
        relation_role="CANONICAL_SOURCE", canonical_segment_financial_id=7,
    )
    values.update(changes)
    values.setdefault("row_semantic_digest", row_digest(
        company_code=values["company_code"], fiscal_year_end=values["fiscal_year_end"],
        quarter=values["quarter"], segment_name=values["segment_name"],
        observed_sales=values["observed_sales"], observed_profit=values["observed_profit"],
    ))
    return ObservationRecord(**values)


def lineage(obs, oid, **changes):
    values = dict(
        filing_id=obs.filing_id, requested_id=obs.requested_id,
        tdnet_doc_id=obs.tdnet_doc_id, final_status="filing_ok",
        relation_role=obs.relation_role, row_semantic_digest=obs.row_semantic_digest,
        source_zip_sha256=obs.source_zip_sha256,
        canonical_segment_financial_id=obs.canonical_segment_financial_id,
        observation_id=oid,
    )
    values.update(changes)
    return FilingLineageRecord(**values)


def baseline(con):
    return dict(con.execute("SELECT * FROM segment_financials WHERE id=7").fetchone())


def test_strict_exact_lineage_and_canonical_source():
    con = db(); obs = observation(); oid = insert_observation(con, obs, baseline(con))
    insert_lineage(con, lineage(obs, oid))
    assert con.execute("SELECT relation_role FROM filing_segment_lineage").fetchone()[0] == "CANONICAL_SOURCE"


def test_equivalent_reference_requires_equal_value():
    con = db(); obs = observation(filing_id="f2", tdnet_doc_id="doc-alias", relation_role="EQUIVALENT_REFERENCE")
    assert insert_observation(con, obs, baseline(con)) > 0
    bad = observation(filing_id="f3", observed_sales=11, relation_role="EQUIVALENT_REFERENCE")
    with pytest.raises(ValueError, match="value_mismatch"):
        insert_observation(con, bad, baseline(con))


def test_conflicting_value_is_noncanonical_observation():
    con = db(); obs = observation(observed_sales=11, tdnet_doc_id="doc-new", relation_role="NONCANONICAL_OBSERVATION")
    oid = insert_observation(con, obs, baseline(con)); insert_lineage(con, lineage(obs, oid))
    assert con.execute("SELECT observed_sales FROM filing_segment_observations").fetchone()[0] == 11
    assert con.execute("SELECT segment_sales FROM segment_financials").fetchone()[0] == 10


def test_same_value_cannot_be_noncanonical():
    con = db(); obs = observation(relation_role="NONCANONICAL_OBSERVATION")
    with pytest.raises(ValueError, match="requires_value_difference"):
        insert_observation(con, obs, baseline(con))


@pytest.mark.parametrize("new_date", ["2027-01-01", "2025-01-01"])
def test_disclosure_chronology_never_changes_baseline(new_date):
    con = db(); before = segment_table_digest(con)
    obs = observation(observed_profit=3, tdnet_doc_id="other", disclosure_date=new_date,
                      relation_role="NONCANONICAL_OBSERVATION")
    insert_observation(con, obs, baseline(con))
    assert segment_table_digest(con) == before


def test_requested_id_is_never_internal_id():
    con = db(); obs = observation(
        tdnet_doc_id="requested-1",
        source_context={"tdnet_doc_id_source": "REQUESTED_ID_FALLBACK"},
    )
    with pytest.raises(ValueError, match="substitution"):
        insert_observation(con, obs, baseline(con))


def test_equal_official_ids_are_not_misclassified_as_substitution():
    con = db(); obs = observation(
        tdnet_doc_id="requested-1",
        source_context={"tdnet_doc_id_source": "XBRL_INTERNAL_ID"},
    )
    assert insert_observation(con, obs, baseline(con)) > 0


def test_null_internal_id_observation():
    con = db(); obs = observation(tdnet_doc_id=None, relation_role="EQUIVALENT_REFERENCE")
    oid = insert_observation(con, obs, baseline(con)); insert_lineage(con, lineage(obs, oid))
    assert con.execute("SELECT tdnet_doc_id FROM filing_segment_lineage").fetchone()[0] is None


def test_zero_payload_filing_lineage():
    con = db(); r = FilingLineageRecord("f0", "r0", None, "skipped_normal", "ZERO_PAYLOAD_NORMAL", "empty", zero_payload=True)
    insert_lineage(con, r)
    assert con.execute("SELECT zero_payload FROM filing_segment_lineage").fetchone()[0] == 1


def test_known_quarantine_empty_filing_lineage():
    con = db(); r = FilingLineageRecord("fq", "rq", None, "known_quarantined", "KNOWN_QUARANTINE_EMPTY", "empty-q", zero_payload=True, known_quarantine=True, quarantine_reason="too_few_sales")
    insert_lineage(con, r)
    assert con.execute("SELECT quarantine_reason FROM filing_segment_lineage").fetchone()[0] == "too_few_sales"


def test_canonical_source_is_unique_per_business_row():
    con = db()
    for filing in ("f1", "f2"):
        obs = observation(filing_id=filing); oid = insert_observation(con, obs, baseline(con))
        if filing == "f1": insert_lineage(con, lineage(obs, oid))
        else:
            with pytest.raises(sqlite3.IntegrityError): insert_lineage(con, lineage(obs, oid))


def test_idempotency_plan_reaches_zero_pending():
    con = db(); obs = observation(); initial_lineage = lineage(obs, 1)
    assert pending_plan(con, [obs], [initial_lineage])["pending_observation_insert"] == 1
    oid = insert_observation(con, obs, baseline(con)); saved = lineage(obs, oid); insert_lineage(con, saved)
    assert pending_plan(con, [obs], [saved]) == {
        "pending_observation_insert": 0, "pending_lineage_insert": 0,
        "pending_segment_insert": 0, "pending_update": 0, "pending_delete": 0,
        "conflicts": 0, "unresolved": 0,
    }


def test_route_map_dedupes_planner_ids_and_keeps_observation_only():
    rows = [
        {"filing_id": "a", "relation_role": "CANONICAL_SOURCE", "canonical_segment_financial_id": 7, "observation_id": 1},
        {"filing_id": "b", "relation_role": "EQUIVALENT_REFERENCE", "canonical_segment_financial_id": 7, "observation_id": 2},
        {"filing_id": "c", "relation_role": "NONCANONICAL_OBSERVATION", "canonical_segment_financial_id": 7, "observation_id": 3},
    ]
    result = build_route_map(rows)
    assert result["planner_segment_ids"] == [7]
    assert result["planner_id_duplicates"] == 0
    assert result["canonical_mutation_planned"] is False
    assert result["filings"][2]["routes"] == ["OBSERVATION_ONLY_NO_CANONICAL_MUTATION"]


def test_bad_digest_fails_closed():
    con = db(); obs = observation(row_semantic_digest="wrong")
    with pytest.raises(ValueError, match="digest_mismatch"):
        insert_observation(con, obs, baseline(con))


def test_filing_only_role_cannot_have_row_reference():
    con = db(); r = FilingLineageRecord("f", "r", None, "skipped_normal", "ZERO_PAYLOAD_NORMAL", "x", canonical_segment_financial_id=7, zero_payload=True)
    with pytest.raises(ValueError, match="row_reference"):
        insert_lineage(con, r)


def test_row_role_requires_observation():
    con = db(); r = FilingLineageRecord("f", "r", None, "filing_ok", "EQUIVALENT_REFERENCE", "x")
    with pytest.raises(ValueError, match="reference_missing"):
        insert_lineage(con, r)


def test_audited_preexisting_reference_does_not_require_new_observation():
    con = db(); r = FilingLineageRecord(
        "f", "r", None, "filing_ok", "EQUIVALENT_REFERENCE", "x",
        canonical_segment_financial_id=7, evidence_class="AUDITED_PREEXISTING_CANARY",
    )
    insert_lineage(con, r)
    assert con.execute("SELECT observation_id FROM filing_segment_lineage").fetchone()[0] is None


def test_schema_has_no_requested_id_copy_trigger():
    con = db()
    triggers = con.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
    assert triggers == []
