"""Read-only fresh-download planning for every filing in a V4 campaign."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping


STOP_CAMPAIGN_CHANGED = "STOP_V4_CAMPAIGN_FRESH_DOWNLOAD_CAMPAIGN_CHANGED"
STOP_URL_UNRESOLVED = "STOP_V4_CAMPAIGN_FRESH_DOWNLOAD_URL_UNRESOLVED"
STOP_PATH_CONFLICT = "STOP_V4_CAMPAIGN_FRESH_DOWNLOAD_PATH_CONFLICT"
STOP_COUNT = "STOP_V4_CAMPAIGN_FRESH_DOWNLOAD_COUNT_MISMATCH"
STOP_OUTPUT = "STOP_V4_CAMPAIGN_FRESH_DOWNLOAD_UNSAFE_OUTPUT"

CLASSIFICATIONS = (
    "STANDARD_FRESH_DOWNLOAD",
    "QUARANTINE_FRESH_RECHECK",
    "INVALID_OR_MISSING_DOWNLOAD_URL",
    "DUPLICATE_DOWNLOAD_URL",
    "OTHER_UNRESOLVED",
)
OFFICIAL_HOSTS = frozenset({"www.release.tdnet.info", "release.tdnet.info"})
_ZIP_PATH_RE = re.compile(r"^/inbs/0812(?P<requested>20\d{12})\.zip$", re.IGNORECASE)
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
MAX_WINDOWS_PATH = 259

RATE_LIMIT_CONTRACT = {
    "workers": 1,
    "minimum_interval_seconds": 1,
    "http_timeout_seconds": 60,
    "maximum_attempts": 3,
    "retry_statuses": [429, 503],
    "backoff": "exponential",
    "not_found_policy": "no_immediate_retry_or_low_frequency_recheck_queue",
    "maximum_chunk_size": 100,
    "chunk_boundary": "stop_and_audit",
    "maximum_consecutive_failures": 10,
}

RESUME_CONTRACT = {
    "statuses": [
        "DOWNLOAD_NOT_STARTED", "DOWNLOAD_IN_PROGRESS", "DOWNLOAD_SUCCEEDED",
        "DOWNLOAD_FAILED_RETRYABLE", "DOWNLOAD_FAILED_PERMANENT",
        "IDENTITY_VERIFIED", "IDENTITY_MISMATCH", "QUARANTINED",
    ],
    "valid_zip_and_provenance": "skip_download_after_sha_and_identity_revalidation",
    "zip_without_provenance": "incomplete",
    "invalid_provenance": "fail_closed_no_overwrite",
    "temporary_files": "never_formal_output",
    "ready_gate": "zip_sha_and_identity_verified_with_valid_provenance",
    "quarantine_release": "separate_review_required",
    "final_files": ["xbrl.zip", "provenance.json"],
    "temporary_file_patterns": [".xbrl.zip.<uuid>.tmp", ".provenance.json.<uuid>.tmp"],
    "provenance_publish_order": "zip_first_provenance_last",
    "download_tool_version": "v1",
    "provenance_required_fields": [
        "campaign_id", "manifest_row_id", "requested_disclosure_no",
        "company_code", "normalized_company_code", "source_url",
        "normalized_xbrl_url", "downloaded_at", "http_status",
        "download_attempt", "zip_sha256", "zip_size", "internal_document_id",
        "zip_internal_ticker", "zip_internal_period", "zip_internal_quarter",
        "document_type", "identity_status", "code_sha", "run_id",
        "download_tool_version", "error_code", "error_message",
    ],
}


class FreshDownloadPlanStop(RuntimeError):
    """Fail-closed structured stop."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, object]]) -> bytes:
    return b"".join(_json_bytes(row) for row in rows)


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_output_dir(output_dir: Path, repo_root: Path) -> None:
    if not output_dir.is_absolute() or _is_under(output_dir, repo_root) or output_dir.exists():
        raise FreshDownloadPlanStop(STOP_OUTPUT)


def connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_absolute() or not path.is_file():
        raise FreshDownloadPlanStop(STOP_CAMPAIGN_CHANGED)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_campaign_rows(
    campaign_db: Path,
    *,
    campaign_id: str,
    expected_count: int,
    campaign_db_sha256: str,
) -> list[dict[str, object]]:
    if sha256_file(campaign_db).lower() != campaign_db_sha256.lower():
        raise FreshDownloadPlanStop(STOP_CAMPAIGN_CHANGED)
    conn = connect_read_only(campaign_db)
    try:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id",
            (campaign_id,),
        )]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()
    row_ids = [str(row.get("manifest_row_id") or "") for row in rows]
    if (
        len(rows) != expected_count or len(set(row_ids)) != expected_count
        or any(not value for value in row_ids) or row_ids != sorted(row_ids)
        or integrity != "ok" or foreign_keys != 0
    ):
        raise FreshDownloadPlanStop(STOP_CAMPAIGN_CHANGED)
    return rows


def validate_download_url(value: object, requested_id: object) -> tuple[str, str]:
    url = str(value or "").strip()
    if not url:
        return "", "MISSING_NORMALIZED_XBRL_URL"
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "", "INVALID_URL_PARSE"
    if parsed.scheme.lower() != "https":
        return "", "NON_HTTPS_URL"
    if parsed.hostname is None or parsed.hostname.lower() not in OFFICIAL_HOSTS:
        return "", "NON_OFFICIAL_TDNET_HOST"
    try:
        port = parsed.port
    except ValueError:
        return "", "UNEXPECTED_URL_AUTH_OR_PORT"
    if parsed.username or parsed.password or port not in {None, 443}:
        return "", "UNEXPECTED_URL_AUTH_OR_PORT"
    if parsed.query or parsed.fragment:
        return "", "URL_QUERY_OR_FRAGMENT_PRESENT"
    if "%" in parsed.path:
        return "", "NON_CANONICAL_PERCENT_ENCODING"
    match = _ZIP_PATH_RE.fullmatch(parsed.path)
    if not match:
        return "", "INVALID_TDNET_XBRL_ZIP_PATH"
    if match.group("requested") != str(requested_id or ""):
        return "", "REQUESTED_ID_URL_MISMATCH"
    canonical_key = urllib.parse.urlunsplit(("https", parsed.hostname.lower(), parsed.path.lower(), "", ""))
    return canonical_key, ""


def _reserved_component(path: Path) -> str:
    for component in path.parts:
        stem = component.rstrip(" .").split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            return component
    return ""


def _target_paths(target_cache_root: Path, manifest_row_id: str) -> tuple[Path, Path, Path]:
    directory = Path(os.path.abspath(target_cache_root / manifest_row_id))
    return directory, directory / "xbrl.zip", directory / "provenance.json"


def build_download_plan(
    rows: list[dict[str, object]],
    *,
    campaign_id: str,
    target_cache_root: Path,
) -> tuple[list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    if not target_cache_root.is_absolute():
        raise FreshDownloadPlanStop(STOP_PATH_CONFLICT)
    url_info: dict[str, tuple[str, str]] = {}
    grouped_urls: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        row_id = str(row["manifest_row_id"])
        key, error = validate_download_url(
            row.get("normalized_xbrl_url"), row.get("requested_disclosure_no"),
        )
        url_info[row_id] = (key, error)
        if key:
            grouped_urls[key].append(row_id)
    duplicate_keys = {key for key, ids in grouped_urls.items() if len(ids) > 1}

    plan: list[dict[str, object]] = []
    path_records: list[dict[str, object]] = []
    for row in rows:
        row_id = str(row["manifest_row_id"])
        requested = str(row.get("requested_disclosure_no") or "")
        key, url_error = url_info[row_id]
        retryable = row.get("retryable")
        if url_error:
            classification = "INVALID_OR_MISSING_DOWNLOAD_URL"
            reason = url_error
            download_allowed = auto_ready = quarantine = False
        elif key in duplicate_keys:
            classification = "DUPLICATE_DOWNLOAD_URL"
            reason = "DUPLICATE_NORMALIZED_XBRL_URL"
            download_allowed = True
            auto_ready = False
            quarantine = True
        elif retryable in {1, True}:
            classification = "STANDARD_FRESH_DOWNLOAD"
            reason = "RETRYABLE_FRESH_DOWNLOAD"
            download_allowed = auto_ready = True
            quarantine = False
        elif retryable in {0, False}:
            classification = "QUARANTINE_FRESH_RECHECK"
            reason = str(row.get("error_code") or "NON_RETRYABLE_IDENTITY_RECHECK")
            download_allowed = True
            auto_ready = False
            quarantine = True
        else:
            classification = "OTHER_UNRESOLVED"
            reason = "INVALID_RETRYABLE_VALUE"
            download_allowed = auto_ready = quarantine = False
        if classification not in CLASSIFICATIONS:
            raise FreshDownloadPlanStop(STOP_COUNT)
        target_dir, target_zip, target_provenance = _target_paths(target_cache_root, row_id)
        item = {
            "campaign_id": campaign_id,
            "manifest_row_id": row_id,
            "requested_disclosure_no": requested,
            "company_code": row.get("company_code"),
            "normalized_company_code": row.get("normalized_company_code"),
            "source_url": row.get("source_url"),
            "normalized_xbrl_url": row.get("normalized_xbrl_url"),
            "target_directory": str(target_dir),
            "target_zip_path": str(target_zip),
            "target_provenance_path": str(target_provenance),
            "current_identity_status": row.get("identity_status"),
            "current_cache_status": row.get("cache_status"),
            "current_overall_status": row.get("overall_status"),
            "retryable": bool(retryable) if retryable in {0, 1, False, True} else None,
            "plan_classification": classification,
            "reason_code": reason,
            "download_allowed": download_allowed,
            "auto_ready_allowed": auto_ready,
            "quarantine_release_required": quarantine,
        }
        plan.append(item)
        path_records.append({
            "manifest_row_id": row_id,
            "target_directory": str(target_dir),
            "target_zip_path": str(target_zip),
            "target_provenance_path": str(target_provenance),
            "reserved_component": _reserved_component(target_dir),
            "maximum_path_length": max(len(str(target_zip)), len(str(target_provenance))),
        })

    fields = ("target_directory", "target_zip_path", "target_provenance_path")
    collisions: list[dict[str, object]] = []
    for field in fields:
        groups: dict[str, list[str]] = defaultdict(list)
        for record in path_records:
            groups[str(record[field]).casefold()].append(str(record["manifest_row_id"]))
        collisions.extend(
            {"field": field, "path_key": key, "manifest_row_ids": ids}
            for key, ids in groups.items() if len(ids) > 1
        )
    reserved = [record for record in path_records if record["reserved_component"]]
    too_long = [record for record in path_records if int(record["maximum_path_length"]) > MAX_WINDOWS_PATH]
    audit = {
        "input_count": len(rows),
        "manifest_row_id_unique": len({str(row["manifest_row_id"]) for row in rows}),
        "target_directory_unique_casefold": len({str(record["target_directory"]).casefold() for record in path_records}),
        "target_zip_unique_casefold": len({str(record["target_zip_path"]).casefold() for record in path_records}),
        "target_provenance_unique_casefold": len({str(record["target_provenance_path"]).casefold() for record in path_records}),
        "collisions": collisions,
        "reserved_name_conflicts": reserved,
        "path_length_conflicts": too_long,
        "maximum_observed_path_length": max((int(record["maximum_path_length"]) for record in path_records), default=0),
        "windows_max_path_contract": MAX_WINDOWS_PATH,
    }
    if collisions or reserved or too_long or audit["manifest_row_id_unique"] != len(rows):
        raise FreshDownloadPlanStop(STOP_PATH_CONFLICT)
    duplicates = [
        {"normalized_xbrl_url_key": key, "row_count": len(ids), "manifest_row_ids": ids}
        for key, ids in sorted(grouped_urls.items()) if len(ids) > 1
    ]
    if len(plan) != len(rows) or sum(Counter(str(row["plan_classification"]) for row in plan).values()) != len(rows):
        raise FreshDownloadPlanStop(STOP_COUNT)
    return plan, audit, duplicates


def write_download_plan(
    *,
    output_dir: Path,
    repo_root: Path,
    rows: list[dict[str, object]],
    path_audit: dict[str, object],
    duplicate_groups: list[dict[str, object]],
    execution: dict[str, object],
) -> dict[str, object]:
    validate_output_dir(output_dir, repo_root)
    if any(row.get("plan_classification") not in CLASSIFICATIONS for row in rows):
        raise FreshDownloadPlanStop(STOP_COUNT)
    counts = Counter(str(row["plan_classification"]) for row in rows)
    standard = [row for row in rows if row["plan_classification"] == "STANDARD_FRESH_DOWNLOAD"]
    quarantine = [row for row in rows if row["plan_classification"] == "QUARANTINE_FRESH_RECHECK"]
    invalid = [row for row in rows if row["plan_classification"] == "INVALID_OR_MISSING_DOWNLOAD_URL"]
    files: dict[str, bytes] = {
        f"download-plan-{len(rows)}.jsonl": _jsonl_bytes(rows),
        f"standard-download-{len(standard)}.jsonl": _jsonl_bytes(standard),
        f"quarantine-recheck-{len(quarantine)}.jsonl": _jsonl_bytes(quarantine),
        "invalid-or-missing-url.jsonl": _jsonl_bytes(invalid),
        "duplicate-url-groups.json": _json_bytes(duplicate_groups),
        "path-collision-audit.json": _json_bytes(path_audit),
        "rate-limit-contract.json": _json_bytes(RATE_LIMIT_CONTRACT),
        "resume-contract.json": _json_bytes(RESUME_CONTRACT),
    }
    summary = {
        "input_count": len(rows), "output_count": len(rows),
        "classification_counts": {name: counts.get(name, 0) for name in CLASSIFICATIONS},
        "duplicate_url_groups": len(duplicate_groups),
        "duplicate_url_rows": sum(int(group["row_count"]) for group in duplicate_groups),
        "path_collisions": len(path_audit["collisions"]),
        "network_calls": 0, "db_writes": 0, "cache_writes": 0,
        "zip_accesses": 0, "downloads": 0,
    }
    files["summary.json"] = _json_bytes(summary)
    files["execution.json"] = _json_bytes(execution)
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (output_dir / name).write_bytes(content)
    digests = {name: sha256_file(output_dir / name) for name in sorted(files)}
    (output_dir / "digests.json").write_bytes(_json_bytes(digests))
    return {"summary": summary, "digests": digests, "output_dir": str(output_dir)}
