"""Fail-closed orchestration for consecutive V4 Fresh Download chunks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from lib.backfill.campaign_fresh_downloader import (
    JQUANTS_SOURCE_ROUTE,
    check_production_runtime,
    is_production_ready_identity_result,
    load_provenance,
    manifest_semantic_sha256,
    sha256_file,
)
from lib.backfill.campaign_state import SCHEMA_VERSION, get_schema_version, table_exists
from tools.backfill_campaign_fresh_state_migrate import cache_tree_digest

STOP_GUARD = "STOP_V4_FRESH_LOOP_GUARD_FAILED"
STOP_PATH = "STOP_V4_FRESH_LOOP_OUTPUT_PATH_INVALID"
STOP_CHILD = "STOP_V4_FRESH_LOOP_CHILD_FAILURE"
STOP_POSTFLIGHT = "STOP_V4_FRESH_LOOP_CHILD_POSTFLIGHT_FAILED"
STOP_EXTERNAL_DB = "STOP_V4_FRESH_LOOP_DB_CHANGED_BETWEEN_CHUNKS"
STOP_EXTERNAL_CACHE = "STOP_V4_FRESH_LOOP_CACHE_CHANGED_BETWEEN_CHUNKS"
PARENT_NAME = re.compile(r"^v4-campaign-production-loop-\d{8}-\d{6}$")
CHILD_NAME = re.compile(r"^v4-campaign-production-download-\d{8}-\d{6}$")
ELIGIBLE_STATUSES = ("NOT_STARTED", "FAILED_RETRYABLE")


class FreshDownloadLoopStop(RuntimeError):
    """Structured fail-closed parent-loop stop."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write_json(path: Path, value: object) -> None:
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


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_absolute() or not path.is_file():
        raise FreshDownloadLoopStop(STOP_GUARD)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rows_digest(conn: sqlite3.Connection, table: str, campaign_id: str, excluded: set[str] | None = None) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(f"SELECT * FROM {table} WHERE campaign_id=? ORDER BY manifest_row_id", (campaign_id,)):
        current = dict(row)
        if excluded and str(current["manifest_row_id"]) in excluded:
            continue
        digest.update(_json_bytes(current))
    return digest.hexdigest()


def select_next_rows(campaign_db: Path, campaign_id: str, limit: int) -> list[dict[str, object]]:
    if not 1 <= limit <= 100:
        raise FreshDownloadLoopStop(STOP_GUARD)
    conn = _connect_ro(campaign_db)
    try:
        if get_schema_version(conn) != SCHEMA_VERSION or not table_exists(conn, "campaign_fresh_downloads"):
            raise FreshDownloadLoopStop(STOP_GUARD)
        rows = conn.execute(
            "SELECT f.*,c.requested_disclosure_no FROM campaign_fresh_downloads f "
            "JOIN campaign_filings c USING(campaign_id,manifest_row_id) "
            "WHERE f.campaign_id=? AND f.plan_classification='STANDARD_FRESH_DOWNLOAD' "
            "AND f.fresh_status IN ('NOT_STARTED','FAILED_RETRYABLE') "
            "ORDER BY f.manifest_row_id LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _counts(campaign_db: Path, campaign_id: str) -> dict[str, int]:
    conn = _connect_ro(campaign_db)
    try:
        return {str(k): int(v) for k, v in conn.execute(
            "SELECT fresh_status,COUNT(*) FROM campaign_fresh_downloads WHERE campaign_id=? GROUP BY fresh_status",
            (campaign_id,),
        )}
    finally:
        conn.close()


def _remaining_count(campaign_db: Path, campaign_id: str) -> int:
    conn = _connect_ro(campaign_db)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM campaign_fresh_downloads WHERE campaign_id=? "
            "AND plan_classification='STANDARD_FRESH_DOWNLOAD' "
            "AND fresh_status IN ('NOT_STARTED','FAILED_RETRYABLE')",
            (campaign_id,),
        ).fetchone()[0])
    finally:
        conn.close()


def _db_baseline(campaign_db: Path, campaign_id: str, target_ids: set[str]) -> dict[str, object]:
    conn = _connect_ro(campaign_db)
    try:
        return {
            "sha256": sha256_file(campaign_db),
            "campaign_filings_digest": _rows_digest(conn, "campaign_filings", campaign_id),
            "non_target_fresh_digest": _rows_digest(conn, "campaign_fresh_downloads", campaign_id, target_ids),
            "counts": _counts(campaign_db, campaign_id),
        }
    finally:
        conn.close()


def _db_postflight(campaign_db: Path, campaign_id: str, ids: Sequence[str], before: Mapping[str, object]) -> dict[str, object]:
    conn = _connect_ro(campaign_db)
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = [dict(row) for row in conn.execute(
            f"SELECT * FROM campaign_fresh_downloads WHERE campaign_id=? AND manifest_row_id IN ({placeholders}) ORDER BY manifest_row_id",
            [campaign_id, *ids],
        )]
        filing_digest = _rows_digest(conn, "campaign_filings", campaign_id)
        non_target = _rows_digest(conn, "campaign_fresh_downloads", campaign_id, set(ids))
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        pk_duplicates = int(conn.execute(
            "SELECT COUNT(*) FROM (SELECT campaign_id,manifest_row_id,COUNT(*) n FROM campaign_fresh_downloads GROUP BY 1,2 HAVING n>1)"
        ).fetchone()[0])
        orphans = int(conn.execute(
            "SELECT COUNT(*) FROM campaign_fresh_downloads f LEFT JOIN campaign_filings c USING(campaign_id,manifest_row_id) WHERE c.manifest_row_id IS NULL"
        ).fetchone()[0])
    finally:
        conn.close()
    valid = (
        len(rows) == len(ids)
        and all(row["fresh_status"] == "COMPLETE" for row in rows)
        and filing_digest == before["campaign_filings_digest"]
        and non_target == before["non_target_fresh_digest"]
        and integrity == "ok" and foreign_keys == 0 and pk_duplicates == 0 and orphans == 0
    )
    return {"valid": valid, "rows": len(rows), "counts": _counts(campaign_db, campaign_id),
            "campaign_filings_unchanged": filing_digest == before["campaign_filings_digest"],
            "non_target_fresh_unchanged": non_target == before["non_target_fresh_digest"],
            "integrity": integrity, "foreign_keys": foreign_keys, "pk_duplicates": pk_duplicates,
            "orphans": orphans, "sha256": sha256_file(campaign_db)}


def _verify_artifacts(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    verdicts: Counter[str] = Counter()
    for row in rows:
        payload = load_provenance(Path(str(row["target_zip_path"])), Path(str(row["target_provenance_path"])))
        if not is_production_ready_identity_result(payload) or payload.get("manifest_row_id") != row["manifest_row_id"]:
            raise FreshDownloadLoopStop(STOP_POSTFLIGHT)
        verdicts[str(payload["identity_verdict"])] += 1
    return {"accepted": len(rows), "production_ready": len(rows), "identity_verdicts": dict(verdicts)}


def _validate_parent_path(path: Path) -> None:
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("C:/tmp").resolve()}
    try:
        resolved_parent = path.parent.resolve()
    except OSError as exc:
        raise FreshDownloadLoopStop(STOP_PATH) from exc
    if not path.is_absolute() or path.exists() or not PARENT_NAME.fullmatch(path.name) or resolved_parent not in temp_roots:
        raise FreshDownloadLoopStop(STOP_PATH)


def _manifest(rows: Sequence[Mapping[str, object]], campaign_id: str) -> dict[str, object]:
    return {"campaign_id": campaign_id, "rows": [{
        "manifest_row_id": str(row["manifest_row_id"]),
        "plan_classification": "STANDARD_FRESH_DOWNLOAD",
        "requested_disclosure_no": str(row["requested_disclosure_no"]),
    } for row in rows]}


def default_child_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), check=False, capture_output=True, text=True)


def default_idle_minutes() -> float:
    if os.name != "nt":
        raise FreshDownloadLoopStop(STOP_GUARD)
    script = (
        "$n=Get-Date;$names='TDNET_Backfill_Segments_V4','TDNET_Nightly','TDNET_Realtime','TDNET_Reconcile';"
        "$v=foreach($x in $names){try{(Get-ScheduledTaskInfo -TaskName $x).NextRunTime}catch{}};"
        "(($v|Where-Object{$_ -gt $n}|Sort-Object|Select-Object -First 1)-$n).TotalMinutes"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def run_loop(
    *, campaign_db: Path, campaign_id: str, download_plan: Path, cache_root: Path,
    parent_output_dir: Path, chunk_size: int, max_chunks: int, min_idle_window_minutes: float,
    source_route: str, confirm_production_cache_root: str, confirm_campaign_id: str,
    confirm_max_chunks: int, apply: bool, production_apply: bool, repo_root: Path,
    child_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = default_child_runner,
    runtime_checker: Callable[[Path], Mapping[str, object]] = check_production_runtime,
    idle_minutes_provider: Callable[[], float] = default_idle_minutes,
    child_output_provider: Callable[[int], Path] | None = None,
    python_executable: str = sys.executable,
) -> dict[str, object]:
    if (not apply or not production_apply or source_route != JQUANTS_SOURCE_ROUTE or chunk_size != 100
            or not 1 <= max_chunks <= 500 or max_chunks != confirm_max_chunks
            or min_idle_window_minutes < 20 or confirm_campaign_id != campaign_id
            or confirm_production_cache_root != str(cache_root)):
        raise FreshDownloadLoopStop(STOP_GUARD)
    _validate_parent_path(parent_output_dir)
    if not campaign_db.is_file() or not download_plan.is_file() or not cache_root.is_dir():
        raise FreshDownloadLoopStop(STOP_GUARD)
    parent_output_dir.mkdir()
    for name in ("chunks", "manifests", "audit"):
        (parent_output_dir / name).mkdir()
    journal: dict[str, object] = {"phase": "CREATED", "campaign_id": campaign_id, "started_at": _now(), "finished_at": None, "chunks": []}
    journal_path = parent_output_dir / "journal.json"
    atomic_write_json(journal_path, journal)
    expected_db_sha = sha256_file(campaign_db)
    expected_cache_digest = cache_tree_digest(cache_root)
    plan_sha = sha256_file(download_plan)
    for number in range(1, max_chunks + 1):
        runtime = dict(runtime_checker(repo_root))
        idle = float(idle_minutes_provider())
        if idle < min_idle_window_minutes:
            journal.update(phase="STOPPED_IDLE_WINDOW", finished_at=_now(), idle_minutes=idle)
            atomic_write_json(journal_path, journal)
            break
        if sha256_file(campaign_db) != expected_db_sha:
            raise FreshDownloadLoopStop(STOP_EXTERNAL_DB)
        if cache_tree_digest(cache_root) != expected_cache_digest:
            raise FreshDownloadLoopStop(STOP_EXTERNAL_CACHE)
        selected = select_next_rows(campaign_db, campaign_id, chunk_size)
        if not selected:
            journal.update(phase="COMPLETE", finished_at=_now(), campaign_completed=True)
            atomic_write_json(journal_path, journal)
            break
        ids = [str(row["manifest_row_id"]) for row in selected]
        before = _db_baseline(campaign_db, campaign_id, set(ids))
        payload = _manifest(selected, campaign_id)
        manifest_path = parent_output_dir / "manifests" / f"chunk-{number:04d}.json"
        atomic_write_json(manifest_path, payload)
        manifest_sha = sha256_file(manifest_path)
        semantic_sha = manifest_semantic_sha256(payload["rows"])
        child_output = child_output_provider(number) if child_output_provider else Path("C:/tmp") / f"v4-campaign-production-download-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if child_output.exists() or not CHILD_NAME.fullmatch(child_output.name):
            raise FreshDownloadLoopStop(STOP_PATH)
        n = len(selected)
        command = [python_executable, "-B", "-m", "tools.backfill_campaign_fresh_download",
            "--campaign-db", str(campaign_db), "--campaign-id", campaign_id,
            "--campaign-db-sha256", expected_db_sha, "--download-plan", str(download_plan),
            "--download-plan-sha256", plan_sha, "--manifest-list", str(manifest_path),
            "--manifest-byte-sha256", manifest_sha, "--manifest-semantic-sha256", semantic_sha,
            "--cache-root", str(cache_root), "--output-dir", str(child_output),
            "--expected-count", str(n), "--max-items", str(n), "--confirm-production-item-count", str(n),
            "--source-route", source_route, "--confirm-production-cache-root", str(cache_root),
            "--confirm-campaign-id", campaign_id, "--apply", "--production-apply"]
        journal["phase"] = "RUNNING"
        chunk = {"chunk_number": number, "first": ids[0], "last": ids[-1], "count": n,
                 "manifest": str(manifest_path), "manifest_byte_sha256": manifest_sha,
                 "manifest_semantic_sha256": semantic_sha, "db_start_sha256": expected_db_sha,
                 "cache_start_digest": expected_cache_digest, "child_output": str(child_output),
                 "runtime": runtime, "idle_minutes": idle, "started_at": _now()}
        journal["chunks"].append(chunk)
        atomic_write_json(journal_path, journal)
        proc = child_runner(command)
        chunk["child_exit_code"] = int(proc.returncode)
        chunk["child_stdout_sha256"] = hashlib.sha256((proc.stdout or "").encode()).hexdigest()
        chunk["child_stderr_sha256"] = hashlib.sha256((proc.stderr or "").encode()).hexdigest()
        child_journal_path = child_output / "journal.json"
        if proc.returncode != 0:
            child_journal = json.loads(child_journal_path.read_text(encoding="utf-8")) if child_journal_path.is_file() else {}
            chunk.update(finished_at=_now(), failure_code=child_journal.get("failure_code"), child_phase=child_journal.get("current_phase"))
            journal.update(phase="STOPPED_CHILD_FAILURE", finished_at=_now(), failure_chunk=number)
            atomic_write_json(journal_path, journal)
            atomic_write_json(parent_output_dir / "summary.json", {"final_judgment": STOP_CHILD, "chunks_started": number})
            raise FreshDownloadLoopStop(STOP_CHILD)
        try:
            if not child_journal_path.is_file():
                raise FreshDownloadLoopStop(STOP_POSTFLIGHT)
            child_journal = json.loads(child_journal_path.read_text(encoding="utf-8"))
            post = _db_postflight(campaign_db, campaign_id, ids, before)
            artifacts = _verify_artifacts(selected)
            if child_journal.get("current_phase") != "COMPLETE" or not post["valid"] or artifacts["accepted"] != n:
                raise FreshDownloadLoopStop(STOP_POSTFLIGHT)
        except (FreshDownloadLoopStop, OSError, ValueError, json.JSONDecodeError) as exc:
            chunk.update(finished_at=_now(), failure_code=STOP_POSTFLIGHT, child_phase="POSTFLIGHT_FAILED")
            journal.update(phase="STOPPED_CHILD_FAILURE", finished_at=_now(), failure_chunk=number)
            atomic_write_json(journal_path, journal)
            atomic_write_json(parent_output_dir / "summary.json", {"final_judgment": STOP_POSTFLIGHT, "chunks_started": number})
            raise FreshDownloadLoopStop(STOP_POSTFLIGHT) from exc
        expected_db_sha = sha256_file(campaign_db)
        expected_cache_digest = cache_tree_digest(cache_root)
        row_states = list(child_journal.get("rows", {}).values())
        chunk.update(finished_at=_now(), child_phase="COMPLETE", child_run_id=child_journal.get("run_id"),
                     child_final_judgment="PASS_V4_FRESH_CHUNK", db_end_sha256=expected_db_sha,
                     cache_end_digest=expected_cache_digest,
                     stage_a=dict(Counter(row.get("stage_a_state") for row in row_states)),
                     stage_b=dict(Counter(row.get("stage_b_state") for row in row_states)),
                     fresh_before=before["counts"], fresh_after=post["counts"], postflight=post, artifacts=artifacts)
        atomic_write_json(parent_output_dir / "chunks" / f"chunk-{number:04d}.json", chunk)
        atomic_write_json(journal_path, journal)
    else:
        journal.update(phase="COMPLETE", finished_at=_now(), campaign_completed=False)
        atomic_write_json(journal_path, journal)
    remaining = _remaining_count(campaign_db, campaign_id)
    summary = {"final_judgment": "PASS_V4_FRESH_DOWNLOAD_LOOP", "phase": journal["phase"],
               "chunks_started": len(journal["chunks"]), "chunks_completed": sum(c.get("child_phase") == "COMPLETE" for c in journal["chunks"]),
               "remaining": remaining, "counts": _counts(campaign_db, campaign_id)}
    atomic_write_json(parent_output_dir / "summary.json", summary)
    digests = {str(path.relative_to(parent_output_dir)): sha256_file(path) for path in sorted(parent_output_dir.rglob("*")) if path.is_file() and path.name != "digests.json"}
    atomic_write_json(parent_output_dir / "digests.json", digests)
    return {"journal": journal, "summary": summary, "output_dir": str(parent_output_dir)}
