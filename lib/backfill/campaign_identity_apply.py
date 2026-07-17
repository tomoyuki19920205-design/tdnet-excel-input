"""Apply a verified V4 identity plan to an explicitly temporary campaign DB."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from lib.backfill.campaign_state import SCHEMA_VERSION, get_schema_version


STOP_INPUT = "STOP_V4_CAMPAIGN_IDENTITY_APPLY_INPUT_CHANGED"
STOP_PRECONDITION = "STOP_V4_CAMPAIGN_IDENTITY_APPLY_PRECONDITION_CHANGED"
STOP_UNSAFE_PATH = "STOP_V4_CAMPAIGN_IDENTITY_APPLY_UNSAFE_DB_PATH"
STOP_TEMP_FAILED = "STOP_V4_CAMPAIGN_IDENTITY_APPLY_TEMP_FAILED"
STOP_PARTIAL = "STOP_V4_CAMPAIGN_IDENTITY_APPLY_PARTIAL_COMMIT"
STOP_SEMANTIC = "STOP_V4_CAMPAIGN_IDENTITY_APPLY_SEMANTIC_MISMATCH"

EXPECTED_CLASSIFICATION_COUNTS = {
    "READY_IDENTITY_VERIFIED": 367,
    "TARGET_ZIP_NEEDS_SIDECAR": 1175,
    "LEGACY_CACHE_COPY_CANDIDATE": 3529,
    "METADATA_RESOLVED_CACHE_MISSING": 40818,
    "METADATA_INCOMPLETE_CACHE_MISSING": 0,
    "CACHE_IDENTITY_MISMATCH": 321,
    "TARGET_CACHE_CONFLICT": 2,
    "LEGACY_STATE_AMBIGUOUS": 6,
    "JQUANTS_METADATA_AMBIGUOUS": 0,
    "INVALID_OR_UNSUPPORTED_URL": 0,
    "NOT_APPLICABLE": 0,
    "OTHER_UNRESOLVED": 0,
}

# identity_status, cache_status, overall_status, error_code, retryable
STATUS_MAPPING = {
    "READY_IDENTITY_VERIFIED": ("VERIFIED", "READY", "IDENTITY_VERIFIED", None, 1),
    "TARGET_ZIP_NEEDS_SIDECAR": ("VERIFIED", "SIDECAR_REQUIRED", "IDENTITY_VERIFIED", None, 1),
    "LEGACY_CACHE_COPY_CANDIDATE": ("VERIFIED", "LEGACY_COPY_REQUIRED", "IDENTITY_VERIFIED", None, 1),
    "METADATA_RESOLVED_CACHE_MISSING": ("METADATA_RESOLVED", "MISSING", "IDENTITY_RESOLVED", None, 1),
    "METADATA_INCOMPLETE_CACHE_MISSING": ("UNRESOLVED", "MISSING", "METADATA_INCOMPLETE", "METADATA_INCOMPLETE", 1),
    "CACHE_IDENTITY_MISMATCH": ("MISMATCH", "IDENTITY_MISMATCH", "QUARANTINED", "CACHE_IDENTITY_MISMATCH", 0),
    "TARGET_CACHE_CONFLICT": ("CONFLICT", "CONFLICT", "QUARANTINED", "TARGET_CACHE_CONFLICT", 0),
    "LEGACY_STATE_AMBIGUOUS": ("AMBIGUOUS", "AMBIGUOUS", "QUARANTINED", "LEGACY_STATE_AMBIGUOUS", 0),
    "JQUANTS_METADATA_AMBIGUOUS": ("AMBIGUOUS", "UNKNOWN", "QUARANTINED", "JQUANTS_METADATA_AMBIGUOUS", 0),
    "INVALID_OR_UNSUPPORTED_URL": ("UNRESOLVED", "INVALID", "QUARANTINED", "INVALID_OR_UNSUPPORTED_URL", 0),
    "NOT_APPLICABLE": ("NOT_APPLICABLE", "NOT_APPLICABLE", "NOT_APPLICABLE", None, 0),
    "OTHER_UNRESOLVED": ("UNRESOLVED", "UNKNOWN", "QUARANTINED", "OTHER_UNRESOLVED", 0),
}

UNCHANGED_FIELDS = (
    "campaign_id", "manifest_row_id", "requested_disclosure_no", "company_code",
    "normalized_company_code", "source_url", "normalized_xbrl_url",
    "registration_status", "extraction_status", "sqlite_save_status",
    "canonical_save_status", "supabase_save_status", "started_at", "completed_at",
)

APPLIED_FIELDS = (
    "expected_period", "expected_quarter", "internal_document_id", "zip_sha256",
    "zip_internal_ticker", "zip_internal_period", "zip_internal_quarter",
    "identity_status", "cache_status", "overall_status", "error_code", "error_stage",
    "error_message", "retryable",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_temp_path(path: Path, repo_root: Path) -> None:
    if not path.is_absolute() or _is_under(path, repo_root):
        raise RuntimeError(STOP_UNSAFE_PATH)
    roots = (Path(r"C:\tmp"), Path(tempfile.gettempdir()))
    if not any(_is_under(path, root) for root in roots):
        raise RuntimeError(STOP_UNSAFE_PATH)


def _load_digest_manifest(plan_path: Path, plan_sha256: str) -> None:
    digest_path = plan_path.parent / "digests.json"
    try:
        digests = json.loads(digest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(STOP_INPUT) from exc
    if not isinstance(digests, dict) or digests.get(plan_path.name) != plan_sha256:
        raise RuntimeError(STOP_INPUT)
    for name, expected in digests.items():
        candidate = plan_path.parent / str(name)
        if not candidate.is_file() or sha256_file(candidate) != expected:
            raise RuntimeError(STOP_INPUT)


def load_plan(
    plan_path: Path,
    *,
    expected_sha256: str,
    campaign_id: str,
    expected_counts: Mapping[str, int] = EXPECTED_CLASSIFICATION_COUNTS,
) -> list[dict[str, object]]:
    if (
        not plan_path.is_absolute() or not plan_path.is_file()
        or len(expected_sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
    ):
        raise RuntimeError(STOP_INPUT)
    actual_sha = sha256_file(plan_path)
    if actual_sha.lower() != expected_sha256.lower():
        raise RuntimeError(STOP_INPUT)
    _load_digest_manifest(plan_path, actual_sha)
    try:
        rows = [json.loads(line) for line in plan_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(STOP_INPUT) from exc
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(STOP_INPUT)
    expected_total = sum(expected_counts.values())
    row_ids = [row.get("manifest_row_id") for row in rows]
    requested = [row.get("requested_disclosure_no") for row in rows]
    counts = Counter(str(row.get("classification") or "") for row in rows)
    normalized_counts = {name: counts.get(name, 0) for name in STATUS_MAPPING}
    if (
        len(rows) != expected_total
        or len(set(row_ids)) != expected_total or any(not value for value in row_ids)
        or len(set(requested)) != expected_total or any(not value for value in requested)
        or any(row.get("campaign_id") != campaign_id for row in rows)
        or set(counts) - set(STATUS_MAPPING)
        or normalized_counts != dict(expected_counts)
        or any(not isinstance(row.get("expected_identity"), dict) or not isinstance(row.get("actual_identity"), dict) for row in rows)
    ):
        raise RuntimeError(STOP_INPUT)
    return rows


def _desired(row: Mapping[str, object]) -> dict[str, object]:
    classification = str(row["classification"])
    identity, cache, overall, error_code, retryable = STATUS_MAPPING[classification]
    expected = row["expected_identity"]
    actual = row["actual_identity"]
    assert isinstance(expected, dict) and isinstance(actual, dict)
    is_error = error_code is not None
    return {
        "expected_period": expected.get("expected_period"),
        "expected_quarter": expected.get("expected_quarter"),
        "internal_document_id": actual.get("internal_document_id"),
        "zip_sha256": actual.get("zip_sha256"),
        "zip_internal_ticker": actual.get("ticker"),
        "zip_internal_period": actual.get("period"),
        "zip_internal_quarter": actual.get("quarter"),
        "identity_status": identity,
        "cache_status": cache,
        "overall_status": overall,
        "error_code": error_code,
        "error_stage": "identity_plan" if is_error else None,
        "error_message": str(row.get("reason_code") or error_code) if is_error else None,
        "retryable": retryable,
    }


def _read_rows(conn: sqlite3.Connection, campaign_id: str) -> list[dict[str, object]]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(
        "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id",
        (campaign_id,),
    )]


def validate_precondition(
    conn: sqlite3.Connection,
    *,
    campaign_id: str,
    plan_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows = _read_rows(conn, campaign_id)
    plan_ids = {str(row["manifest_row_id"]) for row in plan_rows}
    db_ids = {str(row["manifest_row_id"]) for row in rows}
    campaign_count = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    if (
        get_schema_version(conn) != SCHEMA_VERSION
        or campaign_count != 1 or len(rows) != len(plan_rows) or plan_ids != db_ids
        or any(row["identity_status"] != "UNVERIFIED" for row in rows)
        or any(row["cache_status"] != "UNKNOWN" for row in rows)
        or any(row["overall_status"] != "REGISTERED" for row in rows)
        or any(row["error_code"] != "MISSING_EXPECTED_QUARTER" for row in rows)
        or any(row["registration_status"] != "REGISTERED" for row in rows)
        or any(row["extraction_status"] != "NOT_STARTED" for row in rows)
        or any(row["sqlite_save_status"] != "NOT_STARTED" for row in rows)
        or any(row["canonical_save_status"] != "NOT_STARTED" for row in rows)
        or any(row["supabase_save_status"] != "NOT_STARTED" for row in rows)
        or any(row["started_at"] is not None or row["completed_at"] is not None for row in rows)
    ):
        raise RuntimeError(STOP_PRECONDITION)
    return rows


def _write_audit(output_dir: Path, files: Mapping[str, object]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, value in files.items():
        (output_dir / name).write_bytes(_json_bytes(value))
    digests = {
        name: sha256_file(output_dir / name)
        for name in sorted(files)
    }
    (output_dir / "digests.json").write_bytes(_json_bytes(digests))
    return digests


def _verify(
    conn: sqlite3.Connection,
    *,
    campaign_id: str,
    plan_rows: list[dict[str, object]],
    before_rows: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    after_rows = _read_rows(conn, campaign_id)
    before = {str(row["manifest_row_id"]): row for row in before_rows}
    after = {str(row["manifest_row_id"]): row for row in after_rows}
    plan = {str(row["manifest_row_id"]): row for row in plan_rows}
    missing = sorted(set(plan) - set(after))
    extra = sorted(set(after) - set(plan))
    changed_incorrectly = 0
    unchanged_violations = 0
    for row_id in sorted(set(plan) & set(after)):
        desired = _desired(plan[row_id])
        if any(after[row_id][field] != desired[field] for field in APPLIED_FIELDS):
            changed_incorrectly += 1
        if any(after[row_id][field] != before[row_id][field] for field in UNCHANGED_FIELDS):
            unchanged_violations += 1
    def grouped(column: str) -> dict[str, int]:
        return {
            str(row[0]): int(row[1])
            for row in conn.execute(
                f"SELECT {column}, COUNT(*) FROM campaign_filings WHERE campaign_id=? GROUP BY {column}",
                (campaign_id,),
            )
        }
    verification = {
        "campaign_filings": len(after_rows),
        "identity_status": grouped("identity_status"),
        "cache_status": grouped("cache_status"),
        "overall_status": grouped("overall_status"),
        "retryable": grouped("retryable"),
        "expected_quarter_reflected": conn.execute(
            "SELECT COUNT(*) FROM campaign_filings WHERE campaign_id=? AND NULLIF(expected_quarter,'') IS NOT NULL",
            (campaign_id,),
        ).fetchone()[0],
        "internal_document_id_reflected": conn.execute(
            "SELECT COUNT(*) FROM campaign_filings WHERE campaign_id=? AND NULLIF(internal_document_id,'') IS NOT NULL",
            (campaign_id,),
        ).fetchone()[0],
        "zip_sha256_reflected": conn.execute(
            "SELECT COUNT(*) FROM campaign_filings WHERE campaign_id=? AND NULLIF(zip_sha256,'') IS NOT NULL",
            (campaign_id,),
        ).fetchone()[0],
        "schema_version": get_schema_version(conn),
        "pk_duplicates": conn.execute(
            "SELECT COUNT(*) FROM (SELECT campaign_id,manifest_row_id,COUNT(*) n FROM campaign_filings GROUP BY campaign_id,manifest_row_id HAVING n>1)"
        ).fetchone()[0],
        "foreign_key_check": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
    }
    semantic = {
        "missing": len(missing), "extra": len(extra),
        "changed_incorrectly": changed_incorrectly,
        "unchanged_status_violations": unchanged_violations,
    }
    return verification, semantic


def apply_identity_plan(
    *,
    campaign_db: Path,
    campaign_id: str,
    plan_results: Path,
    plan_results_sha256: str,
    output_dir: Path,
    apply: bool,
    repo_root: Path,
    expected_counts: Mapping[str, int] = EXPECTED_CLASSIFICATION_COUNTS,
) -> dict[str, object]:
    validate_temp_path(campaign_db, repo_root)
    validate_temp_path(output_dir, repo_root)
    plan_rows = load_plan(
        plan_results, expected_sha256=plan_results_sha256,
        campaign_id=campaign_id, expected_counts=expected_counts,
    )
    if not apply:
        return {"apply": False, "db_changed": False, "input_count": len(plan_rows)}
    if not campaign_db.is_file() or output_dir.exists():
        raise RuntimeError(STOP_PRECONDITION)

    before_sha = sha256_file(campaign_db)
    conn = sqlite3.connect(str(campaign_db), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    before_rows: list[dict[str, object]] = []
    started = _now()
    updated = 0
    try:
        before_rows = validate_precondition(conn, campaign_id=campaign_id, plan_rows=plan_rows)
        conn.execute("BEGIN IMMEDIATE")
        stamp = _now()
        sql = (
            "UPDATE campaign_filings SET expected_period=?,expected_quarter=?,"
            "internal_document_id=?,zip_sha256=?,zip_internal_ticker=?,zip_internal_period=?,"
            "zip_internal_quarter=?,identity_status=?,cache_status=?,overall_status=?,"
            "error_code=?,error_stage=?,error_message=?,retryable=?,updated_at=? "
            "WHERE campaign_id=? AND manifest_row_id=?"
        )
        for plan_row in plan_rows:
            desired = _desired(plan_row)
            values = [desired[field] for field in APPLIED_FIELDS]
            cursor = conn.execute(
                sql,
                [*values, stamp, campaign_id, plan_row["manifest_row_id"]],
            )
            if cursor.rowcount != 1:
                raise RuntimeError(STOP_TEMP_FAILED)
            updated += 1
        if updated != len(plan_rows):
            raise RuntimeError(STOP_TEMP_FAILED)
        transaction_verification, transaction_semantic = _verify(
            conn, campaign_id=campaign_id, plan_rows=plan_rows,
            before_rows=before_rows,
        )
        if (
            transaction_semantic != {
                "missing": 0, "extra": 0, "changed_incorrectly": 0,
                "unchanged_status_violations": 0,
            }
            or transaction_verification["campaign_filings"] != len(plan_rows)
            or transaction_verification["pk_duplicates"]
            or transaction_verification["foreign_key_check"]
            or transaction_verification["integrity_check"] != "ok"
            or transaction_verification["schema_version"] != SCHEMA_VERSION
        ):
            raise RuntimeError(STOP_SEMANTIC)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        after_sha = sha256_file(campaign_db)
        failure = {
            "apply": True, "started_at": started, "failed_at": _now(),
            "updated_before_rollback": updated, "error": str(exc),
            "db_sha256_before": before_sha, "db_sha256_after_rollback": after_sha,
        }
        _write_audit(output_dir, {"identity-apply-failure.json": failure})
        if after_sha != before_sha:
            raise RuntimeError(STOP_PARTIAL) from exc
        raise RuntimeError(str(exc) if isinstance(exc, RuntimeError) else STOP_TEMP_FAILED) from exc

    verification, semantic = _verify(
        conn, campaign_id=campaign_id, plan_rows=plan_rows, before_rows=before_rows,
    )
    conn.close()
    if (
        semantic != {"missing": 0, "extra": 0, "changed_incorrectly": 0, "unchanged_status_violations": 0}
        or verification["campaign_filings"] != len(plan_rows)
        or verification["pk_duplicates"] or verification["foreign_key_check"]
        or verification["integrity_check"] != "ok"
        or verification["schema_version"] != SCHEMA_VERSION
    ):
        raise RuntimeError(STOP_SEMANTIC)
    finished = _now()
    after_sha = sha256_file(campaign_db)
    summary = {
        "apply": True, "input_count": len(plan_rows), "updated": updated,
        "started_at": started, "finished_at": finished,
        "db_sha256_before": before_sha, "db_sha256_after": after_sha,
    }
    digests = _write_audit(output_dir, {
        "identity-apply-execution.json": summary,
        "identity-apply-summary.json": {
            "classification_counts": dict(sorted(Counter(str(row["classification"]) for row in plan_rows).items())),
            **summary,
        },
        "database-verification.json": verification,
        "semantic-diff.json": semantic,
    })
    return {**summary, **verification, "semantic": semantic, "output_dir": str(output_dir), "digests": digests}
