"""Manifest-scoped, fail-closed fresh ZIP downloader for V4 campaigns.

The downloader reads the production campaign database in SQLite read-only
mode.  Its only writable locations are an explicitly temporary cache root and
an explicitly temporary audit output directory.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

import requests

from lib.backfill.jquants_td_files_adapter import (
    SOURCE_ROUTE as JQUANTS_SOURCE_ROUTE,
    TdFilesAdapterError,
    contains_secret_material,
    download_signed_zip,
    resolve_xbrl_file,
)
from lib.backfill.campaign_fresh_download_plan import validate_download_url
from lib.backfill.campaign_fresh_auto_quarantine import (
    canonical_failure_stage,
    safe_failure_telemetry,
    write_known_failure_evidence,
)
from lib.backfill.campaign_state import (
    FreshDownloadCASFailed,
    FreshStateMigrationConflict,
    SCHEMA_VERSION,
    apply_fresh_download_successes,
    connect_db,
    get_schema_version,
    load_fresh_download_rows,
    table_exists,
)
from src.segment.zip_identity_verifier import (
    PROVENANCE_VERSION,
    TrustedProvenance,
    extract_actual_metadata_from_zip,
    verify_zip_identity,
)

STOP_UNSAFE_PATH = "STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_UNSAFE_PATH"
STOP_INPUT = "STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_INPUT_CHANGED"
STOP_URL = "STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_URL_CONTRACT_VIOLATION"
STOP_HTTP = "STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_HTTP_FAILED"
STOP_IDENTITY = "STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_IDENTITY_MISMATCH"
STOP_TARGET = "STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_TARGET_CONFLICT"
STOP_CONSECUTIVE = "STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_CONSECUTIVE_FAILURES"
STOP_PRODUCTION_GUARD = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_GUARD_FAILED"
STOP_PRODUCTION_PATH = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_PATH_NOT_ALLOWLISTED"
STOP_PRODUCTION_BACKUP = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_BACKUP_FAILED"
STOP_PRODUCTION_RUNTIME = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_RUNTIME_ACTIVE"
STOP_PRODUCTION_DB_CHANGED = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_DB_CHANGED_DURING_RUN"
STOP_PRODUCTION_DB_CAS = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_DB_CAS_FAILED"
STOP_PRODUCTION_DIVERGENCE = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_DB_ARTIFACT_DIVERGENCE"
STOP_PRODUCTION_ARTIFACT = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_ARTIFACT_CONFLICT"
STOP_PRODUCTION_COUNT = "STOP_V4_FRESH_DOWNLOAD_PRODUCTION_COUNT_CONTRACT_INVALID"

PLAN_CLASSES = frozenset({"STANDARD_FRESH_DOWNLOAD", "QUARANTINE_FRESH_RECHECK"})
PRODUCTION_READY_IDENTITY_VERDICTS = frozenset({
    "exact_document_id_match",
    "official_linked_xbrl_match",
    "official_linked_xbrl_match_without_internal_id",
})
WITHOUT_INTERNAL_ID_VERDICT = "official_linked_xbrl_match_without_internal_id"
WITHOUT_INTERNAL_ID_STATUS = "absent_in_artifact"
WITHOUT_INTERNAL_ID_LINKAGE = "jquants_td_files_exact_discno"
WITHOUT_INTERNAL_ID_VALUE_SOURCES = {
    "ticker": "xbrli_identifier_sicc", "period": "dei",
    "quarter": "dei", "document_type": "dei",
    "internal_document_id": WITHOUT_INTERNAL_ID_STATUS,
}
OFFICIAL_HOSTS = frozenset({"www.release.tdnet.info", "release.tdnet.info"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
TOOL_VERSION = "1"
USER_AGENT = "tdnet-excel-input/v4-campaign-fresh-downloader/1"
MAX_REDIRECTS = 5
PROVENANCE_SCHEMA_VERSION = "1"
PRODUCTION_MAX_COUNT = 100
PRODUCTION_OUTPUT_RE = re.compile(r"^v4-campaign-production-download-\d{8}-\d{6}$")

_RUNTIME_MODULES = frozenset({
    "tools.backfill_campaign_fresh_download_loop",
    "tools.backfill_campaign_fresh_download",
    "tools.scheduler_nightly",
    "tools.scheduler_realtime",
    "tools.backfill_segments_tdnet",
    "tools.sync_segments",
    "tools.backfill_xbrl_to_canonical",
    "tools.backfill_canonical_segments",
    "tools.backfill_canonical_financials",
    "tools.rebuild_canonical_financials",
    "tools.filings_process",
    "tools.filings_ingest",
    "lib.pipeline.canonical_sync",
})
_RUNTIME_SCRIPT_NAMES = frozenset({
    "scheduler_nightly.py",
    "scheduler_realtime.py",
    "backfill_segments_tdnet.py",
    "sync_segments.py",
    "backfill_xbrl_to_canonical.py",
    "backfill_canonical_segments.py",
    "backfill_canonical_financials.py",
    "rebuild_canonical_financials.py",
    "filings_process.py",
    "filings_ingest.py",
})
_PYTHON_EXECUTABLE_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?$", re.IGNORECASE)
_LAUNCHER_EXECUTABLES = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe"})


@dataclass(frozen=True)
class ProductionEnvironment:
    campaign_db: Path
    cache_root: Path
    output_parent: Path = Path(r"C:\tmp")
    output_pattern: re.Pattern[str] = PRODUCTION_OUTPUT_RE


def default_production_environment(repo_root: Path, campaign_id: str) -> ProductionEnvironment:
    return ProductionEnvironment(
        campaign_db=repo_root / "data" / "backfill_campaign_v4.db",
        cache_root=repo_root / "data" / "v4_campaign_cache" / campaign_id,
    )


class FreshDownloaderStop(RuntimeError):
    """Structured downloader stop."""


class DownloadFailure(FreshDownloaderStop):
    """A row-scoped failure with secret-free HTTP diagnostic evidence."""

    def __init__(
        self, stop_code: str, *, failure_code: str, failure_stage: str,
        attempts: list[dict[str, object]], retryable: bool,
    ) -> None:
        super().__init__(stop_code)
        self.failure_code = failure_code
        self.failure_stage = failure_stage
        self.attempts = attempts
        self.retryable = retryable

    def result(self, row: Mapping[str, object]) -> dict[str, object]:
        last = self.attempts[-1] if self.attempts else {}
        stage_a = next(
            (attempt for attempt in reversed(self.attempts) if attempt.get("stage") == "TD_FILES"),
            {},
        )
        stage_b = next(
            (attempt for attempt in reversed(self.attempts) if attempt.get("stage") == "SIGNED_URL"),
            {},
        )
        return {
            "manifest_row_id": row["manifest_row_id"],
            "requested_disclosure_no": row["requested_disclosure_no"],
            "plan_classification": row["plan_classification"],
            "status": "FAILED", "error": str(self),
            "failure_code": self.failure_code,
            "failure_stage": self.failure_stage,
            "source_route": JQUANTS_SOURCE_ROUTE,
            "retryable": self.retryable,
            "attempt_count": len(self.attempts),
            "requested_url": last.get("requested_url"),
            "http_status": last.get("http_status"),
            "reason_phrase": last.get("reason_phrase"),
            "final_url": last.get("final_url"),
            "redirect_history": last.get("redirect_history", []),
            "response_headers": last.get("response_headers", {}),
            "content_type": last.get("content_type"),
            "content_length_header": last.get("content_length_header"),
            "bytes_received": last.get("bytes_received", 0),
            "exception_type": last.get("exception_type"),
            "exception_message": last.get("exception_message"),
            "td_files_http_status": stage_a.get("http_status"),
            "td_files_reason": stage_a.get("reason_phrase"),
            "td_files_elapsed": stage_a.get("elapsed_seconds"),
            "td_files_result_code": stage_a.get("result_code"),
            "xbrl_candidate_count": stage_a.get("xbrl_candidate_count"),
            "signed_url_received": stage_a.get("signed_url_received", False),
            "signed_url_host": stage_a.get("signed_url_host"),
            "signed_url_scheme": stage_a.get("signed_url_scheme"),
            "signed_url_redacted_digest": stage_a.get("signed_url_redacted_digest"),
            "file_http_status": stage_b.get("http_status"),
            "download_attempts": self.attempts,
        }


class _RequestDiagnosticError(RuntimeError):
    def __init__(
        self, failure_code: str, failure_stage: str, *,
        redirect_history: list[dict[str, object]] | None = None,
        http_status: int | None = None, reason_phrase: str | None = None,
        final_url: str | None = None, response_headers: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code
        self.failure_stage = failure_stage
        self.redirect_history = redirect_history or []
        self.http_status = http_status
        self.reason_phrase = reason_phrase
        self.final_url = final_url
        self.response_headers = dict(response_headers or {})


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _now_jst() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_temp_write_path(path: Path, repo_root: Path) -> None:
    if not path.is_absolute() or _is_under(path, repo_root):
        raise FreshDownloaderStop(STOP_UNSAFE_PATH)
    allowed = (Path(r"C:\tmp"), Path(tempfile.gettempdir()))
    if not any(_is_under(path, root) for root in allowed):
        raise FreshDownloaderStop(STOP_UNSAFE_PATH)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _has_unsafe_raw_path(path: Path) -> bool:
    raw = str(path)
    if not path.is_absolute() or ".." in path.parts:
        return True
    drive, tail = os.path.splitdrive(raw)
    return ":" in tail or not drive


def _has_reparse_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        try:
            stat = current.lstat()
        except OSError:
            return True
        attributes = int(getattr(stat, "st_file_attributes", 0))
        if current.is_symlink() or attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            return True
    return False


def _require_exact_path(actual: Path, expected: Path) -> None:
    if _has_unsafe_raw_path(actual) or _normalized_path(actual) != _normalized_path(expected):
        raise FreshDownloaderStop(STOP_PRODUCTION_PATH)
    if _has_reparse_component(actual):
        raise FreshDownloaderStop(STOP_PRODUCTION_PATH)


def validate_production_paths(
    *, campaign_db: Path, cache_root: Path, output_dir: Path,
    manifest_list: Path, download_plan: Path, environment: ProductionEnvironment,
    repo_root: Path,
) -> None:
    _require_exact_path(campaign_db, environment.campaign_db)
    _require_exact_path(cache_root, environment.cache_root)
    if (
        _has_unsafe_raw_path(output_dir)
        or _normalized_path(output_dir.parent) != _normalized_path(environment.output_parent)
        or environment.output_pattern.fullmatch(output_dir.name) is None
        or _has_reparse_component(output_dir)
    ):
        raise FreshDownloaderStop(STOP_PRODUCTION_PATH)
    for input_path in (manifest_list, download_plan):
        if _has_unsafe_raw_path(input_path) or not input_path.is_file() or _is_under(input_path, repo_root):
            raise FreshDownloaderStop(STOP_PRODUCTION_PATH)
        if _has_reparse_component(input_path):
            raise FreshDownloaderStop(STOP_PRODUCTION_PATH)
    forbidden = (
        repo_root, repo_root / "data", repo_root / "data" / "tdnet_cache",
        repo_root / "data" / "xbrl_archive",
    )
    if any(_normalized_path(cache_root) == _normalized_path(path) for path in forbidden):
        raise FreshDownloaderStop(STOP_PRODUCTION_PATH)


def _validate_manifest_row_id(value: object) -> str:
    row_id = str(value or "")
    if not row_id or row_id in {".", ".."} or Path(row_id).name != row_id or any(c in row_id for c in "\\/:*?\"<>|"):
        raise FreshDownloaderStop(STOP_INPUT)
    return row_id


def _target_paths(cache_root: Path, campaign_id: str, row_id: str) -> tuple[Path, Path, Path]:
    directory = cache_root / "cache" / campaign_id / _validate_manifest_row_id(row_id)
    return directory, directory / "xbrl.zip", directory / "provenance.json"


def _production_target_paths(cache_root: Path, campaign_id: str, row_id: str) -> tuple[Path, Path, Path]:
    if cache_root.name != campaign_id:
        raise FreshDownloaderStop(STOP_PRODUCTION_PATH)
    directory = cache_root / _validate_manifest_row_id(row_id)
    if _has_reparse_component(directory):
        raise FreshDownloaderStop(STOP_PRODUCTION_PATH)
    return directory, directory / "xbrl.zip", directory / "provenance.json"


def _validate_http_url(url: str, requested_id: str) -> str:
    try:
        normalized, reason = validate_download_url(url, requested_id)
    except Exception as exc:
        raise FreshDownloaderStop(STOP_URL) from exc
    if not normalized or reason:
        raise FreshDownloaderStop(STOP_URL)
    parts = urlsplit(normalized)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise FreshDownloaderStop(STOP_URL)
    return normalized


def connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_absolute() or not path.is_file():
        raise FreshDownloaderStop(STOP_INPUT)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_manifest_list(path: Path, campaign_id: str) -> tuple[list[dict[str, object]], str]:
    if not path.is_absolute() or not path.is_file():
        raise FreshDownloaderStop(STOP_INPUT)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FreshDownloaderStop(STOP_INPUT) from exc
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("campaign_id") != campaign_id or not isinstance(rows, list) or not rows:
        raise FreshDownloaderStop(STOP_INPUT)
    result: list[dict[str, object]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise FreshDownloaderStop(STOP_INPUT)
        row_id = _validate_manifest_row_id(raw.get("manifest_row_id"))
        requested = str(raw.get("requested_disclosure_no") or "")
        classification = str(raw.get("plan_classification") or "")
        if not requested or classification not in PLAN_CLASSES:
            raise FreshDownloaderStop(STOP_INPUT)
        result.append({"manifest_row_id": row_id, "requested_disclosure_no": requested, "plan_classification": classification})
    if len({r["manifest_row_id"] for r in result}) != len(result) or len({r["requested_disclosure_no"] for r in result}) != len(result):
        raise FreshDownloaderStop(STOP_INPUT)
    if [r["manifest_row_id"] for r in result] != sorted(str(r["manifest_row_id"]) for r in result):
        raise FreshDownloaderStop(STOP_INPUT)
    return result, sha256_file(path)


def load_selected_plan(path: Path, selected: list[dict[str, object]], campaign_id: str) -> tuple[list[dict[str, object]], str]:
    if not path.is_absolute() or not path.is_file():
        raise FreshDownloaderStop(STOP_INPUT)
    wanted = {str(row["manifest_row_id"]): row for row in selected}
    found: dict[str, dict[str, object]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FreshDownloaderStop(STOP_INPUT) from exc
            row_id = str(row.get("manifest_row_id") or "") if isinstance(row, dict) else ""
            if row_id not in wanted:
                continue
            if (
                row_id in found or row.get("campaign_id") != campaign_id
                or row.get("requested_disclosure_no") != wanted[row_id]["requested_disclosure_no"]
                or row.get("plan_classification") != wanted[row_id]["plan_classification"]
                or row.get("download_allowed") is not True
            ):
                raise FreshDownloaderStop(STOP_INPUT)
            found[row_id] = dict(row)
    if set(found) != set(wanted):
        raise FreshDownloaderStop(STOP_INPUT)
    rows = [found[str(row["manifest_row_id"])] for row in selected]
    return rows, sha256_file(path)


def load_campaign_rows(path: Path, campaign_id: str, plan_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    conn = connect_read_only(path)
    try:
        row_ids = [str(row["manifest_row_id"]) for row in plan_rows]
        placeholders = ",".join("?" for _ in row_ids)
        rows = conn.execute(
            f"SELECT * FROM campaign_filings WHERE campaign_id=? AND manifest_row_id IN ({placeholders})",
            [campaign_id, *row_ids],
        ).fetchall()
        by_id = {str(row["manifest_row_id"]): dict(row) for row in rows}
    finally:
        conn.close()
    if len(by_id) != len(plan_rows):
        raise FreshDownloaderStop(STOP_INPUT)
    result: list[dict[str, object]] = []
    for plan_row in plan_rows:
        current = by_id[str(plan_row["manifest_row_id"])]
        if any(current.get(field) != plan_row.get(field) for field in (
            "requested_disclosure_no", "company_code", "normalized_company_code", "source_url", "normalized_xbrl_url"
        )):
            raise FreshDownloaderStop(STOP_INPUT)
        if not current.get("expected_period") or not current.get("expected_quarter"):
            raise FreshDownloaderStop(STOP_INPUT)
        result.append({**plan_row, **current})
    return result


def load_production_rows(
    path: Path, campaign_id: str, plan_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Load immutable filing metadata plus the dedicated Fresh state."""
    filing_rows = load_campaign_rows(path, campaign_id, plan_rows)
    conn = connect_read_only(path)
    try:
        if get_schema_version(conn) != SCHEMA_VERSION or not table_exists(
            conn, "campaign_fresh_downloads"
        ):
            raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
        fresh_rows = load_fresh_download_rows(
            conn, campaign_id, [str(row["manifest_row_id"]) for row in plan_rows]
        )
    except FreshStateMigrationConflict as exc:
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD) from exc
    finally:
        conn.close()
    fresh_by_id = {str(row["manifest_row_id"]): row for row in fresh_rows}
    result: list[dict[str, object]] = []
    for filing in filing_rows:
        fresh = fresh_by_id[str(filing["manifest_row_id"])]
        if fresh["plan_classification"] != filing["plan_classification"]:
            raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
        result.append({**filing, **fresh})
    return result


def manifest_semantic_sha256(rows: Sequence[Mapping[str, object]]) -> str:
    semantic = [
        {
            "manifest_row_id": str(row["manifest_row_id"]),
            "requested_disclosure_no": str(row["requested_disclosure_no"]),
            "plan_classification": str(row["plan_classification"]),
        }
        for row in rows
    ]
    return hashlib.sha256(_json_bytes(semantic)).hexdigest()


def _copy_file_fsync(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sqlite_checks(path: Path) -> dict[str, object]:
    conn = connect_read_only(path)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()
    return {"integrity_check": integrity, "foreign_key_check": foreign_keys}


def create_verified_backup(campaign_db: Path, output_dir: Path) -> dict[str, object]:
    auxiliaries = [Path(f"{campaign_db}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    if any(path.exists() for path in auxiliaries):
        raise FreshDownloaderStop(STOP_PRODUCTION_BACKUP)
    source_sha = sha256_file(campaign_db)
    source_size = campaign_db.stat().st_size
    source_checks = _sqlite_checks(campaign_db)
    if source_checks != {"integrity_check": "ok", "foreign_key_check": 0}:
        raise FreshDownloaderStop(STOP_PRODUCTION_BACKUP)
    backup = output_dir / "backup" / "backfill_campaign_v4.before.db"
    if backup.exists():
        if backup.stat().st_size != source_size or sha256_file(backup) != source_sha:
            raise FreshDownloaderStop(STOP_PRODUCTION_BACKUP)
    else:
        _copy_file_fsync(campaign_db, backup)
    checks = _sqlite_checks(backup)
    if backup.stat().st_size != source_size or sha256_file(backup) != source_sha or checks != source_checks:
        raise FreshDownloaderStop(STOP_PRODUCTION_BACKUP)
    return {
        "path": str(backup), "size": source_size, "sha256": source_sha,
        **checks, "source_auxiliary_files": [],
    }


def _journal_write(path: Path, journal: Mapping[str, object]) -> None:
    _write_atomic_json(path, journal)


def _journal_update(path: Path, journal: dict[str, object], phase: str, **values: object) -> None:
    journal.update(values)
    journal["current_phase"] = phase
    _journal_write(path, journal)


def _command_token(value: object) -> str:
    return str(value or "").strip().strip("\"'").replace("\\", "/").lower()


def _is_runtime_python_invocation(tokens: Sequence[object]) -> bool:
    """Return true only for a direct Python invocation of a guarded workload."""
    values = [_command_token(token) for token in tokens if str(token or "").strip()]
    if not values or not _PYTHON_EXECUTABLE_RE.fullmatch(Path(values[0]).name):
        return False
    for index, value in enumerate(values[:-1]):
        if value == "-m" and values[index + 1] in _RUNTIME_MODULES:
            return True
    return any(Path(value).name in _RUNTIME_SCRIPT_NAMES for value in values[1:])


def _launcher_payload(tokens: Sequence[object]) -> list[str]:
    values = [str(token or "") for token in tokens]
    for index, value in enumerate(values):
        if value.lower() in {"-command", "/c", "-c"}:
            payload = " ".join(values[index + 1:]).strip().strip("\"'")
            if not payload:
                return []
            # A launcher payload is only trusted when it begins with an actual
            # Python command (or PowerShell's explicit call operator).  This
            # deliberately excludes Get-CimInstance/rg/logging expressions
            # that merely contain a guarded module name as text.
            return re.findall(r'''(?:[^\s"']+|"[^"]*"|'[^']*')+''', payload)
    return []


def _is_related_production_command(cmdline: Sequence[object]) -> bool:
    """Classify real guarded workloads without matching monitor text literals."""
    tokens = [str(token or "") for token in cmdline]
    if not tokens:
        return False
    if _is_runtime_python_invocation(tokens):
        return True
    executable = _command_token(tokens[0])
    if Path(executable).name not in _LAUNCHER_EXECUTABLES:
        return False
    payload = _launcher_payload(tokens)
    if payload and _command_token(payload[0]) == "&":
        payload = payload[1:]
    return _is_runtime_python_invocation(payload)


def check_production_runtime(repo_root: Path) -> dict[str, object]:
    locks = [
        repo_root / "state" / "locks" / "nightly.lock",
        repo_root / "state" / "locks" / "tdnet_pipeline.lock",
    ]
    lock_root = repo_root / "state" / "locks"
    if lock_root.is_dir():
        locks.extend(
            path for path in lock_root.iterdir()
            if path.is_file() and any(word in path.name.lower() for word in ("backfill", "campaign"))
        )
    existing_locks = sorted({str(path) for path in locks if path.exists()})
    active: list[dict[str, object]] = []
    try:
        import psutil
        process = psutil.Process(os.getpid())
        excluded = {process.pid, *(parent.pid for parent in process.parents())}
        for candidate in psutil.process_iter(("pid", "name", "cmdline")):
            if candidate.info["pid"] in excluded:
                continue
            if _is_related_production_command(candidate.info.get("cmdline") or []):
                active.append({"pid": candidate.info["pid"], "name": candidate.info.get("name")})
    except Exception as exc:
        raise FreshDownloaderStop(STOP_PRODUCTION_RUNTIME) from exc
    evidence = {"checked_at": _now_utc(), "active_processes": active, "locks": existing_locks}
    if active or existing_locks:
        raise FreshDownloaderStop(STOP_PRODUCTION_RUNTIME)
    return evidence


def _campaign_rows_digest(conn: sqlite3.Connection, campaign_id: str, excluded_ids: set[str]) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id", (campaign_id,)
    ):
        current = dict(row)
        if str(current["manifest_row_id"]) in excluded_ids:
            continue
        digest.update(_json_bytes(current))
    return digest.hexdigest()


def _fresh_rows_digest(conn: sqlite3.Connection, campaign_id: str, excluded_ids: set[str]) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT * FROM campaign_fresh_downloads WHERE campaign_id=? ORDER BY manifest_row_id",
        (campaign_id,),
    ):
        current = dict(row)
        if str(current["manifest_row_id"]) in excluded_ids:
            continue
        digest.update(_json_bytes(current))
    return digest.hexdigest()


def _retry_after_seconds(value: object, now: Callable[[], float] = time.time) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(text)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, target.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


_SAFE_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "retry-after", "location", "date", "server",
})


def _safe_response_headers(headers: Mapping[str, object]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in _SAFE_RESPONSE_HEADERS
    }


def _status_failure_code(status: int) -> str:
    if status in {400, 401, 403, 404, 410, 429}:
        return f"HTTP_{status}"
    if 500 <= status <= 599:
        return "HTTP_5XX"
    if 300 <= status <= 399:
        return "HTTP_3XX_REJECTED"
    return "UNKNOWN_HTTP_FAILURE"


def _classify_request_exception(exc: requests.RequestException) -> tuple[str, str, bool]:
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, requests.exceptions.ProxyError):
        return "CONNECT_FAILED", "proxy_connect", True
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS_FAILED", "tls_handshake", False
    if isinstance(exc, requests.Timeout):
        return "TIMEOUT", "request", True
    if any(marker in text for marker in ("nameresolution", "getaddrinfo", "name or service not known", "nodename nor servname")):
        return "DNS_FAILED", "dns_resolution", True
    return "CONNECT_FAILED", "request", True


def _new_attempt(attempt_number: int, requested_url: str) -> tuple[dict[str, object], float]:
    return ({
        "attempt_number": attempt_number,
        "requested_url": requested_url,
        "request_started_at": _now_utc(),
        "request_finished_at": None,
        "elapsed_seconds": None,
        "http_status": None,
        "reason_phrase": None,
        "final_url": None,
        "redirect_history": [],
        "response_headers": {},
        "content_type": None,
        "content_length_header": None,
        "retry_after": None,
        "bytes_received": 0,
        "exception_type": None,
        "exception_message": None,
        "retryable": False,
        "failure_stage": None,
        "failure_code": None,
        "backoff_seconds": None,
    }, time.monotonic())


def _finish_attempt(record: dict[str, object], started: float) -> None:
    record["request_finished_at"] = _now_utc()
    record["elapsed_seconds"] = round(max(0.0, time.monotonic() - started), 6)


def _request_following_safe_redirects(
    session: requests.Session,
    url: str,
    requested_id: str,
    timeout_seconds: float,
    network_counter: list[int],
) -> tuple[requests.Response, list[dict[str, object]], str]:
    try:
        current = _validate_http_url(url, requested_id)
    except FreshDownloaderStop as exc:
        raise _RequestDiagnosticError("URL_PRECHECK_FAILED", "url_precheck") from exc
    redirects: list[dict[str, object]] = []
    for _ in range(MAX_REDIRECTS + 1):
        network_counter[0] += 1
        response = session.get(
            current, headers={"User-Agent": USER_AGENT}, timeout=timeout_seconds,
            stream=True, allow_redirects=False,
        )
        if response.status_code not in REDIRECT_STATUS:
            return response, redirects, current
        location = response.headers.get("Location", "")
        resolved = urljoin(current, location)
        hostname = (urlsplit(resolved).hostname or "").lower()
        hop = {
            "status": response.status_code,
            "from_url": current,
            "location": location,
            "resolved_url": resolved,
            "hostname": hostname,
            "allowed": False,
        }
        try:
            candidate = _validate_http_url(resolved, requested_id)
        except FreshDownloaderStop as exc:
            redirects.append(hop)
            headers = _safe_response_headers(response.headers)
            reason = str(getattr(response, "reason", "") or "")
            response.close()
            raise _RequestDiagnosticError(
                "REDIRECT_OUTSIDE_OFFICIAL_DOMAIN", "redirect_validation",
                redirect_history=redirects, http_status=response.status_code,
                reason_phrase=reason, final_url=current, response_headers=headers,
            ) from exc
        hop["allowed"] = True
        hop["resolved_url"] = candidate
        redirects.append(hop)
        response.close()
        current = candidate
    raise _RequestDiagnosticError(
        "HTTP_3XX_REJECTED", "redirect_limit", redirect_history=redirects,
        final_url=current,
    )


def _write_stream(response: requests.Response, temp_path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with temp_path.open("xb") as stream:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            stream.write(chunk)
            digest.update(chunk)
            total += len(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) != total:
                raise FreshDownloaderStop(STOP_HTTP)
        except ValueError as exc:
            raise FreshDownloaderStop(STOP_HTTP) from exc
    return digest.hexdigest(), total


def _zip_integrity(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise FreshDownloaderStop(STOP_IDENTITY)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FreshDownloaderStop(STOP_IDENTITY) from exc


def _write_atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("xb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        if json.loads(temp_path.read_text(encoding="utf-8")) != dict(payload):
            raise FreshDownloaderStop(STOP_IDENTITY)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def load_provenance(zip_path: Path, provenance_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FreshDownloaderStop(STOP_IDENTITY) from exc
    required = {
        "schema_version", "campaign_id", "manifest_row_id", "requested_disclosure_no",
        "company_code", "normalized_company_code", "source_url", "normalized_xbrl_url",
        "final_url", "downloaded_at", "downloaded_at_utc", "downloaded_at_jst", "http_status",
        "content_type", "content_length", "download_attempts", "zip_sha256", "zip_size",
        "internal_document_id", "zip_internal_ticker", "zip_internal_period",
        "zip_internal_quarter", "document_type", "identity_verdict", "identity_status", "plan_classification",
        "auto_ready_allowed", "quarantine_release_required", "code_sha", "run_id",
        "download_tool_version", "error_code", "error_message",
    }
    if not isinstance(payload, dict) or not required.issubset(payload) or payload.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise FreshDownloaderStop(STOP_IDENTITY)
    if not zip_path.is_file() or sha256_file(zip_path) != payload.get("zip_sha256") or zip_path.stat().st_size != payload.get("zip_size"):
        raise FreshDownloaderStop(STOP_IDENTITY)
    meta = extract_actual_metadata_from_zip(
        str(zip_path), expected_period=str(payload["zip_internal_period"]),
        expected_quarter=str(payload["zip_internal_quarter"]),
    )
    expected_meta = {
        "internal_document_id": str(payload["internal_document_id"] or ""),
        "ticker": str(payload["zip_internal_ticker"]),
        "period": str(payload["zip_internal_period"]),
        "quarter": str(payload["zip_internal_quarter"]),
        "document_type": str(payload["document_type"]),
    }
    if meta != expected_meta:
        raise FreshDownloaderStop(STOP_IDENTITY)
    verdict = payload.get("identity_verdict")
    if payload.get("identity_status") == "DOWNLOAD_IDENTITY_VERIFIED":
        if verdict not in PRODUCTION_READY_IDENTITY_VERDICTS:
            raise FreshDownloaderStop(STOP_IDENTITY)
    if verdict == WITHOUT_INTERNAL_ID_VERDICT:
        if (
            payload.get("internal_document_id") is not None
            or meta["internal_document_id"] != ""
            or payload.get("internal_document_id_status") != WITHOUT_INTERNAL_ID_STATUS
            or payload.get("linkage_basis") != WITHOUT_INTERNAL_ID_LINKAGE
            or payload.get("source_route") != JQUANTS_SOURCE_ROUTE
            or payload.get("td_files_type") != "x"
            or payload.get("td_files_http_status") != 200
            or payload.get("td_files_result_code") != "TD_FILES_OK"
            or payload.get("xbrl_candidate_count") != 1
            or payload.get("signed_url_received") is not True
            or payload.get("file_http_status") != 200
            or payload.get("http_status") != 200
            or re.fullmatch(r"\d{14}", str(payload.get("requested_disclosure_no") or "")) is None
            or payload.get("plan_classification") != "STANDARD_FRESH_DOWNLOAD"
            or payload.get("auto_ready_allowed") is not True
            or payload.get("quarantine_release_required") is not False
            or payload.get("identity_value_sources") != WITHOUT_INTERNAL_ID_VALUE_SOURCES
        ):
            raise FreshDownloaderStop(STOP_IDENTITY)
    return payload


def is_production_ready_identity_result(payload: Mapping[str, object]) -> bool:
    """Return whether formally loaded provenance is eligible for production READY."""
    base_ready = bool(
        payload.get("identity_verdict") in PRODUCTION_READY_IDENTITY_VERDICTS
        and payload.get("identity_status") == "DOWNLOAD_IDENTITY_VERIFIED"
        and payload.get("plan_classification") == "STANDARD_FRESH_DOWNLOAD"
        and payload.get("auto_ready_allowed") is True
        and payload.get("quarantine_release_required") is False
    )
    if not base_ready:
        return False
    if payload.get("identity_verdict") != WITHOUT_INTERNAL_ID_VERDICT:
        return True
    return bool(
        payload.get("internal_document_id") is None
        and payload.get("internal_document_id_status") == WITHOUT_INTERNAL_ID_STATUS
        and payload.get("linkage_basis") == WITHOUT_INTERNAL_ID_LINKAGE
        and payload.get("source_route") == JQUANTS_SOURCE_ROUTE
        and payload.get("td_files_type") == "x"
        and payload.get("td_files_http_status") == 200
        and payload.get("td_files_result_code") == "TD_FILES_OK"
        and payload.get("xbrl_candidate_count") == 1
        and payload.get("signed_url_received") is True
        and payload.get("file_http_status") == 200
        and re.fullmatch(r"\d{14}", str(payload.get("requested_disclosure_no") or ""))
        and payload.get("identity_value_sources") == WITHOUT_INTERNAL_ID_VALUE_SOURCES
    )


def _download_one(
    *, row: Mapping[str, object], cache_root: Path, campaign_id: str,
    session: requests.Session, timeout_seconds: float, max_retries: int,
    sleep: Callable[[float], None], code_sha: str, run_id: str,
    network_counter: list[int],
) -> tuple[dict[str, object], int]:
    row_id = str(row["manifest_row_id"])
    requested = str(row["requested_disclosure_no"])
    classification = str(row["plan_classification"])
    directory, zip_path, provenance_path = _target_paths(cache_root, campaign_id, row_id)
    if zip_path.exists() or provenance_path.exists():
        if zip_path.is_file() and provenance_path.is_file():
            existing = load_provenance(zip_path, provenance_path)
            if existing.get("manifest_row_id") == row_id and existing.get("requested_disclosure_no") == requested:
                return {
                    "manifest_row_id": row_id, "requested_disclosure_no": requested,
                    "plan_classification": classification, "status": "ALREADY_COMPLETE",
                    "network_calls": 0, "provenance": existing,
                }, 0
        raise FreshDownloaderStop(STOP_TARGET)
    directory.mkdir(parents=True, exist_ok=True)
    temp_zip = directory / f".xbrl.zip.{uuid.uuid4().hex}.tmp"
    attempts: list[dict[str, object]] = []
    initial_network_calls = network_counter[0]
    response: requests.Response | None = None
    final_url = ""
    response_headers: dict[str, str] = {}
    content_type = ""
    zip_sha = ""
    zip_size = 0
    try:
        for attempt_number in range(1, max_retries + 1):
            requested_url = str(row["normalized_xbrl_url"])
            record, attempt_started = _new_attempt(attempt_number, requested_url)
            try:
                response, redirects, final_url = _request_following_safe_redirects(
                    session, requested_url, requested, timeout_seconds,
                    network_counter,
                )
                response_headers = _safe_response_headers(response.headers)
                content_type = str(response.headers.get("Content-Type", ""))
                record.update({
                    "http_status": response.status_code,
                    "reason_phrase": str(getattr(response, "reason", "") or ""),
                    "final_url": final_url,
                    "redirect_history": redirects,
                    "response_headers": response_headers,
                    "content_type": content_type or None,
                    "content_length_header": response.headers.get("Content-Length"),
                    "retry_after": response.headers.get("Retry-After"),
                })
                if response.status_code == 200:
                    if "html" in content_type.lower():
                        record.update({
                            "failure_stage": "content_type_validation",
                            "failure_code": "CONTENT_TYPE_REJECTED", "retryable": False,
                        })
                        _finish_attempt(record, attempt_started)
                        attempts.append(record)
                        raise DownloadFailure(
                            STOP_HTTP, failure_code="CONTENT_TYPE_REJECTED",
                            failure_stage="content_type_validation", attempts=attempts,
                            retryable=False,
                        )
                    try:
                        zip_sha, zip_size = _write_stream(response, temp_zip)
                    except requests.RequestException as exc:
                        failure_code, failure_stage, retryable = _classify_request_exception(exc)
                        record.update({
                            "bytes_received": temp_zip.stat().st_size if temp_zip.exists() else 0,
                            "exception_type": type(exc).__name__, "exception_message": str(exc),
                            "failure_stage": "body_stream", "failure_code": "BODY_STREAM_FAILED",
                            "retryable": retryable,
                        })
                        _finish_attempt(record, attempt_started)
                        attempts.append(record)
                        response.close(); response = None
                        if temp_zip.exists():
                            temp_zip.unlink()
                        if attempt_number == max_retries:
                            raise DownloadFailure(
                                STOP_HTTP, failure_code="BODY_STREAM_FAILED",
                                failure_stage="body_stream", attempts=attempts,
                                retryable=retryable,
                            ) from exc
                        delay = float(2 ** (attempt_number - 1))
                        record["backoff_seconds"] = delay
                        sleep(delay)
                        continue
                    except FreshDownloaderStop as exc:
                        record.update({
                            "bytes_received": temp_zip.stat().st_size if temp_zip.exists() else 0,
                            "exception_type": type(exc).__name__, "exception_message": str(exc),
                            "failure_stage": "body_stream", "failure_code": "BODY_STREAM_FAILED",
                            "retryable": False,
                        })
                        _finish_attempt(record, attempt_started)
                        attempts.append(record)
                        raise DownloadFailure(
                            STOP_HTTP, failure_code="BODY_STREAM_FAILED",
                            failure_stage="body_stream", attempts=attempts,
                            retryable=False,
                        ) from exc
                    record.update({"bytes_received": zip_size, "retryable": False})
                    _finish_attempt(record, attempt_started)
                    attempts.append(record)
                    response.close(); response = None
                    break
                retryable = response.status_code in RETRYABLE_STATUS
                failure_code = _status_failure_code(response.status_code)
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                record.update({
                    "failure_stage": "http_status", "failure_code": failure_code,
                    "retryable": retryable,
                })
                response.close()
                response = None
                if not retryable or attempt_number == max_retries:
                    _finish_attempt(record, attempt_started); attempts.append(record)
                    raise DownloadFailure(
                        STOP_HTTP, failure_code=failure_code, failure_stage="http_status",
                        attempts=attempts, retryable=retryable,
                    )
                delay = retry_after if retry_after is not None else float(2 ** (attempt_number - 1))
                record["backoff_seconds"] = delay
                _finish_attempt(record, attempt_started)
                attempts.append(record)
                sleep(delay)
                continue
            except DownloadFailure:
                raise
            except _RequestDiagnosticError as exc:
                record.update({
                    "http_status": exc.http_status, "reason_phrase": exc.reason_phrase,
                    "final_url": exc.final_url, "redirect_history": exc.redirect_history,
                    "response_headers": exc.response_headers,
                    "content_type": exc.response_headers.get("content-type"),
                    "content_length_header": exc.response_headers.get("content-length"),
                    "retry_after": exc.response_headers.get("retry-after"),
                    "failure_stage": exc.failure_stage, "failure_code": exc.failure_code,
                    "retryable": False,
                })
                _finish_attempt(record, attempt_started)
                attempts.append(record)
                raise DownloadFailure(
                    STOP_URL, failure_code=exc.failure_code,
                    failure_stage=exc.failure_stage, attempts=attempts,
                    retryable=False,
                ) from exc
            except requests.RequestException as exc:
                failure_code, failure_stage, retryable = _classify_request_exception(exc)
                record.update({
                    "exception_type": type(exc).__name__, "exception_message": str(exc),
                    "failure_stage": failure_stage, "failure_code": failure_code,
                    "retryable": retryable,
                })
                _finish_attempt(record, attempt_started)
                attempts.append(record)
                if attempt_number == max_retries:
                    raise DownloadFailure(
                        STOP_HTTP, failure_code=failure_code,
                        failure_stage=failure_stage, attempts=attempts,
                        retryable=retryable,
                    ) from exc
                delay = float(2 ** (attempt_number - 1))
                record["backoff_seconds"] = delay
                sleep(delay)
                continue
        if not zip_sha:
            raise DownloadFailure(
                STOP_HTTP, failure_code="UNKNOWN_HTTP_FAILURE",
                failure_stage="request", attempts=attempts, retryable=False,
            )
        try:
            _zip_integrity(temp_zip)
        except FreshDownloaderStop as exc:
            attempts[-1].update({
                "failure_stage": "zip_validation", "failure_code": "ZIP_INVALID",
                "retryable": False,
            })
            raise DownloadFailure(
                STOP_IDENTITY, failure_code="ZIP_INVALID",
                failure_stage="zip_validation", attempts=attempts,
                retryable=False,
            ) from exc
        expected_period = str(row["expected_period"])
        expected_quarter = str(row["expected_quarter"])
        meta = extract_actual_metadata_from_zip(str(temp_zip), expected_period=expected_period, expected_quarter=expected_quarter)
        provenance = TrustedProvenance(
            source="jquants", requested_disclosure_no=requested, requested_file_type="x",
            resolved_by_function="campaign_fresh_downloader", official_request_succeeded=True,
            response_status=200, downloaded_size=zip_size, downloaded_sha256=zip_sha,
            internal_document_id=meta.get("internal_document_id", ""), ticker=meta.get("ticker", ""),
            period=meta.get("period", ""), quarter=meta.get("quarter", ""),
            document_type=meta.get("document_type", ""), resolved_at=_now_utc(),
        )
        verdict = verify_zip_identity(
            str(temp_zip), requested, str(row["normalized_company_code"]),
            expected_period, expected_quarter, provenance,
        )
        readiness = {
            "identity_verdict": verdict.verdict,
            "identity_status": "DOWNLOAD_IDENTITY_VERIFIED" if verdict.passed else "DOWNLOAD_IDENTITY_MISMATCH",
            "plan_classification": classification,
            "auto_ready_allowed": bool(verdict.passed and classification == "STANDARD_FRESH_DOWNLOAD"),
            "quarantine_release_required": classification == "QUARANTINE_FRESH_RECHECK",
        }
        auto_ready = is_production_ready_identity_result(readiness)
        if classification == "STANDARD_FRESH_DOWNLOAD" and not auto_ready:
            conflict = verdict.rejection_reason == "zip_internal_identity_conflict"
            attempts[-1].update({
                "failure_stage": "ZIP_IDENTITY" if conflict else "identity_validation",
                "failure_code": (
                    "ZIP_INTERNAL_IDENTITY_CONFLICT"
                    if conflict else "DOWNLOAD_IDENTITY_MISMATCH"
                ),
                "identity_rejection_reason": verdict.rejection_reason,
                "identity_conflict_fields": verdict.details.get("conflict_fields", []),
                "identity_candidates": [
                    {
                        key: candidate.get(key)
                        for key in (
                            "path", "format", "ticker", "period", "quarter",
                            "document_type", "internal_document_id",
                        )
                    }
                    for candidate in verdict.details.get("candidates", [])
                    if isinstance(candidate, dict)
                ],
                "zip_sha256": verdict.zip_sha256,
                "retryable": False,
            })
            raise DownloadFailure(
                STOP_IDENTITY,
                failure_code=(
                    "ZIP_INTERNAL_IDENTITY_CONFLICT"
                    if conflict else "DOWNLOAD_IDENTITY_MISMATCH"
                ),
                failure_stage="ZIP_IDENTITY" if conflict else "identity_validation",
                attempts=attempts,
                retryable=False,
            )
        if classification == "STANDARD_FRESH_DOWNLOAD":
            identity_status = "DOWNLOAD_IDENTITY_VERIFIED" if verdict.passed else "DOWNLOAD_IDENTITY_MISMATCH"
        else:
            identity_status = "QUARANTINE_RECHECK_MATCH" if verdict.passed else "QUARANTINE_RECHECK_MISMATCH"
        error_code = None if verdict.passed else verdict.rejection_reason
        payload: dict[str, object] = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "identity_provenance_version": PROVENANCE_VERSION,
            "campaign_id": campaign_id, "manifest_row_id": row_id,
            "requested_disclosure_no": requested, "company_code": row["company_code"],
            "normalized_company_code": row["normalized_company_code"],
            "source_url": row["source_url"], "normalized_xbrl_url": row["normalized_xbrl_url"],
            "final_url": final_url, "downloaded_at": _now_utc(),
            "downloaded_at_utc": _now_utc(), "downloaded_at_jst": _now_jst(),
            "http_status": 200, "content_type": content_type,
            "content_length": response_headers.get("content-length", str(zip_size)),
            "download_attempts": attempts, "zip_sha256": zip_sha, "zip_size": zip_size,
            "internal_document_id": meta.get("internal_document_id", ""),
            "zip_internal_ticker": meta.get("ticker", ""),
            "zip_internal_period": meta.get("period", ""),
            "zip_internal_quarter": meta.get("quarter", ""),
            "document_type": meta.get("document_type", ""),
            "identity_status": identity_status,
            "identity_verdict": verdict.verdict, "identity_rejection_reason": verdict.rejection_reason,
            "plan_classification": classification, "auto_ready_allowed": auto_ready,
            "quarantine_release_required": classification == "QUARANTINE_FRESH_RECHECK",
            "code_sha": code_sha, "run_id": run_id, "download_tool_version": TOOL_VERSION,
            "error_code": error_code, "error_message": error_code,
        }
        os.replace(temp_zip, zip_path)
        if sha256_file(zip_path) != zip_sha:
            raise FreshDownloaderStop(STOP_IDENTITY)
        _zip_integrity(zip_path)
        _write_atomic_json(provenance_path, payload)
        loaded = load_provenance(zip_path, provenance_path)
        if loaded != payload:
            raise FreshDownloaderStop(STOP_IDENTITY)
        return {
            "manifest_row_id": row_id, "requested_disclosure_no": requested,
            "plan_classification": classification,
            "status": "READY" if auto_ready else "QUARANTINED",
            "zip_path": str(zip_path), "provenance_path": str(provenance_path),
            "zip_sha256": zip_sha, "identity_status": payload["identity_status"],
            "identity_verdict": payload["identity_verdict"],
            "internal_document_id": payload["internal_document_id"],
            "ticker": payload["zip_internal_ticker"],
            "period": payload["zip_internal_period"], "quarter": payload["zip_internal_quarter"],
            "document_type": payload["document_type"], "attempt_count": len(attempts),
            "network_calls": network_counter[0] - initial_network_calls, "auto_ready": auto_ready,
            "quarantine_release_required": payload["quarantine_release_required"],
            "download_attempts": attempts,
        }, network_counter[0] - initial_network_calls
    finally:
        if response is not None:
            response.close()
        if temp_zip.exists():
            temp_zip.unlink()


def _download_one_jquants(
    *, row: Mapping[str, object], cache_root: Path, campaign_id: str,
    session: requests.Session, timeout_seconds: float, max_retries: int,
    sleep: Callable[[float], None], code_sha: str, run_id: str,
    network_counter: list[int],
    auth_loader: Callable[[], tuple[dict[str, str], dict[str, object]]] | None = None,
    target_path_builder: Callable[[Path, str, str], tuple[Path, Path, Path]] = _target_paths,
) -> tuple[dict[str, object], int]:
    """Download one row through the exact-DiscNo J-Quants TD files route."""
    row_id = str(row["manifest_row_id"])
    requested = str(row["requested_disclosure_no"])
    classification = str(row["plan_classification"])
    directory, zip_path, provenance_path = target_path_builder(cache_root, campaign_id, row_id)
    if zip_path.exists() or provenance_path.exists():
        if zip_path.is_file() and provenance_path.is_file():
            existing = load_provenance(zip_path, provenance_path)
            if (
                existing.get("manifest_row_id") == row_id
                and existing.get("requested_disclosure_no") == requested
                and existing.get("source_route") == JQUANTS_SOURCE_ROUTE
            ):
                return {
                    "manifest_row_id": row_id,
                    "requested_disclosure_no": requested,
                    "plan_classification": classification,
                    "status": "ALREADY_COMPLETE",
                    "network_calls": 0,
                    "provenance": existing,
                }, 0
        raise FreshDownloaderStop(STOP_TARGET)
    directory.mkdir(parents=True, exist_ok=True)
    temp_zip = directory / f".xbrl.zip.{uuid.uuid4().hex}.tmp"
    attempts: list[dict[str, object]] = []
    initial_network_calls = network_counter[0]
    try:
        resolution = None
        for attempt_number in range(1, max_retries + 1):
            try:
                resolve_kwargs = {
                    "requested_disclosure_no": requested,
                    "session": session,
                    "timeout_seconds": timeout_seconds,
                    "network_counter": network_counter,
                }
                if auth_loader is not None:
                    resolve_kwargs["auth_loader"] = auth_loader
                resolution = resolve_xbrl_file(**resolve_kwargs)
                evidence = dict(resolution.evidence)
                evidence["attempt_number"] = attempt_number
                attempts.append(evidence)
                break
            except TdFilesAdapterError as exc:
                evidence = dict(exc.evidence)
                evidence["attempt_number"] = attempt_number
                attempts.append(evidence)
                if not exc.retryable or attempt_number == max_retries:
                    raise DownloadFailure(
                        STOP_HTTP, failure_code=exc.classification,
                        failure_stage=exc.stage, attempts=attempts,
                        retryable=exc.retryable,
                    ) from exc
                retry_after = _retry_after_seconds(evidence.get("retry_after"))
                delay = retry_after if retry_after is not None else float(2 ** (attempt_number - 1))
                evidence["backoff_seconds"] = delay
                sleep(delay)
        if resolution is None:
            raise FreshDownloaderStop(STOP_HTTP)
        downloaded = None
        for attempt_number in range(1, max_retries + 1):
            try:
                downloaded = download_signed_zip(
                    resolution=resolution,
                    destination=temp_zip,
                    session=session,
                    timeout_seconds=timeout_seconds,
                    network_counter=network_counter,
                )
                evidence = dict(downloaded.evidence)
                evidence["attempt_number"] = attempt_number
                attempts.append(evidence)
                break
            except TdFilesAdapterError as exc:
                evidence = dict(exc.evidence)
                evidence["attempt_number"] = attempt_number
                attempts.append(evidence)
                if temp_zip.exists():
                    temp_zip.unlink()
                if not exc.retryable or attempt_number == max_retries:
                    raise DownloadFailure(
                        STOP_HTTP, failure_code=exc.classification,
                        failure_stage=exc.stage, attempts=attempts,
                        retryable=exc.retryable,
                    ) from exc
                retry_after = _retry_after_seconds(evidence.get("retry_after"))
                delay = retry_after if retry_after is not None else float(2 ** (attempt_number - 1))
                evidence["backoff_seconds"] = delay
                sleep(delay)
        if downloaded is None:
            raise FreshDownloaderStop(STOP_HTTP)

        try:
            _zip_integrity(temp_zip)
        except FreshDownloaderStop as exc:
            attempts[-1].update({
                "failure_stage": "ZIP_VALIDATION",
                "failure_code": "ZIP_INVALID",
                "retryable": False,
            })
            raise DownloadFailure(
                STOP_IDENTITY, failure_code="ZIP_INVALID",
                failure_stage="ZIP_VALIDATION", attempts=attempts,
                retryable=False,
            ) from exc
        expected_period = str(row["expected_period"])
        expected_quarter = str(row["expected_quarter"])
        meta = extract_actual_metadata_from_zip(
            str(temp_zip),
            expected_period=expected_period,
            expected_quarter=expected_quarter,
        )
        trusted = TrustedProvenance(
            source="jquants",
            requested_disclosure_no=requested,
            requested_file_type="x",
            resolved_by_function="jquants_td_files_adapter",
            official_request_succeeded=True,
            response_status=int(resolution.evidence["http_status"]),
            downloaded_size=downloaded.size,
            downloaded_sha256=downloaded.sha256,
            internal_document_id=meta.get("internal_document_id", ""),
            ticker=meta.get("ticker", ""),
            period=meta.get("period", ""),
            quarter=meta.get("quarter", ""),
            document_type=meta.get("document_type", ""),
            resolved_at=_now_utc(),
        )
        verdict = verify_zip_identity(
            str(temp_zip), requested, str(row["normalized_company_code"]),
            expected_period, expected_quarter, trusted,
        )
        readiness = {
            "identity_verdict": verdict.verdict,
            "identity_status": "DOWNLOAD_IDENTITY_VERIFIED" if verdict.passed else "DOWNLOAD_IDENTITY_MISMATCH",
            "plan_classification": classification,
            "auto_ready_allowed": bool(verdict.passed and classification == "STANDARD_FRESH_DOWNLOAD"),
            "quarantine_release_required": classification == "QUARANTINE_FRESH_RECHECK",
            "internal_document_id": None if not meta.get("internal_document_id") else meta["internal_document_id"],
            "internal_document_id_status": WITHOUT_INTERNAL_ID_STATUS,
            "linkage_basis": WITHOUT_INTERNAL_ID_LINKAGE,
            "source_route": JQUANTS_SOURCE_ROUTE,
            "td_files_result_code": resolution.evidence["result_code"],
            "xbrl_candidate_count": resolution.evidence["xbrl_candidate_count"],
            "requested_disclosure_no": requested,
            "td_files_type": "x",
            "td_files_http_status": resolution.evidence["http_status"],
            "signed_url_received": resolution.evidence["signed_url_received"],
            "file_http_status": downloaded.evidence["http_status"],
            "identity_value_sources": dict(WITHOUT_INTERNAL_ID_VALUE_SOURCES),
        }
        auto_ready = is_production_ready_identity_result(readiness)
        if classification == "STANDARD_FRESH_DOWNLOAD" and not auto_ready:
            conflict = verdict.rejection_reason == "zip_internal_identity_conflict"
            attempts[-1].update({
                "failure_stage": "ZIP_IDENTITY" if conflict else "IDENTITY",
                "failure_code": (
                    "ZIP_INTERNAL_IDENTITY_CONFLICT"
                    if conflict else "DOWNLOAD_IDENTITY_MISMATCH"
                ),
                "identity_rejection_reason": verdict.rejection_reason,
                "identity_conflict_fields": verdict.details.get("conflict_fields", []),
                "identity_candidates": [
                    {
                        key: candidate.get(key)
                        for key in (
                            "path", "format", "ticker", "period", "quarter",
                            "document_type", "internal_document_id",
                        )
                    }
                    for candidate in verdict.details.get("candidates", [])
                    if isinstance(candidate, dict)
                ],
                "zip_sha256": verdict.zip_sha256,
                "retryable": False,
            })
            raise DownloadFailure(
                STOP_IDENTITY,
                failure_code=(
                    "ZIP_INTERNAL_IDENTITY_CONFLICT"
                    if conflict else "DOWNLOAD_IDENTITY_MISMATCH"
                ),
                failure_stage="ZIP_IDENTITY" if conflict else "IDENTITY",
                attempts=attempts,
                retryable=False,
            )
        identity_status = (
            "DOWNLOAD_IDENTITY_VERIFIED"
            if classification == "STANDARD_FRESH_DOWNLOAD" and verdict.passed
            else "DOWNLOAD_IDENTITY_MISMATCH"
            if classification == "STANDARD_FRESH_DOWNLOAD"
            else "QUARANTINE_RECHECK_MATCH"
            if verdict.passed
            else "QUARANTINE_RECHECK_MISMATCH"
        )
        payload: dict[str, object] = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "identity_provenance_version": PROVENANCE_VERSION,
            "campaign_id": campaign_id,
            "manifest_row_id": row_id,
            "requested_disclosure_no": requested,
            "requested_disc_no": requested,
            "company_code": row["company_code"],
            "normalized_company_code": row["normalized_company_code"],
            "source_url": row["source_url"],
            "normalized_xbrl_url": row["normalized_xbrl_url"],
            "source_route": JQUANTS_SOURCE_ROUTE,
            "td_files_type": "x",
            "td_files_http_status": resolution.evidence["http_status"],
            "td_files_reason": resolution.evidence["reason_phrase"],
            "td_files_elapsed": resolution.evidence["elapsed_seconds"],
            "td_files_result_code": resolution.evidence["result_code"],
            "xbrl_candidate_count": resolution.evidence["xbrl_candidate_count"],
            "signed_url_received": resolution.evidence["signed_url_received"],
            "signed_url_host": resolution.evidence["signed_url_host"],
            "signed_url_scheme": resolution.evidence["signed_url_scheme"],
            "signed_url_received_at": resolution.evidence["signed_url_received_at"],
            "signed_url_redacted_digest": resolution.evidence["signed_url_redacted_digest"],
            "file_http_status": downloaded.evidence["http_status"],
            "final_url": None,
            "downloaded_at": _now_utc(),
            "downloaded_at_utc": _now_utc(),
            "downloaded_at_jst": _now_jst(),
            "http_status": downloaded.evidence["http_status"],
            "content_type": downloaded.evidence["content_type"],
            "content_length": downloaded.evidence["content_length"] or str(downloaded.size),
            "download_attempts": attempts,
            "zip_sha256": downloaded.sha256,
            "zip_size": downloaded.size,
            "internal_document_id": None if verdict.verdict == WITHOUT_INTERNAL_ID_VERDICT else meta.get("internal_document_id", ""),
            "internal_document_id_status": (
                WITHOUT_INTERNAL_ID_STATUS if verdict.verdict == WITHOUT_INTERNAL_ID_VERDICT else "present_in_artifact"
            ),
            "linkage_basis": WITHOUT_INTERNAL_ID_LINKAGE,
            "identity_value_sources": readiness["identity_value_sources"],
            "zip_internal_ticker": meta.get("ticker", ""),
            "zip_internal_period": meta.get("period", ""),
            "zip_internal_quarter": meta.get("quarter", ""),
            "document_type": meta.get("document_type", ""),
            "official_linkage_status": verdict.verdict,
            "identity_status": identity_status,
            "identity_verdict": verdict.verdict,
            "identity_rejection_reason": verdict.rejection_reason,
            "plan_classification": classification,
            "auto_ready_allowed": auto_ready,
            "quarantine_release_required": classification == "QUARANTINE_FRESH_RECHECK",
            "code_sha": code_sha,
            "run_id": run_id,
            "download_tool_version": TOOL_VERSION,
            "error_code": None if verdict.passed else verdict.rejection_reason,
            "error_message": None if verdict.passed else verdict.rejection_reason,
        }
        if contains_secret_material(payload):
            raise FreshDownloaderStop(STOP_HTTP)
        os.replace(temp_zip, zip_path)
        if sha256_file(zip_path) != downloaded.sha256:
            raise FreshDownloaderStop(STOP_IDENTITY)
        _zip_integrity(zip_path)
        published_meta = extract_actual_metadata_from_zip(
            str(zip_path), expected_period=expected_period,
            expected_quarter=expected_quarter,
        )
        if published_meta != meta:
            raise FreshDownloaderStop(STOP_IDENTITY)
        _write_atomic_json(provenance_path, payload)
        loaded = load_provenance(zip_path, provenance_path)
        if loaded != payload or contains_secret_material(loaded):
            raise FreshDownloaderStop(STOP_IDENTITY)
        return {
            "manifest_row_id": row_id,
            "requested_disclosure_no": requested,
            "plan_classification": classification,
            "status": "READY" if auto_ready else "QUARANTINED",
            "zip_path": str(zip_path),
            "provenance_path": str(provenance_path),
            "zip_sha256": downloaded.sha256,
            "identity_status": identity_status,
            "identity_verdict": verdict.verdict,
            "internal_document_id": payload["internal_document_id"],
            "ticker": payload["zip_internal_ticker"],
            "period": payload["zip_internal_period"],
            "quarter": payload["zip_internal_quarter"],
            "document_type": payload["document_type"],
            "attempt_count": len(attempts),
            "network_calls": network_counter[0] - initial_network_calls,
            "td_files_requests": 1,
            "signed_url_download_requests": 1,
            "static_url_requests": 0,
            "auto_ready": auto_ready,
            "quarantine_release_required": payload["quarantine_release_required"],
            "download_attempts": attempts,
        }, network_counter[0] - initial_network_calls
    finally:
        if temp_zip.exists():
            temp_zip.unlink()


def publish_injected_verified_artifact(
    *, row: Mapping[str, object], cache_root: Path, campaign_id: str,
    source_zip: Path, source_provenance: Path,
) -> dict[str, object]:
    """Publish a previously verified artifact pair for an isolated simulation."""
    row_id = str(row["manifest_row_id"])
    directory, zip_path, provenance_path = _production_target_paths(cache_root, campaign_id, row_id)
    if zip_path.exists() or provenance_path.exists():
        if zip_path.is_file() and provenance_path.is_file():
            payload = _load_production_provenance(zip_path, provenance_path)
            if (
                payload.get("manifest_row_id") == row_id
                and payload.get("requested_disclosure_no") == row["requested_disclosure_no"]
                and is_production_ready_identity_result(payload)
            ):
                return _result_from_verified_provenance(payload, zip_path, provenance_path, "ALREADY_COMPLETE")
        raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
    source = _load_production_provenance(source_zip, source_provenance)
    if (
        source.get("manifest_row_id") != row_id
        or source.get("requested_disclosure_no") != row["requested_disclosure_no"]
        or source.get("source_route") != JQUANTS_SOURCE_ROUTE
        or not is_production_ready_identity_result(source)
    ):
        raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
    directory.mkdir(parents=True, exist_ok=False)
    temporary_zip = directory / f".xbrl.zip.{uuid.uuid4().hex}.tmp"
    try:
        with source_zip.open("rb") as incoming, temporary_zip.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        _zip_integrity(temporary_zip)
        if sha256_file(temporary_zip) != source["zip_sha256"]:
            raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
        os.replace(temporary_zip, zip_path)
        if sha256_file(zip_path) != source["zip_sha256"]:
            raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
        _zip_integrity(zip_path)
        _write_atomic_json(provenance_path, source)
        loaded = _load_production_provenance(zip_path, provenance_path)
        if loaded != source:
            raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
        return _result_from_verified_provenance(loaded, zip_path, provenance_path, "READY")
    finally:
        if temporary_zip.exists():
            temporary_zip.unlink()


def _result_from_verified_provenance(
    payload: Mapping[str, object], zip_path: Path, provenance_path: Path, status: str,
) -> dict[str, object]:
    return {
        "manifest_row_id": str(payload["manifest_row_id"]),
        "requested_disclosure_no": str(payload["requested_disclosure_no"]),
        "plan_classification": str(payload["plan_classification"]),
        "status": status, "zip_path": str(zip_path), "provenance_path": str(provenance_path),
        "zip_sha256": str(payload["zip_sha256"]),
        "identity_status": str(payload["identity_status"]),
        "identity_verdict": str(payload["identity_verdict"]),
        "internal_document_id": (
            None if payload["internal_document_id"] is None
            else str(payload["internal_document_id"])
        ),
        "ticker": str(payload["zip_internal_ticker"]),
        "period": str(payload["zip_internal_period"]),
        "quarter": str(payload["zip_internal_quarter"]),
        "document_type": str(payload["document_type"]),
        "network_calls": 0, "attempt_count": 0, "auto_ready": True,
    }


def _load_production_provenance(zip_path: Path, provenance_path: Path) -> dict[str, object]:
    try:
        return load_provenance(zip_path, provenance_path)
    except FreshDownloaderStop as exc:
        raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT) from exc


def _write_results(output_dir: Path, results: list[dict[str, object]], summary: dict[str, object]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    values = {"download-results.json": results, "download-summary.json": summary}
    for name, value in values.items():
        (output_dir / name).write_bytes(_json_bytes(value))
    digests = {name: sha256_file(output_dir / name) for name in sorted(values)}
    (output_dir / "digests.json").write_bytes(_json_bytes(digests))
    return digests


def run_downloads(
    *, campaign_db: Path, campaign_id: str, download_plan: Path,
    manifest_list: Path, cache_root: Path, output_dir: Path, apply: bool,
    repo_root: Path, code_sha: str, min_interval_seconds: float = 1.0,
    timeout_seconds: float = 60.0, max_retries: int = 3,
    max_consecutive_failures: int = 10, session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    source_route: str | None = None,
    auth_loader: Callable[[], tuple[dict[str, str], dict[str, object]]] | None = None,
) -> dict[str, object]:
    validate_temp_write_path(cache_root, repo_root)
    validate_temp_write_path(output_dir, repo_root)
    validate_temp_write_path(manifest_list, repo_root)
    if source_route != JQUANTS_SOURCE_ROUTE:
        raise FreshDownloaderStop(STOP_URL)
    if min_interval_seconds < 0 or timeout_seconds <= 0 or max_retries < 1 or max_consecutive_failures < 1:
        raise FreshDownloaderStop(STOP_INPUT)
    selected, manifest_sha = load_manifest_list(manifest_list, campaign_id)
    plan_rows, plan_sha = load_selected_plan(download_plan, selected, campaign_id)
    rows = load_campaign_rows(campaign_db, campaign_id, plan_rows)
    if not apply:
        return {"apply": False, "selected": len(rows), "network_calls": 0, "cache_writes": 0, "output_writes": 0}
    if output_dir.exists():
        raise FreshDownloaderStop(STOP_TARGET)
    run_id = f"fresh-download-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    client = session or requests.Session()
    owns_session = session is None
    results: list[dict[str, object]] = []
    network_counter = [0]
    consecutive_failures = 0
    consecutive_failure_stop = False
    try:
        for index, row in enumerate(rows):
            if index and min_interval_seconds:
                sleep(min_interval_seconds)
            try:
                result, _calls = _download_one_jquants(
                    row=row, cache_root=cache_root, campaign_id=campaign_id,
                    session=client, timeout_seconds=timeout_seconds,
                    max_retries=max_retries, sleep=sleep,
                    code_sha=code_sha, run_id=run_id,
                    network_counter=network_counter,
                    auth_loader=auth_loader,
                )
                results.append(result)
                consecutive_failures = 0
            except DownloadFailure as exc:
                consecutive_failures += 1
                results.append(exc.result(row))
                if consecutive_failures >= max_consecutive_failures:
                    consecutive_failure_stop = True
                    break
            except FreshDownloaderStop as exc:
                if str(exc) in {STOP_URL, STOP_TARGET, STOP_INPUT, STOP_UNSAFE_PATH}:
                    raise
                consecutive_failures += 1
                results.append({
                    "manifest_row_id": row["manifest_row_id"],
                    "requested_disclosure_no": row["requested_disclosure_no"],
                    "plan_classification": row["plan_classification"],
                    "status": "FAILED", "error": str(exc),
                })
                if consecutive_failures >= max_consecutive_failures:
                    consecutive_failure_stop = True
                    break
    finally:
        if owns_session:
            client.close()
    status_counts = Counter(str(row["status"]) for row in results)
    standard_failed = sum(
        row.get("status") == "FAILED" and original.get("plan_classification") == "STANDARD_FRESH_DOWNLOAD"
        for row, original in zip(results, rows)
    )
    summary: dict[str, object] = {
        "apply": True, "campaign_id": campaign_id, "run_id": run_id,
        "selected": len(rows), "processed": len(results), "status_counts": dict(status_counts),
        "standard_failed": standard_failed, "failed": status_counts.get("FAILED", 0),
        "consecutive_failure_stop": consecutive_failure_stop,
        "network_calls": network_counter[0], "manifest_sha256": manifest_sha,
        "download_plan_sha256": plan_sha, "code_sha": code_sha,
        "production_db_writes": 0, "supabase_calls": 0,
        "source_route": JQUANTS_SOURCE_ROUTE,
        "td_files_requests": sum(int(row.get("td_files_requests", 0)) for row in results),
        "signed_url_download_requests": sum(int(row.get("signed_url_download_requests", 0)) for row in results),
        "static_url_requests": 0,
    }
    summary["digests"] = _write_results(output_dir, results, summary)
    return {"summary": summary, "results": results}


def run_production_downloads(
    *, campaign_db: Path, campaign_id: str, campaign_db_sha256: str,
    download_plan: Path, download_plan_sha256: str,
    manifest_list: Path, manifest_byte_sha256: str, manifest_semantic_sha256_value: str,
    cache_root: Path, output_dir: Path, expected_count: int, max_items: int,
    confirm_production_cache_root: str, confirm_campaign_id: str,
    confirm_production_item_count: int,
    apply: bool, production_apply: bool, source_route: str,
    repo_root: Path, code_sha: str, min_interval_seconds: float = 1.0,
    timeout_seconds: float = 60.0, max_retries: int = 3,
    session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
    auth_loader: Callable[[], tuple[dict[str, str], dict[str, object]]] | None = None,
    environment: ProductionEnvironment | None = None,
    runtime_checker: Callable[[Path], dict[str, object]] = check_production_runtime,
    artifact_provider: Callable[[Mapping[str, object], Path, str], dict[str, object]] | None = None,
    state_after_update: Callable[[int, sqlite3.Connection], None] | None = None,
) -> dict[str, object]:
    """Execute a manifest-bound, filesystem-first production download batch."""
    env = environment or default_production_environment(repo_root, campaign_id)
    if not apply or not production_apply or source_route != JQUANTS_SOURCE_ROUTE:
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    counts = (expected_count, max_items, confirm_production_item_count)
    if (
        any(not isinstance(value, int) or isinstance(value, bool) for value in counts)
        or expected_count != max_items
        or expected_count != confirm_production_item_count
        or not 1 <= expected_count <= PRODUCTION_MAX_COUNT
    ):
        raise FreshDownloaderStop(STOP_PRODUCTION_COUNT)
    if confirm_campaign_id != campaign_id or confirm_production_cache_root != str(cache_root):
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    validate_production_paths(
        campaign_db=campaign_db, cache_root=cache_root, output_dir=output_dir,
        manifest_list=manifest_list, download_plan=download_plan,
        environment=env, repo_root=repo_root,
    )
    required_digests = (
        campaign_db_sha256, download_plan_sha256,
        manifest_byte_sha256, manifest_semantic_sha256_value,
    )
    if any(re.fullmatch(r"[0-9a-fA-F]{64}", value or "") is None for value in required_digests):
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    if sha256_file(campaign_db) != campaign_db_sha256.lower():
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    if sha256_file(download_plan) != download_plan_sha256.lower():
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    try:
        selected, actual_manifest_sha = load_manifest_list(manifest_list, campaign_id)
    except FreshDownloaderStop as exc:
        raise FreshDownloaderStop(STOP_PRODUCTION_COUNT) from exc
    if actual_manifest_sha != manifest_byte_sha256.lower():
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    if manifest_semantic_sha256(selected) != manifest_semantic_sha256_value.lower():
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    if len(selected) != expected_count or any(
        row["plan_classification"] != "STANDARD_FRESH_DOWNLOAD" for row in selected
    ):
        raise FreshDownloaderStop(STOP_PRODUCTION_COUNT)
    plan_rows, actual_plan_sha = load_selected_plan(download_plan, selected, campaign_id)
    if actual_plan_sha != download_plan_sha256.lower() or len(plan_rows) != expected_count:
        raise FreshDownloaderStop(STOP_PRODUCTION_GUARD)
    before_rows = load_production_rows(campaign_db, campaign_id, plan_rows)
    target_ids = [str(row["manifest_row_id"]) for row in before_rows]
    if len(set(target_ids)) != expected_count:
        raise FreshDownloaderStop(STOP_PRODUCTION_COUNT)
    if any(
        row.get("plan_classification") != "STANDARD_FRESH_DOWNLOAD"
        or row.get("fresh_status") not in {"NOT_STARTED", "FAILED_RETRYABLE"}
        for row in before_rows
    ):
        raise FreshDownloaderStop(STOP_PRODUCTION_COUNT)
    target_zip_paths = [str(row.get("target_zip_path") or "") for row in before_rows]
    target_provenance_paths = [str(row.get("target_provenance_path") or "") for row in before_rows]
    if (
        "" in target_zip_paths
        or "" in target_provenance_paths
        or len(set(target_zip_paths)) != expected_count
        or len(set(target_provenance_paths)) != expected_count
    ):
        raise FreshDownloaderStop(STOP_PRODUCTION_COUNT)

    runtime_evidence = [runtime_checker(repo_root)]
    journal_path = output_dir / "journal.json"
    run_id = f"production-fresh-download-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    if output_dir.exists():
        if not journal_path.is_file():
            raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if (
            journal.get("campaign_id") != campaign_id
            or journal.get("campaign_db_start_sha256") != campaign_db_sha256.lower()
            or journal.get("manifest_byte_sha256") != actual_manifest_sha
            or journal.get("manifest_semantic_sha256") != manifest_semantic_sha256_value.lower()
            or journal.get("targets") != target_ids
            or journal.get("current_phase") in {"COMPLETE", "DB_COMMITTED"}
        ):
            raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
        run_id = str(journal["run_id"])
    else:
        output_dir.mkdir(parents=False, exist_ok=False)
        journal = {
            "run_id": run_id, "campaign_id": campaign_id, "code_sha": code_sha,
            "manifest_byte_sha256": actual_manifest_sha,
            "manifest_semantic_sha256": manifest_semantic_sha256_value.lower(),
            "download_plan_sha256": actual_plan_sha,
            "campaign_db_start_sha256": campaign_db_sha256.lower(),
            "targets": target_ids, "started_at": _now_utc(),
            "runtime_checks": runtime_evidence, "backup": None,
            "rows": {
                row_id: {
                    "manifest_row_id": row_id,
                    "stage_a_state": "NOT_STARTED", "stage_b_state": "NOT_STARTED",
                    "zip_state": "NOT_STARTED", "provenance_state": "NOT_STARTED",
                    "loader_state": "NOT_STARTED", "filesystem_state": "NOT_STARTED",
                    "fresh_db_start_state": str(row.get("fresh_status")),
                    "fresh_db_end_state": None, "db_state": "BEFORE",
                    "network_attempts_started": 0, "failure_code": None,
                    "artifact_reused": False, "network_attempted": False,
                }
                for row_id, row in zip(target_ids, before_rows)
            },
            "current_phase": "CREATED", "failure_code": None, "finished_at": None,
        }
        _journal_write(journal_path, journal)

    try:
        backup = create_verified_backup(campaign_db, output_dir)
        journal.setdefault("runtime_checks", runtime_evidence)
        _journal_update(journal_path, journal, "BACKUP_VERIFIED", backup=backup)
        runtime_evidence.append(runtime_checker(repo_root))
        journal["runtime_checks"] = runtime_evidence
        _journal_update(journal_path, journal, "NETWORK_STARTED")

        client = session or requests.Session()
        owns_session = session is None
        network_counter = [0]
        results: list[dict[str, object]] = []
        try:
            for index, row in enumerate(before_rows):
                if index and min_interval_seconds:
                    sleep(min_interval_seconds)
                row_id = str(row["manifest_row_id"])
                row_journal = journal["rows"][row_id]
                _directory, zip_path, provenance_path = _production_target_paths(cache_root, campaign_id, row_id)
                if (
                    str(row.get("target_zip_path")) != str(zip_path)
                    or str(row.get("target_provenance_path")) != str(provenance_path)
                ):
                    raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
                if row.get("fresh_status") == "COMPLETE" and (
                    not zip_path.is_file() or not provenance_path.is_file()
                ):
                    raise FreshDownloaderStop(STOP_PRODUCTION_DIVERGENCE)
                if zip_path.exists() != provenance_path.exists():
                    raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
                if zip_path.is_file() and provenance_path.is_file():
                    try:
                        payload = _load_production_provenance(zip_path, provenance_path)
                    except FreshDownloaderStop as exc:
                        if row.get("fresh_status") == "COMPLETE":
                            raise FreshDownloaderStop(STOP_PRODUCTION_DIVERGENCE) from exc
                        raise
                    if (
                        payload.get("manifest_row_id") != row_id
                        or payload.get("requested_disclosure_no") != row["requested_disclosure_no"]
                        or not is_production_ready_identity_result(payload)
                    ):
                        raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
                    if row.get("fresh_status") == "COMPLETE" and any((
                        row.get("artifact_zip_sha256") != payload.get("zip_sha256"),
                        row.get("artifact_internal_document_id") != payload.get("internal_document_id"),
                        row.get("artifact_ticker") != payload.get("zip_internal_ticker"),
                        row.get("artifact_period") != payload.get("zip_internal_period"),
                        row.get("artifact_quarter") != payload.get("zip_internal_quarter"),
                        row.get("identity_verdict") != payload.get("identity_verdict"),
                    )):
                        raise FreshDownloaderStop(STOP_PRODUCTION_DIVERGENCE)
                    result = _result_from_verified_provenance(
                        payload, zip_path, provenance_path, "DB_ONLY_REPAIR",
                    )
                elif artifact_provider is not None:
                    row_journal.update(stage_a_state="STARTED", network_attempts_started=1)
                    _journal_write(journal_path, journal)
                    try:
                        result = dict(artifact_provider(row, cache_root, campaign_id))
                    except DownloadFailure as exc:
                        failure = exc.result(row)
                        raw_stage = str(failure.get("failure_stage") or "")
                        normalized_stage = canonical_failure_stage(failure)
                        row_journal.update(
                            stage_a_state="FAILED",
                            failure_code=failure["failure_code"],
                            failure_stage=normalized_stage,
                            raw_failure_stage=raw_stage,
                            canonical_failure_stage=normalized_stage,
                            source_route=failure["source_route"],
                            http_status=(
                                failure.get("td_files_http_status")
                                if failure.get("td_files_http_status") is not None
                                else failure.get("http_status")
                            ),
                            failure_telemetry=safe_failure_telemetry(failure, row),
                        )
                        journal["failure_code"] = failure["failure_code"]
                        _journal_write(journal_path, journal)
                        evidence = write_known_failure_evidence(
                            output_dir=output_dir, run_id=run_id, campaign_id=campaign_id,
                            row=row, failure=failure, journal_path=journal_path,
                            manifest_path=manifest_list, manifest_sha256=actual_manifest_sha,
                            campaign_db_start_sha256=campaign_db_sha256,
                        )
                        if evidence is not None:
                            contract = evidence["contract"]
                            row_journal.update(
                                stage_a_state="FAILED", failure_code=contract[0],
                                failure_stage=contract[1], source_route=contract[2],
                                http_status=contract[3], failure_evidence=evidence,
                            )
                            journal["failure_code"] = contract[0]
                            _journal_write(journal_path, journal)
                        raise
                    result.update({
                        "attempt_count": 1, "network_calls": 2,
                        "td_files_requests": 1, "signed_url_download_requests": 1,
                        "static_url_requests": 0,
                    })
                else:
                    row_journal.update(stage_a_state="STARTED", network_attempts_started=1)
                    _journal_write(journal_path, journal)
                    try:
                        result, _calls = _download_one_jquants(
                            row=row, cache_root=cache_root, campaign_id=campaign_id,
                            session=client, timeout_seconds=timeout_seconds,
                            max_retries=max_retries, sleep=sleep, code_sha=code_sha,
                            run_id=run_id, network_counter=network_counter,
                            auth_loader=auth_loader, target_path_builder=_production_target_paths,
                        )
                    except DownloadFailure as exc:
                        failure = exc.result(row)
                        raw_stage = str(failure.get("failure_stage") or "")
                        normalized_stage = canonical_failure_stage(failure)
                        row_journal.update(
                            stage_a_state="FAILED",
                            failure_code=failure["failure_code"],
                            failure_stage=normalized_stage,
                            raw_failure_stage=raw_stage,
                            canonical_failure_stage=normalized_stage,
                            source_route=failure["source_route"],
                            http_status=(
                                failure.get("td_files_http_status")
                                if failure.get("td_files_http_status") is not None
                                else failure.get("http_status")
                            ),
                            failure_telemetry=safe_failure_telemetry(failure, row),
                        )
                        journal["failure_code"] = failure["failure_code"]
                        _journal_write(journal_path, journal)
                        evidence = write_known_failure_evidence(
                            output_dir=output_dir, run_id=run_id, campaign_id=campaign_id,
                            row=row, failure=failure, journal_path=journal_path,
                            manifest_path=manifest_list, manifest_sha256=actual_manifest_sha,
                            campaign_db_start_sha256=campaign_db_sha256,
                        )
                        if evidence is not None:
                            contract = evidence["contract"]
                            row_journal.update(
                                stage_a_state="FAILED", failure_code=contract[0],
                                failure_stage=contract[1], source_route=contract[2],
                                http_status=contract[3], failure_evidence=evidence,
                            )
                            journal["failure_code"] = contract[0]
                            _journal_write(journal_path, journal)
                        raise
                    if result.get("status") == "ALREADY_COMPLETE":
                        payload = _load_production_provenance(zip_path, provenance_path)
                        result = _result_from_verified_provenance(payload, zip_path, provenance_path, "DB_ONLY_REPAIR")
                payload = _load_production_provenance(zip_path, provenance_path)
                if (
                    not is_production_ready_identity_result(payload)
                    or result.get("zip_sha256") != payload.get("zip_sha256")
                ):
                    raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
                results.append(result)
                network_attempts = int(result.get("attempt_count", 0) or 0)
                row_journal.update({
                    "stage_a_state": "SUCCEEDED" if network_attempts else "NOT_REQUIRED",
                    "stage_b_state": "SUCCEEDED" if network_attempts else "NOT_REQUIRED",
                    "zip_state": "VERIFIED", "provenance_state": "VERIFIED",
                    "loader_state": "ACCEPTED", "filesystem_state": "ARTIFACTS_READY",
                    "db_state": "PENDING", "network_attempts_started": network_attempts,
                    "artifact_reused": network_attempts == 0,
                    "network_attempted": network_attempts > 0,
                    "zip_sha256": payload["zip_sha256"],
                    "internal_document_id": payload["internal_document_id"],
                    "failure_code": None,
                })
                _journal_write(journal_path, journal)
        finally:
            if owns_session:
                client.close()

        if len(results) != expected_count:
            raise FreshDownloaderStop(STOP_PRODUCTION_ARTIFACT)
        _journal_update(journal_path, journal, "ARTIFACTS_READY")
        _journal_update(journal_path, journal, "DB_PENDING")
        if sha256_file(campaign_db) != campaign_db_sha256.lower():
            journal["failure_code"] = STOP_PRODUCTION_DB_CHANGED
            _journal_write(journal_path, journal)
            raise FreshDownloaderStop(STOP_PRODUCTION_DB_CHANGED)
        runtime_evidence.append(runtime_checker(repo_root))
        journal["runtime_checks"] = runtime_evidence

        conn = connect_db(campaign_db)
        try:
            excluded = set(target_ids)
            filing_rows_before = _campaign_rows_digest(conn, campaign_id, set())
            non_target_before = _fresh_rows_digest(conn, campaign_id, excluded)
            result_by_id = {str(row["manifest_row_id"]): row for row in results}
            complete_rows: list[dict[str, object]] = []
            pending_rows: list[dict[str, object]] = []
            pending_results: list[dict[str, object]] = []
            for row in before_rows:
                result = result_by_id[str(row["manifest_row_id"])]
                complete = (
                    row.get("fresh_status") == "COMPLETE"
                    and row.get("artifact_internal_document_id") == result.get("internal_document_id")
                    and row.get("artifact_zip_sha256") == result.get("zip_sha256")
                    and row.get("artifact_ticker") == result.get("ticker")
                    and row.get("artifact_period") == result.get("period")
                    and row.get("artifact_quarter") == result.get("quarter")
                    and row.get("identity_verdict") == result.get("identity_verdict")
                    and row.get("last_error_code") is None
                    and row.get("last_error_stage") is None
                    and row.get("last_error_message") is None
                )
                if complete:
                    complete_rows.append(dict(row))
                else:
                    pending_rows.append(dict(row))
                    pending_results.append(result)
            readback = complete_rows
            if pending_rows:
                try:
                    readback += apply_fresh_download_successes(
                        conn, campaign_id=campaign_id, before_rows=pending_rows,
                        verified_results=pending_results, expected_count=len(pending_rows),
                        run_id=run_id, journal_path=str(journal_path),
                        attempt_increments=[
                            1 if int(result.get("attempt_count", 0) or 0) > 0 else 0
                            for result in pending_results
                        ],
                        after_update=state_after_update,
                    )
                except (FreshDownloadCASFailed, sqlite3.Error) as exc:
                    journal["failure_code"] = STOP_PRODUCTION_DB_CAS
                    _journal_write(journal_path, journal)
                    raise FreshDownloaderStop(STOP_PRODUCTION_DB_CAS) from exc
            db_updated_count = len(pending_rows)
            non_target_after = _fresh_rows_digest(conn, campaign_id, excluded)
            filing_rows_after = _campaign_rows_digest(conn, campaign_id, set())
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            conn.close()
        if non_target_before != non_target_after:
            raise FreshDownloaderStop(STOP_PRODUCTION_DB_CAS)
        if filing_rows_before != filing_rows_after:
            raise FreshDownloaderStop(STOP_PRODUCTION_DB_CAS)
        if integrity != "ok" or foreign_keys:
            raise FreshDownloaderStop(STOP_PRODUCTION_DB_CAS)
        for row in readback:
            row_journal = journal["rows"][str(row["manifest_row_id"])]
            row_journal["db_state"] = "READY"
            row_journal["fresh_db_end_state"] = "COMPLETE"
        _journal_update(
            journal_path, journal, "DB_COMMITTED",
            db_readback_count=len(readback), non_target_digest=non_target_after,
            integrity_check=integrity, foreign_key_check=foreign_keys,
        )
        runtime_evidence.append(runtime_checker(repo_root))
        journal["runtime_checks"] = runtime_evidence
        summary = {
            "apply": True, "production_apply": True, "campaign_id": campaign_id,
            "run_id": run_id, "selected": len(results), "artifacts_ready": len(results),
            "db_updated": db_updated_count,
            "network_calls": sum(int(row.get("network_calls", 0) or 0) for row in results),
            "non_target_changed": 0, "integrity_check": integrity,
            "foreign_key_check": foreign_keys, "backup": backup,
        }
        results_dir = output_dir / "results"
        if not results_dir.exists():
            summary["digests"] = _write_results(results_dir, results, summary)
        _journal_update(
            journal_path, journal, "COMPLETE", failure_code=None,
            finished_at=_now_utc(), summary=summary,
        )
        return {"summary": summary, "results": results, "readback": readback, "journal": journal}
    except FreshDownloaderStop as exc:
        for row_journal in journal.get("rows", {}).values():
            if (
                row_journal.get("stage_a_state") == "STARTED"
                and row_journal.get("filesystem_state") != "ARTIFACTS_READY"
            ):
                row_journal["stage_a_state"] = "FAILED"
                row_journal["failure_code"] = row_journal.get("failure_code") or str(exc)
        if journal.get("current_phase") not in {"DB_PENDING", "DB_COMMITTED"}:
            _journal_update(
                journal_path, journal, "FAILED",
                failure_code=journal.get("failure_code") or str(exc),
                finished_at=_now_utc(),
            )
        raise
