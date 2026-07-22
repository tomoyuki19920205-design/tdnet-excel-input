"""Fail-closed versioned filing-to-segment observation and lineage storage.

The module deliberately never mutates ``segment_financials``.  Existing rows are
the canonical baseline; a filing's differing value is retained as a versioned
observation until an explicit correction/replacement contract exists.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence


RELATION_ROLES = frozenset({
    "CANONICAL_SOURCE",
    "EQUIVALENT_REFERENCE",
    "NONCANONICAL_OBSERVATION",
    "SUPERSEDED_SOURCE",
    "CORRECTED_SOURCE",
    "ZERO_PAYLOAD_NORMAL",
    "KNOWN_QUARANTINE_EMPTY",
})

ROW_ROLES = frozenset({
    "CANONICAL_SOURCE",
    "EQUIVALENT_REFERENCE",
    "NONCANONICAL_OBSERVATION",
    "SUPERSEDED_SOURCE",
    "CORRECTED_SOURCE",
})

FILING_ONLY_ROLES = frozenset({"ZERO_PAYLOAD_NORMAL", "KNOWN_QUARANTINE_EMPTY"})

ROUTES = frozenset({
    "DIRECT_CANONICAL_REFERENCE",
    "ALIAS_CANONICAL_REFERENCE",
    "OBSERVATION_ONLY_NO_CANONICAL_MUTATION",
    "ZERO_PAYLOAD_NORMAL",
    "KNOWN_QUARANTINE_EMPTY",
})


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def semantic_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def row_digest(*, company_code: str, fiscal_year_end: str, quarter: str,
               segment_name: str, observed_sales: float | int | None,
               observed_profit: float | int | None) -> str:
    return semantic_digest({
        "company_code": company_code,
        "fiscal_year_end": fiscal_year_end,
        "observed_profit": observed_profit,
        "observed_sales": observed_sales,
        "quarter": quarter,
        "segment_name": segment_name,
    })


@dataclass(frozen=True)
class ObservationRecord:
    filing_id: str
    requested_id: str
    tdnet_doc_id: str | None
    company_code: str
    fiscal_year_end: str
    quarter: str
    segment_name: str
    observed_sales: float | int | None
    observed_profit: float | int | None
    source_zip_sha256: str
    row_semantic_digest: str
    disclosure_date: str | None
    source_context: Mapping[str, Any]
    source_concept: Mapping[str, Any]
    relation_role: str
    canonical_segment_financial_id: int | None


@dataclass(frozen=True)
class FilingLineageRecord:
    filing_id: str
    requested_id: str
    tdnet_doc_id: str | None
    final_status: str
    relation_role: str
    row_semantic_digest: str
    source_zip_sha256: str | None = None
    canonical_segment_financial_id: int | None = None
    observation_id: int | None = None
    zero_payload: bool = False
    known_quarantine: bool = False
    quarantine_reason: str | None = None
    evidence_class: str = "V3_VERIFIED_CURRENT_PERIOD"


# Compatibility name retained for the already-started feature.
LineageRecord = FilingLineageRecord


def ensure_filing_segment_lineage(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS filing_segment_observations (
          observation_id INTEGER PRIMARY KEY,
          filing_id TEXT NOT NULL,
          requested_id TEXT NOT NULL,
          tdnet_doc_id TEXT NULL,
          company_code TEXT NOT NULL,
          fiscal_year_end TEXT NOT NULL,
          quarter TEXT NOT NULL,
          segment_name TEXT NOT NULL,
          observed_sales REAL NULL,
          observed_profit REAL NULL,
          source_zip_sha256 TEXT NOT NULL,
          row_semantic_digest TEXT NOT NULL,
          disclosure_date TEXT NULL,
          source_context_json TEXT NOT NULL,
          source_concept_json TEXT NOT NULL,
          relation_role TEXT NOT NULL CHECK (relation_role IN (
            'CANONICAL_SOURCE','EQUIVALENT_REFERENCE','NONCANONICAL_OBSERVATION',
            'SUPERSEDED_SOURCE','CORRECTED_SOURCE')),
          canonical_segment_financial_id INTEGER NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE (filing_id, row_semantic_digest),
          FOREIGN KEY (canonical_segment_financial_id) REFERENCES segment_financials(id)
        );
        CREATE TABLE IF NOT EXISTS filing_segment_lineage (
          lineage_id INTEGER PRIMARY KEY,
          filing_id TEXT NOT NULL,
          requested_id TEXT NOT NULL,
          tdnet_doc_id TEXT NULL,
          final_status TEXT NOT NULL,
          relation_role TEXT NOT NULL CHECK (relation_role IN (
            'CANONICAL_SOURCE','EQUIVALENT_REFERENCE','NONCANONICAL_OBSERVATION',
            'SUPERSEDED_SOURCE','CORRECTED_SOURCE','ZERO_PAYLOAD_NORMAL',
            'KNOWN_QUARANTINE_EMPTY')),
          row_semantic_digest TEXT NOT NULL,
          source_zip_sha256 TEXT NULL,
          canonical_segment_financial_id INTEGER NULL,
          observation_id INTEGER NULL,
          zero_payload INTEGER NOT NULL DEFAULT 0 CHECK (zero_payload IN (0,1)),
          known_quarantine INTEGER NOT NULL DEFAULT 0 CHECK (known_quarantine IN (0,1)),
          quarantine_reason TEXT NULL,
          evidence_class TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE (filing_id, row_semantic_digest),
          FOREIGN KEY (canonical_segment_financial_id) REFERENCES segment_financials(id),
          FOREIGN KEY (observation_id) REFERENCES filing_segment_observations(observation_id),
          CHECK (
            (relation_role IN ('ZERO_PAYLOAD_NORMAL','KNOWN_QUARANTINE_EMPTY')
             AND observation_id IS NULL AND canonical_segment_financial_id IS NULL
             AND zero_payload = 1)
            OR
            (relation_role NOT IN ('ZERO_PAYLOAD_NORMAL','KNOWN_QUARANTINE_EMPTY')
             AND (observation_id IS NOT NULL OR canonical_segment_financial_id IS NOT NULL)
             AND zero_payload = 0)
          )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_filing_segment_canonical_source
          ON filing_segment_lineage(canonical_segment_financial_id)
          WHERE relation_role='CANONICAL_SOURCE';
    """)


def _required_text(value: str | None, code: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(code)
    return normalized


def validate_observation(record: ObservationRecord,
                         baseline: Mapping[str, Any] | None = None) -> None:
    if record.relation_role not in ROW_ROLES:
        raise ValueError("observation_relation_role_invalid")
    for value, code in (
        (record.filing_id, "filing_id_missing"),
        (record.requested_id, "requested_id_missing"),
        (record.company_code, "company_code_missing"),
        (record.fiscal_year_end, "period_missing"),
        (record.quarter, "quarter_missing"),
        (record.segment_name, "segment_name_missing"),
        (record.source_zip_sha256, "source_zip_sha256_missing"),
        (record.row_semantic_digest, "row_semantic_digest_missing"),
    ):
        _required_text(value, code)
    if record.source_context.get("tdnet_doc_id_source") == "REQUESTED_ID_FALLBACK":
        raise ValueError("requested_id_substitution_forbidden")
    expected = row_digest(
        company_code=record.company_code,
        fiscal_year_end=record.fiscal_year_end,
        quarter=record.quarter,
        segment_name=record.segment_name,
        observed_sales=record.observed_sales,
        observed_profit=record.observed_profit,
    )
    if record.row_semantic_digest != expected:
        raise ValueError("row_semantic_digest_mismatch")
    if record.relation_role in {"CANONICAL_SOURCE", "EQUIVALENT_REFERENCE"}:
        if not baseline or record.canonical_segment_financial_id is None:
            raise ValueError("equivalent_reference_baseline_missing")
        keys = ("company_code", "fiscal_year_end", "quarter", "segment_name")
        if any(str(baseline[k]) != str(getattr(record, k)) for k in keys):
            raise ValueError("equivalent_reference_natural_key_mismatch")
        if baseline.get("segment_sales") != record.observed_sales or baseline.get("segment_profit") != record.observed_profit:
            raise ValueError("equivalent_reference_value_mismatch")
    if record.relation_role == "NONCANONICAL_OBSERVATION" and baseline:
        same = (baseline.get("segment_sales") == record.observed_sales and
                baseline.get("segment_profit") == record.observed_profit)
        if same:
            raise ValueError("noncanonical_observation_requires_value_difference")


def insert_observation(conn: sqlite3.Connection, record: ObservationRecord,
                       baseline: Mapping[str, Any] | None = None) -> int:
    validate_observation(record, baseline)
    cur = conn.execute("""
        INSERT INTO filing_segment_observations
        (filing_id,requested_id,tdnet_doc_id,company_code,fiscal_year_end,quarter,
         segment_name,observed_sales,observed_profit,source_zip_sha256,
         row_semantic_digest,disclosure_date,source_context_json,source_concept_json,
         relation_role,canonical_segment_financial_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        record.filing_id, record.requested_id, record.tdnet_doc_id,
        record.company_code, record.fiscal_year_end, record.quarter,
        record.segment_name, record.observed_sales, record.observed_profit,
        record.source_zip_sha256, record.row_semantic_digest,
        record.disclosure_date, canonical_bytes(record.source_context).decode("utf-8"),
        canonical_bytes(record.source_concept).decode("utf-8"), record.relation_role,
        record.canonical_segment_financial_id,
    ))
    return int(cur.lastrowid)


def insert_lineage(conn: sqlite3.Connection, record: FilingLineageRecord) -> int:
    if record.relation_role not in RELATION_ROLES:
        raise ValueError("lineage_relation_role_invalid")
    for value, code in (
        (record.filing_id, "filing_id_missing"),
        (record.requested_id, "requested_id_missing"),
        (record.final_status, "final_status_missing"),
        (record.row_semantic_digest, "row_semantic_digest_missing"),
        (record.evidence_class, "lineage_evidence_class_missing"),
    ):
        _required_text(value, code)
    if record.evidence_class == "REQUESTED_ID_SUBSTITUTION":
        raise ValueError("requested_id_substitution_forbidden")
    filing_only = record.relation_role in FILING_ONLY_ROLES
    if filing_only != bool(record.zero_payload):
        raise ValueError("lineage_zero_payload_role_mismatch")
    if filing_only and (record.observation_id is not None or record.canonical_segment_financial_id is not None):
        raise ValueError("filing_only_lineage_has_row_reference")
    if (not filing_only and record.observation_id is None and
            record.canonical_segment_financial_id is None):
        raise ValueError("row_lineage_reference_missing")
    cur = conn.execute("""
        INSERT INTO filing_segment_lineage
        (filing_id,requested_id,tdnet_doc_id,final_status,relation_role,
         row_semantic_digest,source_zip_sha256,canonical_segment_financial_id,
         observation_id,zero_payload,known_quarantine,quarantine_reason,evidence_class)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        record.filing_id, record.requested_id, record.tdnet_doc_id,
        record.final_status, record.relation_role, record.row_semantic_digest,
        record.source_zip_sha256, record.canonical_segment_financial_id,
        record.observation_id, int(record.zero_payload), int(record.known_quarantine),
        record.quarantine_reason, record.evidence_class,
    ))
    return int(cur.lastrowid)


def pending_plan(conn: sqlite3.Connection,
                 observations: Sequence[ObservationRecord],
                 filing_rows: Sequence[FilingLineageRecord]) -> dict[str, Any]:
    existing_observations = {
        (str(row[0]), str(row[1])) for row in conn.execute(
            "SELECT filing_id,row_semantic_digest FROM filing_segment_observations"
        )
    }
    existing_lineage = {
        (str(row[0]), str(row[1])) for row in conn.execute(
            "SELECT filing_id,row_semantic_digest FROM filing_segment_lineage"
        )
    }
    obs_pending = [r for r in observations if (r.filing_id, r.row_semantic_digest) not in existing_observations]
    lineage_pending = [r for r in filing_rows if (r.filing_id, r.row_semantic_digest) not in existing_lineage]
    return {
        "pending_observation_insert": len(obs_pending),
        "pending_lineage_insert": len(lineage_pending),
        "pending_segment_insert": 0,
        "pending_update": 0,
        "pending_delete": 0,
        "conflicts": 0,
        "unresolved": 0,
    }


def segment_table_digest(conn: sqlite3.Connection) -> str:
    columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(segment_financials)")]
    rows = [dict(zip(columns, row)) for row in conn.execute(
        f"SELECT {','.join(columns)} FROM segment_financials ORDER BY id"
    )]
    return semantic_digest(rows)


def build_route_map(lineage_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    filings: dict[str, dict[str, Any]] = {}
    planner_ids: set[int] = set()
    for raw in lineage_rows:
        row = dict(raw)
        filing_id = str(row["filing_id"])
        role = str(row["relation_role"])
        item = filings.setdefault(filing_id, {
            "filing_id": filing_id, "routes": set(), "segment_ids": set(),
            "observation_ids": set(),
        })
        segment_id = row.get("canonical_segment_financial_id")
        observation_id = row.get("observation_id")
        if segment_id is not None:
            segment_id = int(segment_id)
            item["segment_ids"].add(segment_id)
            planner_ids.add(segment_id)
        if observation_id is not None:
            item["observation_ids"].add(int(observation_id))
        if role == "CANONICAL_SOURCE":
            item["routes"].add("DIRECT_CANONICAL_REFERENCE")
        elif role == "EQUIVALENT_REFERENCE":
            item["routes"].add("ALIAS_CANONICAL_REFERENCE")
        elif role in {"NONCANONICAL_OBSERVATION", "SUPERSEDED_SOURCE", "CORRECTED_SOURCE"}:
            item["routes"].add("OBSERVATION_ONLY_NO_CANONICAL_MUTATION")
        elif role in FILING_ONLY_ROLES:
            item["routes"].add(role)
    result = []
    for filing_id in sorted(filings):
        item = filings[filing_id]
        result.append({
            "filing_id": filing_id,
            "routes": sorted(item["routes"]),
            "canonical_segment_financial_ids": sorted(item["segment_ids"]),
            "observation_ids": sorted(item["observation_ids"]),
        })
    return {
        "filings": result,
        "planner_segment_ids": sorted(planner_ids),
        "planner_id_duplicates": 0,
        "canonical_mutation_planned": False,
    }


def record_to_dict(record: ObservationRecord | FilingLineageRecord) -> dict[str, Any]:
    return asdict(record)
