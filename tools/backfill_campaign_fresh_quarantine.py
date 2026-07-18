#!/usr/bin/env python3
"""Apply one evidence-backed V4 Fresh runtime quarantine transition."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.backfill.campaign_fresh_downloader import (
    _has_reparse_component,
    _has_unsafe_raw_path,
    _is_under,
    sha256_file,
)
from lib.backfill.campaign_state import (
    FreshDownloadQuarantineCASFailed,
    apply_fresh_download_quarantine,
    connect_db,
)

STOP_GUARD = "STOP_V4_FRESH_QUARANTINE_CLI_GUARD_FAILED"
STOP_EVIDENCE = "STOP_V4_FRESH_QUARANTINE_CLI_EVIDENCE_CHANGED"
STOP_DATABASE = "STOP_V4_FRESH_QUARANTINE_CLI_DATABASE_CHANGED"
STOP_PATH = "STOP_V4_FRESH_QUARANTINE_CLI_UNSAFE_PATH"
STOP_OUTPUT = "STOP_V4_FRESH_QUARANTINE_CLI_OUTPUT_FAILED"
OUTPUT_RE = re.compile(r"^v4-fresh-quarantine-\d{8}-\d{6}$")


class FreshQuarantineCLIStop(RuntimeError):
    """Fail-closed CLI stop."""


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _temp_roots() -> tuple[Path, ...]:
    return tuple(dict.fromkeys((Path(tempfile.gettempdir()).resolve(), Path(r"C:\tmp").resolve())))


def _is_temp_path(path: Path) -> bool:
    return any(_is_under(path, root) for root in _temp_roots())


def _safe_database_path(path: Path, repo_root: Path) -> Path:
    if _has_unsafe_raw_path(path) or not path.is_absolute() or not path.is_file() or _has_reparse_component(path):
        raise FreshQuarantineCLIStop(STOP_PATH)
    resolved = path.resolve()
    production = (repo_root / "data" / "backfill_campaign_v4.db").resolve()
    if resolved != production and not _is_temp_path(resolved):
        raise FreshQuarantineCLIStop(STOP_PATH)
    if _is_under(resolved, repo_root) and resolved != production:
        raise FreshQuarantineCLIStop(STOP_PATH)
    return resolved


def _safe_evidence_path(path: Path) -> Path:
    if _has_unsafe_raw_path(path) or not path.is_absolute() or not path.is_dir() or _has_reparse_component(path):
        raise FreshQuarantineCLIStop(STOP_PATH)
    resolved = path.resolve()
    if not _is_temp_path(resolved):
        raise FreshQuarantineCLIStop(STOP_PATH)
    return resolved


def _safe_output_path(path: Path) -> Path:
    if (
        _has_unsafe_raw_path(path) or not path.is_absolute() or path.exists()
        or _has_reparse_component(path.parent) or OUTPUT_RE.fullmatch(path.name) is None
    ):
        raise FreshQuarantineCLIStop(STOP_PATH)
    resolved = path.resolve(strict=False)
    if not _is_temp_path(resolved):
        raise FreshQuarantineCLIStop(STOP_PATH)
    return resolved


def verify_evidence_tree(path: Path, expected_sha256: str) -> dict[str, object]:
    digest_path = path / "digests.json"
    if not digest_path.is_file():
        raise FreshQuarantineCLIStop(STOP_EVIDENCE)
    try:
        payload = json.loads(digest_path.read_text(encoding="utf-8"))
        files = payload["files"]
        recorded = str(payload["tree_digest_excluding_digests_json"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FreshQuarantineCLIStop(STOP_EVIDENCE) from exc
    if (
        not isinstance(files, list) or not files or recorded != expected_sha256.lower()
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_sha256)
    ):
        raise FreshQuarantineCLIStop(STOP_EVIDENCE)
    normalized: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict):
            raise FreshQuarantineCLIStop(STOP_EVIDENCE)
        relative = Path(str(item.get("path") or ""))
        candidate = path / relative
        if (
            relative.is_absolute() or ".." in relative.parts or not candidate.is_file()
            or candidate.resolve().parent != (path / relative.parent).resolve()
        ):
            raise FreshQuarantineCLIStop(STOP_EVIDENCE)
        current = {"path": relative.as_posix(), "size": candidate.stat().st_size, "sha256": sha256_file(candidate)}
        if current != item:
            raise FreshQuarantineCLIStop(STOP_EVIDENCE)
        normalized.append(current)
    actual = hashlib.sha256(_json_bytes(normalized)).hexdigest()
    if actual != recorded:
        raise FreshQuarantineCLIStop(STOP_EVIDENCE)
    return {"path": str(path), "tree_sha256": actual, "digests_sha256": sha256_file(digest_path), "files": len(normalized)}


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: object) -> None:
    _atomic_write(path, _json_bytes(value))


def _update_journal(path: Path, journal: dict[str, object], phase: str, **values: object) -> None:
    journal.update(values)
    journal["current_phase"] = phase
    _write_json(path, journal)


def _write_final_outputs(output: Path, result: Mapping[str, object], command: str) -> dict[str, str]:
    values: dict[str, bytes] = {
        "before-row.json": _json_bytes(result["before"]),
        "after-row.json": _json_bytes(result["after"]),
        "invariants.json": _json_bytes(result["invariants"]),
        "command-sanitized.txt": (command.rstrip() + "\n").encode("utf-8"),
    }
    for name, payload in values.items():
        _atomic_write(output / name, payload)
    digests = {name: sha256_file(output / name) for name in sorted((*values, "journal.json"))}
    _write_json(output / "digests.json", digests)
    return digests


def run_quarantine(
    *, campaign_db: Path, campaign_db_sha256: str, campaign_id: str,
    manifest_row_id: str, requested_document_id: str, expected_status: str,
    expected_attempt_count: int, reason_code: str, failure_stage: str,
    source_route: str, http_status: int, evidence_path: Path,
    evidence_sha256: str, output_dir: Path, confirm_campaign_id: str,
    confirm_manifest_row_id: str, apply: bool, production_apply: bool,
    repo_root: Path, command_sanitized: str = "",
) -> dict[str, object]:
    if (
        not apply or not production_apply or campaign_id != confirm_campaign_id
        or manifest_row_id != confirm_manifest_row_id
        or expected_status != "NOT_STARTED" or expected_attempt_count != 0
        or reason_code != "TD_FILES_DISCNO_NOT_FOUND" or failure_stage != "STAGE_A"
        or source_route != "JQUANTS_TD_FILES" or http_status != 404
    ):
        raise FreshQuarantineCLIStop(STOP_GUARD)
    database = _safe_database_path(campaign_db, repo_root)
    evidence = _safe_evidence_path(evidence_path)
    output = _safe_output_path(output_dir)
    if sha256_file(database) != campaign_db_sha256.lower():
        raise FreshQuarantineCLIStop(STOP_DATABASE)
    evidence_result = verify_evidence_tree(evidence, evidence_sha256)
    output.mkdir(parents=False, exist_ok=False)
    run_id = "fresh-quarantine-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    journal_path = output / "journal.json"
    journal: dict[str, object] = {
        "run_id": run_id, "campaign_id": campaign_id,
        "manifest_row_id": manifest_row_id, "requested_document_id": requested_document_id,
        "campaign_db_start_sha256": campaign_db_sha256.lower(),
        "evidence": evidence_result, "started_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "current_phase": "CREATED", "failure_code": None,
    }
    _write_json(journal_path, journal)
    try:
        _update_journal(journal_path, journal, "EVIDENCE_VERIFIED")
        _update_journal(journal_path, journal, "DB_PENDING")
        conn = connect_db(database)
        try:
            result = apply_fresh_download_quarantine(
                conn, campaign_id=campaign_id, manifest_row_id=manifest_row_id,
                requested_document_id=requested_document_id, expected_status=expected_status,
                expected_attempt_count=expected_attempt_count, reason_code=reason_code,
                failure_stage=failure_stage, source_route=source_route,
                http_status=http_status, evidence_path=str(evidence),
                evidence_sha256=evidence_sha256, run_id=run_id,
            )
        finally:
            conn.close()
        _update_journal(journal_path, journal, "DB_COMMITTED")
        _update_journal(
            journal_path, journal, "COMPLETE",
            finished_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        digests = _write_final_outputs(output, result, command_sanitized)
        return {"status": "QUARANTINED", "run_id": run_id, "output_dir": str(output), "result": result, "digests": digests}
    except Exception as exc:
        _update_journal(
            journal_path, journal, "FAILED", failure_code=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-db", type=Path, required=True)
    parser.add_argument("--campaign-db-sha256", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--manifest-row-id", required=True)
    parser.add_argument("--requested-document-id", required=True)
    parser.add_argument("--expected-status", required=True)
    parser.add_argument("--expected-attempt-count", type=int, required=True)
    parser.add_argument("--reason-code", required=True)
    parser.add_argument("--failure-stage", required=True)
    parser.add_argument("--source-route", required=True)
    parser.add_argument("--http-status", type=int, required=True)
    parser.add_argument("--evidence-path", type=Path, required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--confirm-campaign-id", required=True)
    parser.add_argument("--confirm-manifest-row-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--production-apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = "python -m tools.backfill_campaign_fresh_quarantine " + " ".join(
        f"--{name.replace('_', '-')} {value}" for name, value in vars(args).items()
        if name not in {"apply", "production_apply"}
    ) + (" --apply" if args.apply else "") + (" --production-apply" if args.production_apply else "")
    try:
        result = run_quarantine(
            campaign_db=args.campaign_db, campaign_db_sha256=args.campaign_db_sha256,
            campaign_id=args.campaign_id, manifest_row_id=args.manifest_row_id,
            requested_document_id=args.requested_document_id,
            expected_status=args.expected_status, expected_attempt_count=args.expected_attempt_count,
            reason_code=args.reason_code, failure_stage=args.failure_stage,
            source_route=args.source_route, http_status=args.http_status,
            evidence_path=args.evidence_path, evidence_sha256=args.evidence_sha256,
            output_dir=args.output_dir, confirm_campaign_id=args.confirm_campaign_id,
            confirm_manifest_row_id=args.confirm_manifest_row_id,
            apply=args.apply, production_apply=args.production_apply,
            repo_root=Path(__file__).resolve().parents[1], command_sanitized=command,
        )
    except (FreshQuarantineCLIStop, FreshDownloadQuarantineCASFailed, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({key: result[key] for key in ("status", "run_id", "output_dir")}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
