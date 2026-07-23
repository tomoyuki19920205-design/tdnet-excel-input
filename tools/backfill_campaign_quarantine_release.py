#!/usr/bin/env python3
"""Plan or apply an audited, manifest-scoped V4 quarantine release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.backfill.campaign_state import (
    FreshDownloadReleaseCASFailed,
    apply_quarantine_releases,
    connect_db,
    plan_quarantine_releases,
)

STOP_GUARD = "STOP_V4_QUARANTINE_RELEASE_CLI_GUARD_FAILED"
STOP_DATABASE = "STOP_V4_QUARANTINE_RELEASE_CLI_DATABASE_CHANGED"
STOP_MANIFEST = "STOP_V4_QUARANTINE_RELEASE_CLI_MANIFEST_CHANGED"


class QuarantineReleaseCLIStop(RuntimeError):
    """Fail-closed CLI stop."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_atomic(path: Path, value: object) -> None:
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


def load_release_manifest(path: Path, expected_digest: str) -> list[dict[str, object]]:
    if not path.is_absolute() or not path.is_file() or sha256_file(path) != expected_digest.lower():
        raise QuarantineReleaseCLIStop(STOP_MANIFEST)
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise QuarantineReleaseCLIStop(STOP_MANIFEST)
            rows.append(value)
    if not rows:
        raise QuarantineReleaseCLIStop(STOP_MANIFEST)
    return rows


def run_release(
    *, campaign_db: Path, expected_db_sha256: str, campaign_id: str,
    manifest: Path, evidence_digest: str, run_id: str, output_dir: Path,
    apply: bool, confirm_count: int | None = None,
) -> dict[str, object]:
    if (
        not campaign_db.is_absolute() or not campaign_db.is_file()
        or not manifest.is_absolute() or not output_dir.is_absolute()
        or output_dir.exists() or not run_id or evidence_digest != sha256_file(manifest)
    ):
        raise QuarantineReleaseCLIStop(STOP_GUARD)
    if sha256_file(campaign_db) != expected_db_sha256.lower():
        raise QuarantineReleaseCLIStop(STOP_DATABASE)
    rows = load_release_manifest(manifest, evidence_digest)
    if confirm_count is not None and confirm_count != len(rows):
        raise QuarantineReleaseCLIStop(STOP_GUARD)
    output_dir.mkdir(parents=True, exist_ok=False)
    conn = connect_db(campaign_db)
    try:
        plan = plan_quarantine_releases(
            conn, campaign_id=campaign_id, release_rows=rows,
            manifest_digest=evidence_digest,
        )
        _write_atomic(output_dir / "release-plan.json", plan)
        if plan["conflict_count"]:
            raise QuarantineReleaseCLIStop(STOP_GUARD)
        if apply:
            result = apply_quarantine_releases(
                conn, campaign_id=campaign_id, release_rows=rows,
                manifest_digest=evidence_digest, release_run_id=run_id,
            )
        else:
            result = {"dry_run": True, "released_count": 0, "second_plan": None}
    finally:
        conn.close()
    summary = {
        "apply": apply, "campaign_id": campaign_id, "run_id": run_id,
        "target_count": len(rows), "pending_count": plan["pending_count"],
        "released_count": result["released_count"],
        "manifest_sha256": evidence_digest,
        "database_sha256_after": sha256_file(campaign_db),
        "result": result,
    }
    _write_atomic(output_dir / "release-result.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-db", type=Path, required=True)
    parser.add_argument("--expected-db-sha256", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-digest", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--confirm-count", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_release(
            campaign_db=args.campaign_db, expected_db_sha256=args.expected_db_sha256,
            campaign_id=args.campaign_id, manifest=args.manifest,
            evidence_digest=args.evidence_digest, run_id=args.run_id,
            output_dir=args.output_dir, apply=args.apply,
            confirm_count=args.confirm_count,
        )
    except (OSError, sqlite3.Error, json.JSONDecodeError, FreshDownloadReleaseCASFailed, QuarantineReleaseCLIStop) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "apply": result["apply"], "target_count": result["target_count"],
        "released_count": result["released_count"], "output_dir": str(args.output_dir),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
