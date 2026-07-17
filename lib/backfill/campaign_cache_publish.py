"""Atomic, manifest-scoped cache publishing for the V4 campaign.

Only an explicitly temporary campaign database and cache root are writable.
ZIP files are verified before publication and the provenance sidecar is always
published last, making it the ready marker for a cache entry.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from lib.backfill.campaign_state import SCHEMA_VERSION, get_schema_version
from src.segment.segment_zip_resolver import _load_sidecar_provenance
from src.segment.zip_identity_verifier import (
    PROVENANCE_VERSION,
    TrustedProvenance,
    extract_actual_metadata_from_zip,
    verify_zip_identity,
)

STOP_UNSAFE_PATH = "STOP_V4_CAMPAIGN_CACHE_PUBLISH_UNSAFE_PATH"
STOP_INPUT = "STOP_V4_CAMPAIGN_CACHE_PUBLISH_INPUT_CHANGED"
STOP_PRECONDITION = "STOP_V4_CAMPAIGN_CACHE_PUBLISH_PRECONDITION_CHANGED"
STOP_IDENTITY = "STOP_V4_CAMPAIGN_CACHE_PUBLISH_IDENTITY_MISMATCH"
STOP_CONFLICT = "STOP_V4_CAMPAIGN_CACHE_PUBLISH_TARGET_CONFLICT"
STOP_LOCKED = "STOP_V4_CAMPAIGN_CACHE_PUBLISH_LOCKED"
STOP_DB = "STOP_V4_CAMPAIGN_CACHE_PUBLISH_DB_FAILED"

CLASS_TO_STATUS = {
    "TARGET_ZIP_NEEDS_SIDECAR": "SIDECAR_REQUIRED",
    "LEGACY_CACHE_COPY_CANDIDATE": "LEGACY_COPY_REQUIRED",
    "READY_IDENTITY_VERIFIED": "READY",
}
MUTABLE_CACHE_ERRORS = {
    "CACHE_MISSING", "SIDECAR_MISSING", "LEGACY_COPY_REQUIRED",
    "CACHE_PUBLISH_FAILED", "CACHE_IDENTITY_MISMATCH",
}
PROTECTED_FIELDS = (
    "campaign_id", "manifest_row_id", "requested_disclosure_no",
    "company_code", "normalized_company_code", "source_url",
    "normalized_xbrl_url", "expected_period", "expected_quarter",
    "document_type", "internal_document_id", "zip_sha256",
    "zip_internal_ticker", "zip_internal_period", "zip_internal_quarter",
    "identity_status", "registration_status", "extraction_status",
    "sqlite_save_status", "canonical_save_status", "supabase_save_status",
    "started_at", "completed_at",
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


def _safe_relative(value: object, *, requested: str, legacy: bool = False) -> Path:
    text = str(value or "")
    if legacy and not text:
        return Path()
    path = Path(text)
    expected = Path(requested) / "xbrl.zip"
    if path.is_absolute() or ".." in path.parts or path.name != "xbrl.zip":
        raise RuntimeError(STOP_INPUT)
    if not legacy and path.as_posix() != expected.as_posix():
        raise RuntimeError(STOP_INPUT)
    return path


def load_manifest_list(path: Path, *, campaign_id: str) -> tuple[list[dict[str, object]], str]:
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError(STOP_INPUT)
    digest = sha256_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(STOP_INPUT) from exc
    if not isinstance(payload, dict) or payload.get("campaign_id") != campaign_id:
        raise RuntimeError(STOP_INPUT)
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(STOP_INPUT)
    row_ids: list[str] = []
    requested_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("classification") not in CLASS_TO_STATUS:
            raise RuntimeError(STOP_INPUT)
        row_id = str(row.get("manifest_row_id") or "")
        requested = str(row.get("requested_disclosure_no") or "")
        expected = row.get("expected_identity")
        actual = row.get("actual_identity")
        if (
            not row_id or not requested or not isinstance(expected, dict)
            or not isinstance(actual, dict) or row.get("campaign_id") != campaign_id
            or not actual.get("internal_document_id") or not actual.get("zip_sha256")
            or not actual.get("ticker") or not actual.get("period")
            or not actual.get("quarter") or not actual.get("document_type")
        ):
            raise RuntimeError(STOP_INPUT)
        _safe_relative(row.get("target_relative_path"), requested=requested)
        legacy = _safe_relative(row.get("legacy_relative_path"), requested=requested, legacy=True)
        if row["classification"] == "LEGACY_CACHE_COPY_CANDIDATE" and not legacy.parts:
            raise RuntimeError(STOP_INPUT)
        row_ids.append(row_id)
        requested_ids.append(requested)
    if len(set(row_ids)) != len(rows) or len(set(requested_ids)) != len(rows):
        raise RuntimeError(STOP_INPUT)
    if row_ids != sorted(row_ids):
        raise RuntimeError(STOP_INPUT)
    return rows, digest


def _metadata(zip_path: Path, row: Mapping[str, object]) -> tuple[dict[str, str], TrustedProvenance]:
    expected = row["expected_identity"]
    actual = row["actual_identity"]
    assert isinstance(expected, dict) and isinstance(actual, dict)
    period = str(expected.get("expected_period") or "")
    quarter = str(expected.get("expected_quarter") or "")
    meta = extract_actual_metadata_from_zip(str(zip_path), expected_period=period, expected_quarter=quarter)
    sha = sha256_file(zip_path)
    expected_meta = {
        "ticker": str(actual["ticker"]), "period": str(actual["period"]),
        "quarter": str(actual["quarter"]), "document_type": str(actual["document_type"]),
        "internal_document_id": str(actual["internal_document_id"]),
    }
    if (
        meta != expected_meta or sha.lower() != str(actual["zip_sha256"]).lower()
        or period != meta["period"] or quarter != meta["quarter"]
    ):
        raise RuntimeError(STOP_IDENTITY)
    provenance = TrustedProvenance(
        source="jquants", requested_disclosure_no=str(row["requested_disclosure_no"]),
        requested_file_type="x", resolved_by_function="campaign_cache_publish",
        official_request_succeeded=True, response_status=200,
        downloaded_size=zip_path.stat().st_size, downloaded_sha256=sha,
        internal_document_id=meta["internal_document_id"], ticker=meta["ticker"],
        period=meta["period"], quarter=meta["quarter"],
        document_type=meta["document_type"], resolved_at=_now(),
    )
    verdict = verify_zip_identity(
        str(zip_path), str(row["requested_disclosure_no"]), meta["ticker"],
        meta["period"], meta["quarter"], provenance,
    )
    if not verdict.passed or verdict.internal_id != meta["internal_document_id"]:
        raise RuntimeError(STOP_IDENTITY)
    return meta, provenance


def _sidecar_payload(prov: TrustedProvenance) -> dict[str, object]:
    return {
        "schema_version": PROVENANCE_VERSION, "source": "jquants",
        "requested_disclosure_no": prov.requested_disclosure_no,
        "requested_file_type": "x", "internal_document_id": prov.internal_document_id,
        "zip_sha256": prov.downloaded_sha256, "downloaded_size": prov.downloaded_size,
        "ticker": prov.ticker, "period": prov.period, "quarter": prov.quarter,
        "document_type": prov.document_type, "fetched_at": prov.resolved_at,
        "resolved_by_function": "campaign_cache_publish",
    }


def _publish_zip(source: Path, target: Path, expected_sha: str) -> None:
    if target.exists():
        if target.is_file() and sha256_file(target).lower() == expected_sha.lower():
            with zipfile.ZipFile(target) as archive:
                if archive.testzip() is None:
                    return
        raise RuntimeError(STOP_CONFLICT)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with source.open("rb") as src, temp_path.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if sha256_file(temp_path).lower() != expected_sha.lower():
            raise RuntimeError(STOP_IDENTITY)
        with zipfile.ZipFile(temp_path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError(STOP_IDENTITY)
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _publish_sidecar(zip_path: Path, prov: TrustedProvenance) -> None:
    sidecar = Path(str(zip_path) + ".provenance.json")
    if sidecar.exists():
        loaded = _load_sidecar_provenance(
            str(zip_path), prov.requested_disclosure_no, prov.period, prov.quarter,
        )
        if loaded is not None and loaded.internal_document_id == prov.internal_document_id:
            return
        raise RuntimeError(STOP_CONFLICT)
    temp_path = sidecar.parent / f".{sidecar.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("xb") as stream:
            stream.write(_json_bytes(_sidecar_payload(prov)))
            stream.flush()
            os.fsync(stream.fileno())
        parsed = json.loads(temp_path.read_text(encoding="utf-8"))
        if parsed != _sidecar_payload(prov):
            raise RuntimeError(STOP_IDENTITY)
        os.replace(temp_path, sidecar)
        loaded = _load_sidecar_provenance(
            str(zip_path), prov.requested_disclosure_no, prov.period, prov.quarter,
        )
        if loaded is None or loaded.internal_document_id != prov.internal_document_id:
            raise RuntimeError(STOP_IDENTITY)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _read_db_rows(conn: sqlite3.Connection, campaign_id: str, row_ids: list[str]) -> dict[str, dict[str, object]]:
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in row_ids)
    rows = conn.execute(
        f"SELECT * FROM campaign_filings WHERE campaign_id=? AND manifest_row_id IN ({placeholders})",
        [campaign_id, *row_ids],
    ).fetchall()
    return {str(row["manifest_row_id"]): dict(row) for row in rows}


def _validate_db_rows(rows: list[dict[str, object]], db_rows: Mapping[str, Mapping[str, object]]) -> None:
    if len(db_rows) != len(rows):
        raise RuntimeError(STOP_PRECONDITION)
    for row in rows:
        current = db_rows[str(row["manifest_row_id"])]
        actual = row["actual_identity"]
        expected_status = CLASS_TO_STATUS[str(row["classification"])]
        assert isinstance(actual, dict)
        if (
            current["requested_disclosure_no"] != row["requested_disclosure_no"]
            or current["normalized_company_code"] != actual["ticker"]
            or current["internal_document_id"] != actual["internal_document_id"]
            or str(current["zip_sha256"]).lower() != str(actual["zip_sha256"]).lower()
            or current["zip_internal_period"] != actual["period"]
            or current["zip_internal_quarter"] != actual["quarter"]
            or current["identity_status"] != "VERIFIED"
            or current["cache_status"] not in {expected_status, "READY"}
        ):
            raise RuntimeError(STOP_PRECONDITION)


def _write_audit(output_dir: Path, values: Mapping[str, object]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, value in values.items():
        (output_dir / name).write_bytes(_json_bytes(value))
    digests = {name: sha256_file(output_dir / name) for name in sorted(values)}
    (output_dir / "digests.json").write_bytes(_json_bytes(digests))
    return digests


def _semantic_digest(actions: list[dict[str, object]]) -> str:
    stable = [{
        key: action[key] for key in (
            "manifest_row_id", "requested_disclosure_no", "classification",
            "action", "target_relative_path", "zip_sha256", "internal_document_id",
        )
    } for action in actions]
    return hashlib.sha256(_json_bytes(stable)).hexdigest()


def publish_campaign_cache(
    *, campaign_db: Path, campaign_id: str, cache_root: Path,
    manifest_list: Path, output_dir: Path, apply: bool, repo_root: Path,
) -> dict[str, object]:
    for path in (campaign_db, cache_root, manifest_list, output_dir):
        validate_temp_path(path, repo_root)
    rows, manifest_sha = load_manifest_list(manifest_list, campaign_id=campaign_id)
    if not apply:
        return {"apply": False, "requested": len(rows), "changed": False, "manifest_sha256": manifest_sha}
    if not campaign_db.is_file() or not cache_root.is_dir() or output_dir.exists():
        raise RuntimeError(STOP_PRECONDITION)

    lock = cache_root / ".v4_campaign_cache_publish.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        os.close(descriptor)
    except FileExistsError as exc:
        raise RuntimeError(STOP_LOCKED) from exc

    conn = sqlite3.connect(str(campaign_db), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    row_ids = [str(row["manifest_row_id"]) for row in rows]
    before_rows: dict[str, dict[str, object]] = {}
    actions: list[dict[str, object]] = []
    published_ids: list[str] = []
    started = _now()
    try:
        if get_schema_version(conn) != SCHEMA_VERSION:
            raise RuntimeError(STOP_PRECONDITION)
        before_rows = _read_db_rows(conn, campaign_id, row_ids)
        _validate_db_rows(rows, before_rows)
        for row in rows:
            row_id = str(row["manifest_row_id"])
            requested = str(row["requested_disclosure_no"])
            target_rel = _safe_relative(row["target_relative_path"], requested=requested)
            legacy_rel = _safe_relative(row.get("legacy_relative_path"), requested=requested, legacy=True)
            target = cache_root / target_rel
            classification = str(row["classification"])
            db_already_ready = before_rows[row_id]["cache_status"] == "READY"
            if db_already_ready:
                if not target.is_file():
                    raise RuntimeError(STOP_PRECONDITION)
                _, target_prov = _metadata(target, row)
                existing = _load_sidecar_provenance(
                    str(target), requested, target_prov.period, target_prov.quarter,
                )
                if existing is None or existing.internal_document_id != target_prov.internal_document_id:
                    raise RuntimeError(STOP_IDENTITY)
                action = "ALREADY_READY"
            elif classification == "LEGACY_CACHE_COPY_CANDIDATE":
                source = cache_root / legacy_rel
                if not source.is_file():
                    raise RuntimeError(STOP_PRECONDITION)
                _, source_prov = _metadata(source, row)
                zip_preexisting = target.exists()
                _publish_zip(source, target, source_prov.downloaded_sha256)
                _, target_prov = _metadata(target, row)
                sidecar_preexisting = Path(str(target) + ".provenance.json").exists()
                _publish_sidecar(target, target_prov)
                action = "RECOVERED_READY" if zip_preexisting or sidecar_preexisting else "PUBLISHED_LEGACY"
            else:
                if not target.is_file():
                    raise RuntimeError(STOP_PRECONDITION)
                _, target_prov = _metadata(target, row)
                sidecar_preexisting = Path(str(target) + ".provenance.json").exists()
                _publish_sidecar(target, target_prov)
                if classification == "READY_IDENTITY_VERIFIED":
                    action = "ALREADY_READY"
                else:
                    action = "RECOVERED_READY" if sidecar_preexisting else "PUBLISHED_SIDECAR"
            final = _load_sidecar_provenance(
                str(target), requested, target_prov.period, target_prov.quarter,
            )
            if final is None or final.internal_document_id != target_prov.internal_document_id:
                raise RuntimeError(STOP_IDENTITY)
            if classification != "READY_IDENTITY_VERIFIED" and not db_already_ready:
                published_ids.append(row_id)
            actions.append({
                "manifest_row_id": row_id, "requested_disclosure_no": requested,
                "classification": classification, "action": action,
                "target_relative_path": target_rel.as_posix(),
                "zip_sha256": target_prov.downloaded_sha256,
                "sidecar_sha256": sha256_file(Path(str(target) + ".provenance.json")),
                "internal_document_id": target_prov.internal_document_id,
            })

        conn.execute("BEGIN IMMEDIATE")
        stamp = _now()
        for row_id in published_ids:
            current = before_rows[row_id]
            clear_error = current.get("error_code") in MUTABLE_CACHE_ERRORS
            cursor = conn.execute(
                "UPDATE campaign_filings SET cache_status='READY',"
                "error_code=CASE WHEN ? THEN NULL ELSE error_code END,"
                "error_stage=CASE WHEN ? THEN NULL ELSE error_stage END,"
                "error_message=CASE WHEN ? THEN NULL ELSE error_message END,updated_at=? "
                "WHERE campaign_id=? AND manifest_row_id=?",
                (clear_error, clear_error, clear_error, stamp, campaign_id, row_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(STOP_DB)
        after_in_tx = _read_db_rows(conn, campaign_id, row_ids)
        for row_id, before in before_rows.items():
            after = after_in_tx[row_id]
            if any(after[field] != before[field] for field in PROTECTED_FIELDS):
                raise RuntimeError(STOP_DB)
            expected = "READY" if row_id in published_ids else before["cache_status"]
            if after["cache_status"] != expected:
                raise RuntimeError(STOP_DB)
        conn.commit()
        after_rows = _read_db_rows(conn, campaign_id, row_ids)
        if any(after_rows[row_id]["cache_status"] != "READY" for row_id in row_ids):
            raise RuntimeError(STOP_DB)
        verification = {
            "requested": len(rows), "published": len(published_ids),
            "already_ready": len(rows) - len(published_ids), "failed": 0,
            "cache_status": dict(Counter(str(row["cache_status"]) for row in after_rows.values())),
            "network_calls": 0, "download_calls": 0,
            "zip_published": sum(a["action"] == "PUBLISHED_LEGACY" for a in actions),
            "sidecars_published": sum(a["action"] in {"PUBLISHED_LEGACY", "PUBLISHED_SIDECAR"} for a in actions),
        }
        summary = {
            "apply": True, "campaign_id": campaign_id, "started_at": started,
            "completed_at": _now(), "manifest_sha256": manifest_sha,
            "semantic_digest": _semantic_digest(actions),
            **verification,
        }
        digests = _write_audit(output_dir, {
            "cache-publish-results.json": actions,
            "cache-publish-summary.json": summary,
            "cache-publish-db-readback.json": [after_rows[row_id] for row_id in row_ids],
        })
        return {**summary, "actions": actions, "digests": digests, "output_dir": str(output_dir)}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
