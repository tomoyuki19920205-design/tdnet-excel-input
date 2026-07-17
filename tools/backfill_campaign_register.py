"""Register canonical V4 campaign candidates into an explicitly temporary SQLite DB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from lib.backfill.campaign_state import (
    SCHEMA_VERSION, connect_db, create_campaign, create_campaign_filings,
    initialize_schema, transaction,
)

STOP_INPUT = "STOP_V4_CAMPAIGN_TEMP_REGISTER_INPUT_INVALID"
STOP_PATH = "STOP_V4_CAMPAIGN_TEMP_REGISTER_UNSAFE_DB_PATH"
STOP_EXISTS = "STOP_V4_CAMPAIGN_ALREADY_REGISTERED"
STOP_DIRTY = "STOP_V4_CAMPAIGN_TEMP_REGISTER_DIRTY_WORKTREE"

_DIGESTED = (
    "campaign.json", "manifest-schema.json", "registration-candidates.jsonl",
    "registration-classification.jsonl", "registration-summary.json",
    "requested-id-duplicate-groups.json", "rejected-rows.jsonl",
)
_FIELDS = (
    "campaign_id", "manifest_row_id", "state_filing_id", "requested_disclosure_no",
    "company_code", "normalized_company_code", "source_url", "normalized_xbrl_url",
    "disclosure_date", "expected_period", "expected_quarter", "document_type",
    "internal_document_id", "zip_sha256", "zip_internal_ticker", "zip_internal_period",
    "zip_internal_quarter", "run_id", "worker_version", "extractor_version",
    "extractor_route", "code_sha", "registration_status", "identity_status",
    "cache_status", "extraction_status", "sqlite_save_status", "canonical_save_status",
    "supabase_save_status", "overall_status", "error_code", "error_stage",
    "error_message", "retryable",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _validate_temp_db_path(path: Path, repo_root: Path) -> None:
    if not path.is_absolute() or _is_under(path, repo_root):
        raise RuntimeError(STOP_PATH)
    temp_roots = [Path(r"C:\tmp"), Path(tempfile.gettempdir())]
    if not any(_is_under(path, root) for root in temp_roots):
        raise RuntimeError(STOP_PATH)


def _git_provenance(repo_root: Path) -> dict[str, object]:
    def run(args: list[str]) -> bytes:
        try:
            return subprocess.run(["git", *args], cwd=repo_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("STOP_V4_CAMPAIGN_GIT_PROVENANCE_UNAVAILABLE") from exc
    head = run(["rev-parse", "HEAD"]).decode().strip()
    branch = run(["branch", "--show-current"]).decode().strip()
    tracked = run(["diff", "--binary", "--no-ext-diff"])
    staged = run(["diff", "--cached", "--binary", "--no-ext-diff"])
    if tracked or staged:
        raise RuntimeError(STOP_DIRTY)
    return {"git_head": head, "git_branch": branch, "working_tree_code_present": False, "tracked_diff_present": False, "staged_diff_present": False, "registration_tool_code_sha": head}


def load_candidates(campaign_dir: Path) -> tuple[dict, list[dict], dict]:
    if not campaign_dir.is_dir():
        raise RuntimeError(STOP_INPUT)
    try:
        digests = _json(campaign_dir / "digests.json")
        for name in _DIGESTED:
            if not (campaign_dir / name).is_file() or digests.get(name) != _sha(campaign_dir / name):
                raise RuntimeError(STOP_INPUT)
        campaign = _json(campaign_dir / "campaign.json")
        summary = _json(campaign_dir / "registration-summary.json")
        candidates = [json.loads(line) for line in (campaign_dir / "registration-candidates.jsonl").read_text(encoding="utf-8").splitlines()]
        classifications = [json.loads(line) for line in (campaign_dir / "registration-classification.jsonl").read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(STOP_INPUT) from exc
    ids = [row.get("manifest_row_id") for row in candidates]
    campaign_id = campaign.get("campaign_id")
    if (
        len(candidates) != 46218 or len(classifications) != 46218
        or len(set(ids)) != 46218 or any(not value for value in ids)
        or any(row.get("campaign_id") != campaign_id for row in candidates)
        or any(row.get("code_sha") != campaign.get("code_sha") for row in candidates)
        or summary.get("input_count") != 46218 or summary.get("output_count") != 46218
        or summary.get("classification_counts") != {"METADATA_INCOMPLETE": 46218}
        or summary.get("registration_candidate_count") != 46218 or summary.get("rejected_count") != 0
        or any(row.get("expected_quarter") is not None for row in candidates)
        or any(row.get("classification") != "METADATA_INCOMPLETE" or row.get("error_code") != "MISSING_EXPECTED_QUARTER" for row in candidates)
        or (campaign_dir / "rejected-rows.jsonl").read_bytes().strip()
    ):
        raise RuntimeError(STOP_INPUT)
    return campaign, candidates, summary


def _campaign_values(campaign: dict) -> dict:
    return {"campaign_id": campaign["campaign_id"], "campaign_name": campaign["campaign_name"], "manifest_path": campaign["manifest_path"], "manifest_sha256": campaign["manifest_sha256"], "manifest_record_count": campaign["manifest_record_count"], "code_sha": campaign["code_sha"], "worker_version": campaign["worker_version"], "status": "READY"}


def _readback(db_path: Path, candidates: list[dict]) -> dict:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = [dict(row) for row in conn.execute("SELECT * FROM campaign_filings ORDER BY manifest_row_id")]
        expected = [{key: row.get(key) for key in _FIELDS} for row in candidates]
        actual = [{key: row.get(key) for key in _FIELDS} for row in rows]
        return {
            "campaign_count": conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0],
            "filing_count": len(rows), "semantic_changed_rows": sum(a != b for a, b in zip(expected, actual)),
            "expected_quarter_null": sum(row["expected_quarter"] is None for row in rows),
            "requested_id_distinct": conn.execute("SELECT COUNT(DISTINCT requested_disclosure_no) FROM campaign_filings").fetchone()[0],
            "statuses": {name: conn.execute(f"SELECT COUNT(*) FROM campaign_filings WHERE {name} = ?", (value,)).fetchone()[0] for name, value in {"registration_status":"REGISTERED", "identity_status":"UNVERIFIED", "cache_status":"UNKNOWN", "extraction_status":"NOT_STARTED", "overall_status":"REGISTERED"}.items()},
            "retryable_true": conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE retryable = 1").fetchone()[0],
            "error_code_nonnull": conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE error_code IS NOT NULL").fetchone()[0],
            "internal_document_id_nonnull": conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE internal_document_id IS NOT NULL").fetchone()[0],
            "zip_sha256_nonnull": conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE zip_sha256 IS NOT NULL").fetchone()[0],
            "foreign_key_check": [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")],
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "schema_version": conn.execute("SELECT schema_version FROM campaign_schema_metadata").fetchone()[0],
        }
    finally:
        conn.close()


def register(*, campaign_dir: Path, db_path: Path, output_dir: Path, apply: bool) -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    _validate_temp_db_path(db_path, repo_root)
    campaign, candidates, summary = load_candidates(campaign_dir)
    if not apply:
        return {"dry_run": True, "db_created": False, "input_count": len(candidates)}
    provenance = _git_provenance(repo_root)
    if db_path.exists():
        raise RuntimeError(STOP_EXISTS)
    output_dir.mkdir(parents=True, exist_ok=False)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_db(db_path)
    try:
        initialize_schema(conn)
        with transaction(conn):
            create_campaign(conn, _campaign_values(campaign))
            create_campaign_filings(conn, candidates)
    except Exception:
        conn.close()
        raise
    else:
        conn.close()
    verification = _readback(db_path, candidates)
    if verification["filing_count"] != len(candidates) or verification["semantic_changed_rows"] or verification["foreign_key_check"] or verification["integrity_check"] != "ok":
        raise RuntimeError("STOP_V4_CAMPAIGN_TEMP_REGISTER_SEMANTIC_MISMATCH")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in {"registration-execution.json": {"apply": True, **provenance}, "registration-summary.json": summary, "database-verification.json": verification, "semantic-diff.json": {"missing": 0, "extra": 0, "changed_rows": verification["semantic_changed_rows"], "changed_fields": 0}, "schema.sql": "schema_version=" + SCHEMA_VERSION}.items():
        path = output_dir / name
        if name.endswith(".json"):
            path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        else:
            path.write_text(value + "\n", encoding="utf-8", newline="\n")
    command = "backfill_campaign_register --apply"
    (output_dir / "registration-command.txt").write_text(command + "\n", encoding="utf-8", newline="\n")
    audit_files = [p for p in output_dir.iterdir() if p.name != "digests.json"]
    (output_dir / "digests.json").write_text(json.dumps({p.name: _sha(p) for p in sorted(audit_files)}, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return {"db_path": str(db_path), "output_dir": str(output_dir), **verification, **provenance}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register canonical V4 campaign candidates into a temporary SQLite DB")
    parser.add_argument("--campaign-dir", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = register(campaign_dir=Path(args.campaign_dir), db_path=Path(args.db_path), output_dir=Path(args.output_dir), apply=args.apply)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
