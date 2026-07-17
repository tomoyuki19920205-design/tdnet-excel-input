#!/usr/bin/env python3
"""Safely migrate a temporary V4 campaign database to Fresh state schema v2."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.backfill.campaign_fresh_downloader import (
    FreshDownloaderStop,
    _has_reparse_component,
    _has_unsafe_raw_path,
    _is_under,
    load_provenance,
    sha256_file,
)
from lib.backfill.campaign_state import (
    FRESH_DOWNLOAD_PLAN_CLASSES,
    FRESH_DOWNLOAD_STATUSES,
    FreshStateMigrationConflict,
    connect_db,
    get_schema_version,
    migrate_fresh_download_state,
    select_next_fresh_downloads,
    table_exists,
)

STOP_UNSAFE_DB = "STOP_V4_FRESH_STATE_MIGRATION_CLI_UNSAFE_DB_PATH"
STOP_INPUT = "STOP_V4_FRESH_STATE_MIGRATION_CLI_INPUT_CHANGED"
STOP_ARTIFACT = "STOP_V4_FRESH_STATE_MIGRATION_CLI_ARTIFACT_MISMATCH"
STOP_LEGACY = "STOP_V4_FRESH_STATE_MIGRATION_CLI_LEGACY_RESTORE_FAILED"
STOP_VERIFY = "STOP_V4_FRESH_STATE_MIGRATION_CLI_VERIFICATION_FAILED"


class FreshStateMigrationCLIStop(RuntimeError):
    """Fail-closed structured CLI stop."""


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _semantic_digest(rows: Iterable[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_json_bytes(dict(row)))
    return digest.hexdigest()


def cache_tree_digest(root: Path) -> str:
    entries = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda p: p.relative_to(root).as_posix()):
        entries.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)})
    return hashlib.sha256(_json_bytes(entries)).hexdigest()


def _temp_roots() -> tuple[Path, ...]:
    values = [Path(tempfile.gettempdir()).resolve(), Path("C:/tmp").resolve()]
    return tuple(dict.fromkeys(values))


def _safe_input(path: Path, *, kind: str) -> Path:
    if _has_unsafe_raw_path(path) or not path.is_absolute() or not path.exists() or _has_reparse_component(path):
        raise FreshStateMigrationCLIStop(f"{STOP_INPUT}:{kind}")
    return path.resolve()


def _safe_write_path(path: Path, *, repo_root: Path, must_exist: bool) -> Path:
    if _has_unsafe_raw_path(path) or not path.is_absolute() or _has_reparse_component(path):
        raise FreshStateMigrationCLIStop(STOP_UNSAFE_DB)
    resolved = path.resolve(strict=must_exist)
    if _is_under(resolved, repo_root) or not any(_is_under(resolved, root) for root in _temp_roots()):
        raise FreshStateMigrationCLIStop(STOP_UNSAFE_DB)
    return resolved


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _sidecars_absent(path: Path) -> bool:
    return not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


def _load_plan(path: Path, campaign_id: str, expected_total: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FreshStateMigrationCLIStop(STOP_INPUT) from exc
            if not isinstance(row, dict):
                raise FreshStateMigrationCLIStop(STOP_INPUT)
            row_id = str(row.get("manifest_row_id") or "")
            classification = str(row.get("plan_classification") or "")
            if (
                row.get("campaign_id") != campaign_id or not row_id
                or classification not in FRESH_DOWNLOAD_PLAN_CLASSES
                or row.get("download_allowed") is not True
                or not row.get("target_zip_path") or not row.get("target_provenance_path")
            ):
                raise FreshStateMigrationCLIStop(STOP_INPUT)
            rows.append(dict(row))
    ids = [str(row["manifest_row_id"]) for row in rows]
    if len(rows) != expected_total or ids != sorted(ids) or len(set(ids)) != expected_total:
        raise FreshStateMigrationCLIStop(STOP_INPUT)
    return rows


def _db_snapshot(path: Path, campaign_id: str) -> dict[str, object]:
    conn = _connect_read_only(path)
    try:
        version = get_schema_version(conn)
        filings = [dict(row) for row in conn.execute(
            "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id", (campaign_id,)
        )]
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        fresh_exists = table_exists(conn, "campaign_fresh_downloads")
    finally:
        conn.close()
    return {
        "schema_version": version, "filings": filings, "filing_count": len(filings),
        "filings_digest": _semantic_digest(filings), "integrity_check": integrity,
        "foreign_key_check": foreign_keys, "fresh_table_exists": fresh_exists,
    }


def _load_complete_artifacts(
    cache_root: Path, plan_rows: list[dict[str, object]], campaign_id: str,
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    artifacts: dict[str, dict[str, object]] = {}
    evidence: list[dict[str, object]] = []
    for row in plan_rows:
        row_id = str(row["manifest_row_id"])
        directory = cache_root / row_id
        zip_path, provenance_path = directory / "xbrl.zip", directory / "provenance.json"
        if not zip_path.exists() and not provenance_path.exists():
            continue
        if not zip_path.is_file() or not provenance_path.is_file() or _has_reparse_component(directory):
            raise FreshStateMigrationCLIStop(STOP_ARTIFACT)
        try:
            payload = load_provenance(zip_path, provenance_path)
        except (FreshDownloaderStop, OSError, ValueError) as exc:
            raise FreshStateMigrationCLIStop(STOP_ARTIFACT) from exc
        if any((
            payload.get("campaign_id") != campaign_id,
            payload.get("manifest_row_id") != row_id,
            payload.get("requested_disclosure_no") != row.get("requested_disclosure_no"),
            payload.get("plan_classification") != row.get("plan_classification"),
            payload.get("identity_verdict") != "official_linked_xbrl_match",
            payload.get("source_route") != "JQUANTS_TD_FILES",
        )):
            raise FreshStateMigrationCLIStop(STOP_ARTIFACT)
        artifact = {
            "zip_sha256": payload.get("zip_sha256"),
            "internal_document_id": payload.get("internal_document_id"),
            "zip_internal_ticker": payload.get("zip_internal_ticker"),
            "zip_internal_period": payload.get("zip_internal_period"),
            "zip_internal_quarter": payload.get("zip_internal_quarter"),
            "identity_verdict": payload.get("identity_verdict"),
            "run_id": payload.get("run_id"),
            "downloaded_at_utc": payload.get("downloaded_at_utc") or payload.get("downloaded_at"),
        }
        if any(value in {None, ""} for value in artifact.values()):
            raise FreshStateMigrationCLIStop(STOP_ARTIFACT)
        artifacts[row_id] = artifact
        evidence.append({
            "manifest_row_id": row_id, "zip_path": str(zip_path),
            "provenance_path": str(provenance_path), **artifact,
        })
    return artifacts, evidence


def _verify_database(
    db_path: Path, backup_rows: list[dict[str, object]], plan_rows: list[dict[str, object]],
    artifacts: Mapping[str, Mapping[str, object]], campaign_id: str,
) -> dict[str, object]:
    conn = _connect_read_only(db_path)
    try:
        version = get_schema_version(conn)
        filings = [dict(row) for row in conn.execute(
            "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id", (campaign_id,)
        )]
        fresh = [dict(row) for row in conn.execute(
            "SELECT * FROM campaign_fresh_downloads WHERE campaign_id=? ORDER BY manifest_row_id", (campaign_id,)
        )] if table_exists(conn, "campaign_fresh_downloads") else []
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        orphan = int(conn.execute(
            "SELECT COUNT(*) FROM campaign_fresh_downloads f LEFT JOIN campaign_filings c "
            "ON c.campaign_id=f.campaign_id AND c.manifest_row_id=f.manifest_row_id "
            "WHERE f.campaign_id=? AND c.manifest_row_id IS NULL", (campaign_id,)
        ).fetchone()[0]) if fresh else 0
        pk_duplicates = int(conn.execute(
            "SELECT COUNT(*) FROM (SELECT campaign_id,manifest_row_id,COUNT(*) n FROM campaign_fresh_downloads "
            "WHERE campaign_id=? GROUP BY campaign_id,manifest_row_id HAVING n>1)", (campaign_id,)
        ).fetchone()[0]) if fresh else 0
        next_rows = select_next_fresh_downloads(conn, campaign_id, limit=100) if fresh else []
        indexes = [str(row[1]) for row in conn.execute("PRAGMA index_list(campaign_fresh_downloads)")] if fresh else []
    finally:
        conn.close()
    fresh_by_id = {str(row["manifest_row_id"]): row for row in fresh}
    plan_by_id = {str(row["manifest_row_id"]): row for row in plan_rows}
    artifact_matches = sum(
        1 for row_id, artifact in artifacts.items()
        if row_id in fresh_by_id and all((
            fresh_by_id[row_id].get("fresh_status") == "COMPLETE",
            fresh_by_id[row_id].get("source_route") == "JQUANTS_TD_FILES",
            fresh_by_id[row_id].get("attempt_count") == 1,
            fresh_by_id[row_id].get("artifact_zip_sha256") == artifact.get("zip_sha256"),
            fresh_by_id[row_id].get("artifact_internal_document_id") == artifact.get("internal_document_id"),
            fresh_by_id[row_id].get("artifact_ticker") == artifact.get("zip_internal_ticker"),
            fresh_by_id[row_id].get("artifact_period") == artifact.get("zip_internal_period"),
            fresh_by_id[row_id].get("artifact_quarter") == artifact.get("zip_internal_quarter"),
            fresh_by_id[row_id].get("identity_verdict") == artifact.get("identity_verdict"),
            fresh_by_id[row_id].get("target_zip_path") == plan_by_id[row_id].get("target_zip_path"),
            fresh_by_id[row_id].get("target_provenance_path") == plan_by_id[row_id].get("target_provenance_path"),
        ))
    )
    status = Counter(str(row["fresh_status"]) for row in fresh)
    classes = Counter(str(row["plan_classification"]) for row in fresh)
    prior_identity = Counter(str(row["prior_identity_status"]) for row in fresh)
    prior_cache = Counter(str(row["prior_cache_status"]) for row in fresh)
    prior_overall = Counter(str(row["prior_overall_status"]) for row in fresh)
    changed = sum(left != right for left, right in zip(filings, backup_rows))
    legacy = {
        "missing": max(0, len(backup_rows) - len(filings)), "extra": max(0, len(filings) - len(backup_rows)),
        "changed": changed, "actual_digest": _semantic_digest(filings),
        "expected_digest": _semantic_digest(backup_rows),
    }
    next_cache = Counter(str(row["prior_cache_status"]) for row in next_rows)
    return {
        "schema_version": version, "filing_count": len(filings), "fresh_count": len(fresh),
        "fresh_status": dict(sorted(status.items())), "plan_classification": dict(sorted(classes.items())),
        "prior_identity_status": dict(sorted(prior_identity.items())),
        "prior_cache_status": dict(sorted(prior_cache.items())),
        "prior_overall_status": dict(sorted(prior_overall.items())),
        "legacy_restore": legacy, "complete_artifact_matches": artifact_matches,
        "integrity_check": integrity, "foreign_key_check": foreign_keys,
        "orphan_count": orphan, "pk_duplicate_count": pk_duplicates, "indexes": indexes,
        "next_100": {
            "count": len(next_rows), "first": next_rows[0]["manifest_row_id"] if next_rows else None,
            "last": next_rows[-1]["manifest_row_id"] if next_rows else None,
            "prior_cache_status": dict(sorted(next_cache.items())),
        },
    }


def _write_outputs(output_dir: Path, values: Mapping[str, object]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, value in values.items():
        (output_dir / name).write_bytes(_json_bytes(value))
    digests = {name: sha256_file(output_dir / name) for name in sorted(values)}
    (output_dir / "digests.json").write_bytes(_json_bytes(digests))
    return digests


def run_migration(
    *, campaign_db: Path, campaign_id: str, legacy_backup_db: Path,
    download_plan: Path, cache_root: Path, migration_run_id: str,
    output_dir: Path, campaign_db_sha256: str, legacy_backup_sha256: str,
    download_plan_sha256: str, cache_tree_digest_value: str,
    expected_total: int, expected_complete: int, expected_not_started: int,
    expected_quarantined: int, apply: bool, repo_root: Path,
) -> dict[str, object]:
    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    db = _safe_write_path(campaign_db, repo_root=repo_root, must_exist=True)
    output = _safe_write_path(output_dir, repo_root=repo_root, must_exist=False)
    if (
        output.exists() or not migration_run_id or expected_total <= 0
        or expected_complete + expected_not_started + expected_quarantined != expected_total
    ):
        raise FreshStateMigrationCLIStop(STOP_INPUT)
    backup = _safe_input(legacy_backup_db, kind="backup")
    plan_path = _safe_input(download_plan, kind="plan")
    cache = _safe_input(cache_root, kind="cache")
    actual = {
        "campaign_db": sha256_file(db), "legacy_backup": sha256_file(backup),
        "download_plan": sha256_file(plan_path), "cache_tree": cache_tree_digest(cache),
    }
    expected = {
        "campaign_db": campaign_db_sha256.lower(), "legacy_backup": legacy_backup_sha256.lower(),
        "download_plan": download_plan_sha256.lower(), "cache_tree": cache_tree_digest_value.lower(),
    }
    if actual != expected or not _sidecars_absent(db) or not _sidecars_absent(backup):
        raise FreshStateMigrationCLIStop(STOP_INPUT)
    plan_rows = _load_plan(plan_path, campaign_id, expected_total)
    current = _db_snapshot(db, campaign_id)
    prior = _db_snapshot(backup, campaign_id)
    if any((
        current["filing_count"] != expected_total, prior["filing_count"] != expected_total,
        prior["schema_version"] != "1", prior["fresh_table_exists"],
        current["integrity_check"] != "ok", prior["integrity_check"] != "ok",
        current["foreign_key_check"] != 0, prior["foreign_key_check"] != 0,
    )):
        raise FreshStateMigrationCLIStop(STOP_INPUT)
    artifacts, artifact_evidence = _load_complete_artifacts(cache, plan_rows, campaign_id)
    if len(artifacts) != expected_complete:
        raise FreshStateMigrationCLIStop(STOP_ARTIFACT)
    current_by_id = {str(row["manifest_row_id"]): row for row in current["filings"]}
    prior_by_id = {str(row["manifest_row_id"]): row for row in prior["filings"]}
    changed_ids = {row_id for row_id in current_by_id if current_by_id[row_id] != prior_by_id.get(row_id)}
    if current["schema_version"] == "1" and changed_ids != set(artifacts):
        raise FreshStateMigrationCLIStop(STOP_ARTIFACT)
    if current["schema_version"] == "2" and current["filings"] != prior["filings"]:
        raise FreshStateMigrationCLIStop(STOP_LEGACY)
    if current["schema_version"] not in {"1", "2"}:
        raise FreshStateMigrationCLIStop(STOP_INPUT)
    preflight = {
        "status": "ALREADY_MIGRATED" if current["schema_version"] == "2" else "READY_TO_MIGRATE",
        "apply": apply, "campaign_id": campaign_id, "expected_total": expected_total,
        "complete_candidates": len(artifacts), "changed_ids": sorted(changed_ids), "digests": actual,
    }
    plan_summary = {
        "rows": len(plan_rows), "classifications": dict(sorted(Counter(
            str(row["plan_classification"]) for row in plan_rows
        ).items())), "complete_ids": sorted(artifacts),
    }
    migration = {"status": preflight["status"], "rows": expected_total, "restored_rows": 0}
    if apply:
        if current["schema_version"] != "1" or current["fresh_table_exists"]:
            raise FreshStateMigrationCLIStop("STOP_V4_FRESH_STATE_TABLE_EXISTING_CONFLICT")
        conn = connect_db(db)
        backup_conn = _connect_read_only(backup)
        try:
            migration = migrate_fresh_download_state(
                conn, backup_conn=backup_conn, campaign_id=campaign_id,
                plan_rows=plan_rows, complete_artifacts=artifacts,
                migration_run_id=migration_run_id, migrated_at=started,
                journal_path=str(output / "execution.json"),
            )
        finally:
            conn.close(); backup_conn.close()
    verification = _verify_database(db, prior["filings"], plan_rows, artifacts, campaign_id) if (
        apply or current["schema_version"] == "2"
    ) else None
    if verification is not None:
        expected_status = {"COMPLETE": expected_complete, "NOT_STARTED": expected_not_started, "QUARANTINED": expected_quarantined}
        if any((
            verification["schema_version"] != "2", verification["filing_count"] != expected_total,
            verification["fresh_count"] != expected_total, verification["fresh_status"] != expected_status,
            verification["complete_artifact_matches"] != expected_complete,
            verification["legacy_restore"]["changed"] != 0,
            verification["legacy_restore"]["missing"] != 0,
            verification["legacy_restore"]["extra"] != 0,
            verification["integrity_check"] != "ok", verification["foreign_key_check"] != 0,
            verification["orphan_count"] != 0, verification["pk_duplicate_count"] != 0,
        )):
            raise FreshStateMigrationCLIStop(STOP_VERIFY)
    finished = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    execution = {"started_at": started, "finished_at": finished, "apply": apply, "migration_run_id": migration_run_id, "network_calls": 0}
    outputs: dict[str, object] = {"preflight.json": preflight, "migration-plan.json": plan_summary, "execution.json": execution}
    if verification is not None:
        outputs.update({
            "migration-summary.json": migration,
            "fresh-status-summary.json": {"fresh_status": verification["fresh_status"], "plan_classification": verification["plan_classification"]},
            "complete-five-verification.json": artifact_evidence,
            "legacy-restore-verification.json": verification["legacy_restore"],
            "next-100-selection.json": verification["next_100"],
            "database-verification.json": verification,
        })
    _write_outputs(output, outputs)
    return {"status": migration["status"], "apply": apply, "output_dir": str(output), "verification": verification}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-db", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--legacy-backup-db", type=Path, required=True)
    parser.add_argument("--download-plan", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--migration-run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--campaign-db-sha256", required=True)
    parser.add_argument("--legacy-backup-sha256", required=True)
    parser.add_argument("--download-plan-sha256", required=True)
    parser.add_argument("--cache-tree-digest", required=True)
    parser.add_argument("--expected-total", type=int, required=True)
    parser.add_argument("--expected-complete", type=int, required=True)
    parser.add_argument("--expected-not-started", type=int, required=True)
    parser.add_argument("--expected-quarantined", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        result = run_migration(
            campaign_db=args.campaign_db, campaign_id=args.campaign_id,
            legacy_backup_db=args.legacy_backup_db, download_plan=args.download_plan,
            cache_root=args.cache_root, migration_run_id=args.migration_run_id,
            output_dir=args.output_dir, campaign_db_sha256=args.campaign_db_sha256,
            legacy_backup_sha256=args.legacy_backup_sha256,
            download_plan_sha256=args.download_plan_sha256,
            cache_tree_digest_value=args.cache_tree_digest,
            expected_total=args.expected_total, expected_complete=args.expected_complete,
            expected_not_started=args.expected_not_started,
            expected_quarantined=args.expected_quarantined, apply=args.apply,
            repo_root=repo_root,
        )
    except (FreshStateMigrationCLIStop, FreshStateMigrationConflict, FreshDownloaderStop, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
