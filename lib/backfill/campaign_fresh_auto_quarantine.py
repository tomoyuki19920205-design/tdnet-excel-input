"""Fail-closed evidence contract for known Fresh Download defects.

This module never opens the campaign database for writing.  It only creates
canonical, secret-free evidence below an already-created child output and
validates that evidence for the parent loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Mapping


ALLOWED_CONTRACTS = frozenset({
    ("TD_FILES_DISCNO_NOT_FOUND", "STAGE_A", "JQUANTS_TD_FILES", 404),
    ("ZIP_INTERNAL_IDENTITY_CONFLICT", "ZIP_IDENTITY", "JQUANTS_TD_FILES", 200),
})
SECRET_KEY_RE = re.compile(r"(?:token|secret|authorization|cookie|signed_url|api[_-]?key)", re.I)
URL_RE = re.compile(r"https?://", re.I)


class FreshAutoQuarantineStop(RuntimeError):
    """Evidence or automatic-quarantine contract violation."""


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def failure_http_status(failure: Mapping[str, object]) -> int | None:
    value = failure.get("td_files_http_status")
    if value is None:
        value = failure.get("http_status")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def failure_contract(failure: Mapping[str, object]) -> tuple[str, str, str, int] | None:
    status = failure_http_status(failure)
    if status is None:
        return None
    contract = (
        str(failure.get("failure_code") or ""),
        str(failure.get("failure_stage") or ""),
        "JQUANTS_TD_FILES",
        status,
    )
    return contract if contract in ALLOWED_CONTRACTS else None


def _safe_attempts(failure: Mapping[str, object]) -> list[dict[str, object]]:
    safe_keys = {
        "stage", "http_status", "reason_phrase", "result_code", "content_type",
        "bytes_received", "zip_sha256", "identity_conflict_fields",
        "xbrl_candidate_count", "exception_type",
    }
    result: list[dict[str, object]] = []
    for raw in failure.get("download_attempts", []) or []:
        if not isinstance(raw, Mapping):
            continue
        item = {key: raw[key] for key in sorted(safe_keys) if key in raw}
        candidates = raw.get("identity_candidates")
        if isinstance(candidates, list):
            allowed = {"path", "format", "ticker", "period", "quarter", "document_type", "internal_document_id"}
            item["identity_candidates"] = [
                {key: candidate.get(key) for key in sorted(allowed) if key in candidate}
                for candidate in candidates if isinstance(candidate, Mapping)
            ]
        result.append(item)
    return result


def _assert_secret_free(value: object, key: str = "") -> None:
    if SECRET_KEY_RE.search(key):
        raise FreshAutoQuarantineStop("STOP_V4_FRESH_AUTO_QUARANTINE_SECRET_MATERIAL")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _assert_secret_free(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            _assert_secret_free(child, key)
    elif isinstance(value, str) and URL_RE.search(value):
        raise FreshAutoQuarantineStop("STOP_V4_FRESH_AUTO_QUARANTINE_SECRET_MATERIAL")


def write_known_failure_evidence(
    *, output_dir: Path, run_id: str, campaign_id: str,
    row: Mapping[str, object], failure: Mapping[str, object],
    journal_path: Path, manifest_path: Path, manifest_sha256: str,
    campaign_db_start_sha256: str,
) -> dict[str, object] | None:
    contract = failure_contract(failure)
    if contract is None:
        return None
    row_id = str(row["manifest_row_id"])
    payload = {
        "schema_version": "1", "run_id": run_id, "campaign_id": campaign_id,
        "manifest_row_id": row_id,
        "requested_disclosure_no": str(row["requested_disclosure_no"]),
        "failure": {
            "failure_code": contract[0], "failure_stage": contract[1],
            "source_route": contract[2], "http_status": contract[3],
            "retryable": bool(failure.get("retryable", False)),
        },
        "campaign_db_start_sha256": campaign_db_start_sha256.lower(),
        "manifest_path": str(manifest_path), "manifest_sha256": manifest_sha256.lower(),
        "journal_path": str(journal_path),
        "fresh_db_start_state": str(row.get("fresh_status")),
        "expected_attempt_count": int(row.get("attempt_count") or 0),
        "artifact_start_state": {
            "zip_sha256": row.get("artifact_zip_sha256"),
            "internal_document_id": row.get("artifact_internal_document_id"),
            "completed_at": row.get("completed_at"),
        },
        "attempt_evidence": _safe_attempts(failure),
        "provenance_published": False,
    }
    _assert_secret_free(payload)
    evidence_dir = output_dir / "failure-evidence"
    evidence_dir.mkdir(parents=False, exist_ok=False)
    evidence_file = evidence_dir / f"row-{row_id}.json"
    _atomic_json(evidence_file, payload)
    files = [{
        "path": evidence_file.relative_to(evidence_dir).as_posix(),
        "size": evidence_file.stat().st_size,
        "sha256": sha256_file(evidence_file),
    }]
    tree_sha = hashlib.sha256(_json_bytes(files)).hexdigest()
    _atomic_json(evidence_dir / "digests.json", {
        "files": files, "tree_digest_excluding_digests_json": tree_sha,
    })
    return {
        "path": str(evidence_dir), "tree_sha256": tree_sha,
        "file": str(evidence_file), "file_sha256": files[0]["sha256"],
        "contract": list(contract),
    }


def verify_evidence_tree(path: Path, expected_sha256: str) -> dict[str, object]:
    digest_path = path / "digests.json"
    if not path.is_dir() or not digest_path.is_file():
        raise FreshAutoQuarantineStop("STOP_V4_FRESH_AUTO_QUARANTINE_EVIDENCE_INVALID")
    document = json.loads(digest_path.read_text(encoding="utf-8"))
    recorded = str(document.get("tree_digest_excluding_digests_json") or "")
    files = document.get("files")
    if not isinstance(files, list) or len(files) != 1 or recorded != expected_sha256:
        raise FreshAutoQuarantineStop("STOP_V4_FRESH_AUTO_QUARANTINE_EVIDENCE_INVALID")
    normalized = []
    for item in files:
        candidate = path / str(item.get("path"))
        current = {"path": candidate.relative_to(path).as_posix(), "size": candidate.stat().st_size, "sha256": sha256_file(candidate)}
        if current != item:
            raise FreshAutoQuarantineStop("STOP_V4_FRESH_AUTO_QUARANTINE_EVIDENCE_INVALID")
        normalized.append(current)
    if hashlib.sha256(_json_bytes(normalized)).hexdigest() != recorded:
        raise FreshAutoQuarantineStop("STOP_V4_FRESH_AUTO_QUARANTINE_EVIDENCE_INVALID")
    payload = json.loads((path / normalized[0]["path"]).read_text(encoding="utf-8"))
    _assert_secret_free(payload)
    return payload


def prospective_limit_reason(
    *, auto_quarantines: int, consecutive: int, completed_in_run: int,
    max_auto_quarantines: int, max_consecutive_quarantines: int,
    max_quarantine_rate_percent: int,
) -> str | None:
    next_total = auto_quarantines + 1
    next_consecutive = consecutive + 1
    if next_total > max_auto_quarantines:
        return "MAX_AUTO_QUARANTINES"
    if next_consecutive > max_consecutive_quarantines:
        return "MAX_CONSECUTIVE_QUARANTINES"
    resolved = completed_in_run + next_total
    if resolved >= 100 and (next_total * 100.0 / resolved) > max_quarantine_rate_percent:
        return "MAX_QUARANTINE_RATE"
    return None
