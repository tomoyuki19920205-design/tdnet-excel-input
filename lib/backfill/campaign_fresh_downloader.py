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
import sqlite3
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urljoin, urlsplit

import requests

from lib.backfill.campaign_fresh_download_plan import validate_download_url
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

PLAN_CLASSES = frozenset({"STANDARD_FRESH_DOWNLOAD", "QUARANTINE_FRESH_RECHECK"})
OFFICIAL_HOSTS = frozenset({"www.release.tdnet.info", "release.tdnet.info"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
TOOL_VERSION = "1"
USER_AGENT = "tdnet-excel-input/v4-campaign-fresh-downloader/1"
MAX_REDIRECTS = 5
PROVENANCE_SCHEMA_VERSION = "1"


class FreshDownloaderStop(RuntimeError):
    """Structured downloader stop."""


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


def _validate_manifest_row_id(value: object) -> str:
    row_id = str(value or "")
    if not row_id or row_id in {".", ".."} or Path(row_id).name != row_id or any(c in row_id for c in "\\/:*?\"<>|"):
        raise FreshDownloaderStop(STOP_INPUT)
    return row_id


def _target_paths(cache_root: Path, campaign_id: str, row_id: str) -> tuple[Path, Path, Path]:
    directory = cache_root / "cache" / campaign_id / _validate_manifest_row_id(row_id)
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


def _request_following_safe_redirects(
    session: requests.Session,
    url: str,
    requested_id: str,
    timeout_seconds: float,
    network_counter: list[int],
) -> tuple[requests.Response, list[dict[str, object]]]:
    current = _validate_http_url(url, requested_id)
    redirects: list[dict[str, object]] = []
    for _ in range(MAX_REDIRECTS + 1):
        network_counter[0] += 1
        response = session.get(
            current, headers={"User-Agent": USER_AGENT}, timeout=timeout_seconds,
            stream=True, allow_redirects=False,
        )
        if response.status_code not in REDIRECT_STATUS:
            return response, redirects
        location = response.headers.get("Location", "")
        candidate = urljoin(current, location)
        try:
            candidate = _validate_http_url(candidate, requested_id)
        except FreshDownloaderStop:
            response.close()
            raise
        redirects.append({"status": response.status_code, "from_url": current, "to_url": candidate})
        response.close()
        current = candidate
    raise FreshDownloaderStop(STOP_URL)


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
        "zip_internal_quarter", "document_type", "identity_status", "plan_classification",
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
        "internal_document_id": str(payload["internal_document_id"]),
        "ticker": str(payload["zip_internal_ticker"]),
        "period": str(payload["zip_internal_period"]),
        "quarter": str(payload["zip_internal_quarter"]),
        "document_type": str(payload["document_type"]),
    }
    if meta != expected_meta:
        raise FreshDownloaderStop(STOP_IDENTITY)
    return payload


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
            started_utc = _now_utc()
            record: dict[str, object] = {"attempt": attempt_number, "started_at_utc": started_utc}
            try:
                response, redirects = _request_following_safe_redirects(
                    session, str(row["normalized_xbrl_url"]), requested, timeout_seconds,
                    network_counter,
                )
                final_url = _validate_http_url(str(response.url), requested)
                record.update({"http_status": response.status_code, "redirects": redirects, "final_url": final_url})
                if response.status_code == 200:
                    content_type = str(response.headers.get("Content-Type", ""))
                    if "html" in content_type.lower():
                        record.update({"error": "HTML_RESPONSE", "ended_at_utc": _now_utc()})
                        attempts.append(record)
                        raise FreshDownloaderStop(STOP_HTTP)
                    response_headers = dict(response.headers)
                    try:
                        zip_sha, zip_size = _write_stream(response, temp_zip)
                    except requests.RequestException as exc:
                        record.update({
                            "exception": type(exc).__name__, "error": str(exc),
                            "ended_at_utc": _now_utc(),
                        })
                        attempts.append(record)
                        response.close(); response = None
                        if temp_zip.exists():
                            temp_zip.unlink()
                        if attempt_number == max_retries:
                            raise FreshDownloaderStop(STOP_HTTP) from exc
                        delay = float(2 ** (attempt_number - 1))
                        record["backoff_seconds"] = delay
                        sleep(delay)
                        continue
                    record.update({"bytes": zip_size, "ended_at_utc": _now_utc()})
                    attempts.append(record)
                    response.close(); response = None
                    break
                retryable = response.status_code in RETRYABLE_STATUS
                record["error"] = f"HTTP_{response.status_code}"
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                response.close()
                response = None
                if not retryable or attempt_number == max_retries:
                    record["ended_at_utc"] = _now_utc(); attempts.append(record)
                    raise FreshDownloaderStop(STOP_HTTP)
                delay = retry_after if retry_after is not None else float(2 ** (attempt_number - 1))
                record.update({"retry_after_seconds": retry_after, "backoff_seconds": delay, "ended_at_utc": _now_utc()})
                attempts.append(record)
                sleep(delay)
                continue
            except FreshDownloaderStop:
                if record not in attempts:
                    record["ended_at_utc"] = _now_utc(); attempts.append(record)
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                record.update({"exception": type(exc).__name__, "error": str(exc), "ended_at_utc": _now_utc()})
                attempts.append(record)
                if attempt_number == max_retries:
                    raise FreshDownloaderStop(STOP_HTTP) from exc
                delay = float(2 ** (attempt_number - 1))
                record["backoff_seconds"] = delay
                sleep(delay)
                continue
        if not zip_sha:
            raise FreshDownloaderStop(STOP_HTTP)
        _zip_integrity(temp_zip)
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
        if classification == "STANDARD_FRESH_DOWNLOAD" and not verdict.passed:
            raise FreshDownloaderStop(STOP_IDENTITY)
        auto_ready = bool(verdict.passed and classification == "STANDARD_FRESH_DOWNLOAD")
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
            "content_length": response_headers.get("Content-Length", str(zip_size)),
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
        }, network_counter[0] - initial_network_calls
    finally:
        if response is not None:
            response.close()
        if temp_zip.exists():
            temp_zip.unlink()


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
) -> dict[str, object]:
    validate_temp_write_path(cache_root, repo_root)
    validate_temp_write_path(output_dir, repo_root)
    validate_temp_write_path(manifest_list, repo_root)
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
                result, _calls = _download_one(
                    row=row, cache_root=cache_root, campaign_id=campaign_id,
                    session=client, timeout_seconds=timeout_seconds, max_retries=max_retries,
                    sleep=sleep, code_sha=code_sha, run_id=run_id,
                    network_counter=network_counter,
                )
                results.append(result)
                consecutive_failures = 0
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
    }
    summary["digests"] = _write_results(output_dir, results, summary)
    return {"summary": summary, "results": results}
