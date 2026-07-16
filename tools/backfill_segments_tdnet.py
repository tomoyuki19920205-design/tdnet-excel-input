#!/usr/bin/env python3
"""tools/backfill_segments_tdnet.py — TDNET 並列バックフィル CLI (Step 5: Benchmark)

Usage:
    # Phase 1 互換
    python tools/backfill_segments_tdnet.py --limit 100 --workers 8

    # Phase 2 (XBRL/PDF 分離)
    python tools/backfill_segments_tdnet.py --phase2 --xbrl-workers 6 --pdf-workers 3

    # Resume
    python tools/backfill_segments_tdnet.py --resume --phase2 --retry-quarantine
"""
from __future__ import annotations

import os

import argparse
import csv
import hashlib
import json
import logging
import shutil
import sqlite3
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# .env から環境変数を自動ロード（OPENAI_API_KEY など）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 未インストール時はスキップ

from lib.backfill.listing_provider import CompositeListingProvider
from lib.backfill.listing_sources.tdnet_html import TdnetHtmlListingProvider
from lib.backfill.state_store import BackfillStateStore
from lib.backfill.worker import process_one_filing
from lib.backfill.batch_upsert import batch_upsert_segments
from lib.backfill.metrics import BackfillMetrics, BackfillMetricsV2
from lib.backfill.jsonl_logger import RunLogger, generate_run_id
from lib.backfill.filing_selector import should_process_for_segment_backfill

logger = logging.getLogger("backfill")


_ISOLATED_PATH_ERROR = "STOP_ISOLATED_SQLITE_PATH_ESCAPE"
_PENDING_SET_MISMATCH = "STOP_MANIFEST_PENDING_SET_MISMATCH"
_ISOLATED_SEED_INVALID_MODE = "STOP_BACKFILL_ISOLATED_SEED_INVALID_MODE"
_ISOLATED_SEED_DB_INVALID = "STOP_BACKFILL_ISOLATED_SEED_DB_INVALID"
_ISOLATED_SEED_CACHE_MISSING = "STOP_BACKFILL_ISOLATED_SEED_CACHE_MISSING"
_ISOLATED_SEED_UNSAFE_PATH = "STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH"
_ISOLATED_SEED_COPY_MISMATCH = "STOP_BACKFILL_ISOLATED_SEED_COPY_MISMATCH"
_ISOLATED_SEED_SOURCE_MUTATED = "STOP_BACKFILL_ISOLATED_SEED_SOURCE_MUTATED"
_ISOLATED_SEED_MANIFEST_ID_UNRESOLVED = (
    "STOP_BACKFILL_ISOLATED_SEED_MANIFEST_ID_UNRESOLVED"
)
_ISOLATED_SEED_STOP_CODES = frozenset({
    _ISOLATED_SEED_INVALID_MODE,
    _ISOLATED_SEED_DB_INVALID,
    _ISOLATED_SEED_CACHE_MISSING,
    _ISOLATED_SEED_UNSAFE_PATH,
    _ISOLATED_SEED_COPY_MISMATCH,
    _ISOLATED_SEED_MANIFEST_ID_UNRESOLVED,
    _ISOLATED_SEED_SOURCE_MUTATED,
})
_MANIFEST_REPLAY_INVALID_MODE = "STOP_BACKFILL_MANIFEST_REPLAY_INVALID_MODE"
_MANIFEST_REPLAY_MANIFEST_INVALID = "STOP_BACKFILL_MANIFEST_REPLAY_MANIFEST_INVALID"
_MANIFEST_REPLAY_STATE_MISMATCH = "STOP_BACKFILL_MANIFEST_REPLAY_STATE_MISMATCH"
_MANIFEST_REPLAY_TRANSACTION_FAILED = "STOP_BACKFILL_MANIFEST_REPLAY_TRANSACTION_FAILED"
_MANIFEST_REPLAY_SCOPE_BREACH = "STOP_BACKFILL_MANIFEST_REPLAY_SCOPE_BREACH"
_MANIFEST_REPLAY_PENDING_SCOPE_MISMATCH = "STOP_BACKFILL_MANIFEST_REPLAY_PENDING_SCOPE_MISMATCH"


class IsolatedSeedStop(RuntimeError):
    """A recognized isolated-seed stop that is safe to expose from the CLI."""

    def __init__(self, code: str, stage: str, detail: dict[str, object] | None = None):
        self.code = code
        self.stage = stage
        self.detail = detail or {}
        super().__init__(code)


class ManifestReplayStop(RuntimeError):
    """A machine-readable stop for the manifest-scoped replay gate."""

    def __init__(self, code: str, detail: dict[str, object] | None = None):
        self.code = code
        self.detail = detail or {}
        super().__init__(code)


def _emit_manifest_replay_stop(stop: ManifestReplayStop) -> None:
    print(
        json.dumps(
            {
                "status": "stopped",
                "stop_code": stop.code,
                "detail": stop.detail,
                "worker_started": False,
                "canonical_sync_enabled": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _safe_isolated_seed_stop_detail(code: str, message: str) -> dict[str, object]:
    """Keep only allowlisted, machine-readable detail from an existing STOP string."""
    detail: dict[str, object] = {}
    for key in ("path_role", "reason", "change_kind"):
        marker = f"{key}="
        if marker in message:
            value = message.split(marker, 1)[1].split(";", 1)[0].split()[0]
            if value.replace("_", "").replace("-", "").isalnum():
                detail[key] = value
    if "change_kind" not in detail and "change_types=" in message:
        value = message.split("change_types=", 1)[1].split(";", 1)[0].split()[0]
        if value in {"created", "deleted", "unsafe_path"}:
            detail["change_kind"] = value
    if "source_type=" in message:
        value = message.split("source_type=", 1)[1].split(";", 1)[0].split()[0]
        if value.replace("_", "").replace("-", "").isalnum():
            detail["source_kind"] = value
    if "identifiers=" in message:
        value = message.split("identifiers=", 1)[1].split(";", 1)[0].split()[0]
        if value and not any(token in value for token in ("\\", ":", "..")):
            detail["safe_identifier"] = value
    if "target_count=" in message:
        value = message.split("target_count=", 1)[1].split(";", 1)[0].split()[0]
        if value.isdigit():
            detail["affected_count"] = int(value)
    if code == _ISOLATED_SEED_INVALID_MODE:
        detail["reason"] = "invalid_mode"
    return detail


def _as_isolated_seed_stop(exc: RuntimeError, *, stage: str) -> IsolatedSeedStop | None:
    """Classify only the established isolated-seed STOP code prefix."""
    message = str(exc)
    for code in _ISOLATED_SEED_STOP_CODES:
        if message == code or message.startswith(f"{code}:"):
            return IsolatedSeedStop(
                code,
                stage,
                _safe_isolated_seed_stop_detail(code, message),
            )
    return None


def _emit_isolated_seed_stop(stop: IsolatedSeedStop) -> None:
    print(
        json.dumps(
            {
                "status": "stopped",
                "stop_code": stop.code,
                "stage": stop.stage,
                "detail": stop.detail,
                "worker_started": False,
                "canonical_sync_enabled": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_snapshot(path: Path) -> tuple[int, int, str]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns, _sha256_file(path)


def _path_has_reparse_component(path: Path) -> bool:
    current = path
    while True:
        try:
            info = os.lstat(current)
        except (FileNotFoundError, OSError):
            return True
        attributes = getattr(info, "st_file_attributes", None)
        if attributes is None:
            return True
        try:
            is_symlink = current.is_symlink()
        except OSError:
            return True
        if is_symlink or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return True
        if current.parent == current:
            return False
        current = current.parent


def _assert_isolated_path_safe(raw_path: Path, *, path_role: str) -> None:
    """Fail closed when an existing raw path component is a reparse point."""
    current = Path(raw_path)
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if current.parent == current:
                raise RuntimeError(
                    f"{_ISOLATED_SEED_UNSAFE_PATH}: path_role={path_role}; reason=missing_root"
                )
            current = current.parent
            continue
        except OSError:
            raise RuntimeError(
                f"{_ISOLATED_SEED_UNSAFE_PATH}: path_role={path_role}; reason=inspection_failed"
            ) from None

        attributes = getattr(info, "st_file_attributes", None)
        if attributes is None:
            raise RuntimeError(
                f"{_ISOLATED_SEED_UNSAFE_PATH}: path_role={path_role}; reason=attributes_unavailable"
            )
        try:
            is_symlink = current.is_symlink()
        except OSError:
            raise RuntimeError(
                f"{_ISOLATED_SEED_UNSAFE_PATH}: path_role={path_role}; reason=inspection_failed"
            ) from None
        if is_symlink or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(
                f"{_ISOLATED_SEED_UNSAFE_PATH}: path_role={path_role}; reason=reparse_component"
            )
        if current.parent == current:
            return
        current = current.parent


def _assert_isolated_destination_safe(
    destination: Path,
    *,
    run_root: Path,
    path_role: str,
) -> None:
    """Recheck a raw isolated destination immediately before it is opened or created."""
    _assert_isolated_path_safe(run_root, path_role="run_root")
    _assert_isolated_path_safe(destination.parent, path_role=f"{path_role}_parent")
    _assert_isolated_path_safe(destination, path_role=path_role)
    root = run_root.resolve(strict=True)
    resolved_destination = destination.resolve(strict=False)
    if root not in resolved_destination.parents or destination.exists():
        raise RuntimeError(
            f"{_ISOLATED_SEED_UNSAFE_PATH}: path_role={path_role}; reason=unsafe_destination"
        )


def _validate_seed_source_path(
    raw_path: str,
    *,
    run_root: Path,
    require_file: bool,
    missing_code: str,
) -> Path:
    source_path = Path(raw_path)
    if not source_path.is_absolute():
        raise RuntimeError(f"{_ISOLATED_SEED_UNSAFE_PATH}: seed path must be absolute")
    if not source_path.exists():
        raise RuntimeError(f"{missing_code}: seed source does not exist")
    if _path_has_reparse_component(source_path):
        raise RuntimeError(f"{_ISOLATED_SEED_UNSAFE_PATH}: reparse seed path")
    if require_file and not source_path.is_file():
        raise RuntimeError(f"{missing_code}: seed source is not a normal file")
    if not require_file and not source_path.is_dir():
        raise RuntimeError(f"{missing_code}: seed source is not a directory")
    source = source_path.resolve(strict=True)
    root = run_root.resolve(strict=True)
    if source == root or root in source.parents or source in root.parents:
        raise RuntimeError(f"{_ISOLATED_SEED_UNSAFE_PATH}: seed overlaps run-root")
    return source


def _sqlite_shm_snapshot(source: Path) -> dict[str, object]:
    """Capture only the safety properties of SQLite's ephemeral shared memory."""
    candidate = Path(f"{source}-shm")
    try:
        exists = candidate.exists()
    except OSError:
        return {"exists": None, "safe": False, "size": None}
    if not exists:
        return {"exists": False, "safe": True, "size": None}
    try:
        return {
            "exists": True,
            "safe": candidate.is_file() and not _path_has_reparse_component(candidate),
            "size": candidate.stat().st_size,
        }
    except OSError:
        return {"exists": True, "safe": False, "size": None}


def _assert_sqlite_shm_snapshot_safe(snapshot: dict[str, object]) -> None:
    if snapshot["exists"] is None or not snapshot["safe"]:
        _raise_source_mutated(
            "decision_db_shm", {"shm": {"unsafe_path"}}
        )


def _sqlite_source_snapshot(source: Path) -> dict[str, object]:
    """Keep persistent DB/WAL audits separate from SQLite's ephemeral SHM."""
    shm = _sqlite_shm_snapshot(source)
    _assert_sqlite_shm_snapshot_safe(shm)
    wal_path = Path(f"{source}-wal")
    wal = None
    if wal_path.exists():
        if not wal_path.is_file() or _path_has_reparse_component(wal_path):
            _raise_source_mutated("decision_db_wal", {"wal": {"unsafe_path"}})
        wal = _file_snapshot(wal_path)
    return {
        "persistent": {
            "database": _file_snapshot(source),
            "wal": wal,
        },
        "ephemeral": {"shm": shm},
    }


def _source_snapshot_changes(
    path: Path,
    before: tuple[int, int, str] | None,
) -> set[str]:
    try:
        exists = path.exists()
    except OSError:
        return {"existence"}
    if (before is None) != (not exists):
        return {"existence"}
    if before is None:
        return set()
    try:
        after = _file_snapshot(path)
    except OSError:
        return {"sha"}
    changes = set()
    if before[0] != after[0]:
        changes.add("size")
    if before[1] != after[1]:
        changes.add("mtime")
    if before[2] != after[2]:
        changes.add("sha")
    return changes


def _raise_source_mutated(
    source_type: str,
    changes_by_identifier: dict[str, set[str]],
) -> None:
    changed = {
        identifier: changes
        for identifier, changes in changes_by_identifier.items()
        if changes
    }
    if not changed:
        return
    identifiers = ",".join(sorted(changed))
    change_types = ",".join(sorted({item for values in changed.values() for item in values}))
    raise RuntimeError(
        f"{_ISOLATED_SEED_SOURCE_MUTATED}: "
        f"source_type={source_type} identifiers={identifiers} "
        f"change_types={change_types} target_count={len(changed)}"
    )


def _assert_sqlite_source_unchanged(
    source: Path,
    before: dict[str, object],
) -> None:
    persistent = before["persistent"]
    _raise_source_mutated("decision_db", {
        "database": _source_snapshot_changes(source, persistent["database"]),
    })
    wal_path = Path(f"{source}-wal")
    wal_before = persistent["wal"]
    if wal_path.exists() and (not wal_path.is_file() or _path_has_reparse_component(wal_path)):
        _raise_source_mutated("decision_db_wal", {"wal": {"unsafe_path"}})
    wal_after = _file_snapshot(wal_path) if wal_path.exists() else None
    wal_before_empty = wal_before is None or wal_before[0] == 0
    wal_after_empty = wal_after is None or wal_after[0] == 0
    if not (wal_before_empty and wal_after_empty):
        _raise_source_mutated("decision_db_wal", {
            "wal": _source_snapshot_changes(wal_path, wal_before),
        })
    shm_before = before["ephemeral"]["shm"]
    shm_after = _sqlite_shm_snapshot(source)
    if not shm_after["safe"]:
        _raise_source_mutated(
            "decision_db_shm", {"shm": {"unsafe_path"}}
        )


def _assert_cache_sources_unchanged(
    copy_plan: list[tuple[Path, Path, tuple[int, int, str]]],
) -> None:
    _raise_source_mutated(
        "cache",
        {
            f"{destination_file.parent.name}/{destination_file.name}":
                _source_snapshot_changes(source_file, before)
            for source_file, destination_file, before in copy_plan
        },
    )


def _validate_isolated_decision_db_seed(
    source_path: str,
    *,
    run_root: Path,
) -> tuple[Path, dict[str, object]]:
    source = _validate_seed_source_path(
        source_path,
        run_root=run_root,
        require_file=True,
        missing_code=_ISOLATED_SEED_DB_INVALID,
    )
    before = _sqlite_source_snapshot(source)
    source_conn = None
    try:
        source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        source_conn.execute("PRAGMA schema_version").fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"{_ISOLATED_SEED_DB_INVALID}: {exc}") from exc
    finally:
        try:
            if source_conn is not None:
                source_conn.close()
        finally:
            _assert_sqlite_source_unchanged(source, before)
    return source, before


def _copy_isolated_decision_db(
    source: Path,
    before: dict[str, object],
    *,
    destination: Path,
    run_root: Path,
) -> tuple[str, str]:
    _assert_isolated_destination_safe(
        destination,
        run_root=run_root,
        path_role="decision_db",
    )
    destination = destination.resolve(strict=False)
    root = run_root.resolve(strict=True)
    if root not in destination.parents or source == destination or destination.exists():
        raise RuntimeError(f"{_ISOLATED_SEED_UNSAFE_PATH}: unsafe decision DB destination")

    source_conn = destination_conn = None
    try:
        source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        destination_conn = sqlite3.connect(str(destination))
        source_conn.backup(destination_conn)
        integrity = destination_conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"{_ISOLATED_SEED_COPY_MISMATCH}: SQLite integrity check")
    except RuntimeError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"{_ISOLATED_SEED_DB_INVALID}: {exc}") from exc
    finally:
        try:
            if destination_conn is not None:
                destination_conn.close()
        finally:
            try:
                if source_conn is not None:
                    source_conn.close()
            finally:
                _assert_sqlite_source_unchanged(source, before)

    if not destination.is_file():
        raise RuntimeError(f"{_ISOLATED_SEED_COPY_MISMATCH}: decision DB destination missing")
    return before["persistent"]["database"][2], _sha256_file(destination)


def _manifest_requested_ids(filing_list_path: str) -> list[str]:
    filings = _load_filing_list(filing_list_path)
    requested_ids = [str(getattr(filing, "requested_disclosure_no", "") or "") for filing in filings]
    if (
        not requested_ids
        or any(
            not requested_id
            or Path(requested_id).name != requested_id
            or requested_id in {".", ".."}
            for requested_id in requested_ids
        )
        or len(set(requested_ids)) != len(requested_ids)
    ):
        raise RuntimeError(
            f"{_ISOLATED_SEED_MANIFEST_ID_UNRESOLVED}: requested disclosure ID"
        )
    return requested_ids


def _validate_isolated_cache_seed(
    source_path: str,
    *,
    destination_root: Path,
    run_root: Path,
    requested_ids: list[str],
) -> list[tuple[Path, Path, tuple[int, int, str]]]:
    source_root = _validate_seed_source_path(
        source_path,
        run_root=run_root,
        require_file=False,
        missing_code=_ISOLATED_SEED_CACHE_MISSING,
    )
    required_names = ("xbrl.zip", "xbrl.zip.provenance.json")
    copy_plan: list[tuple[Path, Path, tuple[int, int, str]]] = []
    for requested_id in requested_ids:
        source_dir = source_root / requested_id
        if not source_dir.is_dir():
            raise RuntimeError(
                f"{_ISOLATED_SEED_CACHE_MISSING}: {requested_id} cache directory"
            )
        if _path_has_reparse_component(source_dir):
            raise RuntimeError(
                f"{_ISOLATED_SEED_UNSAFE_PATH}: {requested_id} cache directory"
            )
        destination_dir = destination_root / requested_id
        if destination_dir.exists():
            raise RuntimeError(f"{_ISOLATED_SEED_UNSAFE_PATH}: cache destination exists")
        for name in required_names:
            source_file = source_dir / name
            if not source_file.is_file():
                raise RuntimeError(
                    f"{_ISOLATED_SEED_CACHE_MISSING}: {requested_id}/{name}"
                )
            if _path_has_reparse_component(source_file):
                raise RuntimeError(
                    f"{_ISOLATED_SEED_UNSAFE_PATH}: {requested_id}/{name}"
                )
            copy_plan.append((source_file, destination_dir / name, _file_snapshot(source_file)))
    return copy_plan


def _copy_isolated_cache(
    copy_plan: list[tuple[Path, Path, tuple[int, int, str]]],
    *,
    destination_root: Path,
    requested_ids: list[str],
    run_root: Path,
) -> None:
    try:
        for requested_id in requested_ids:
            destination_dir = destination_root / requested_id
            _assert_isolated_destination_safe(
                destination_dir,
                run_root=run_root,
                path_role="filing_cache",
            )
            destination_dir.mkdir(parents=True, exist_ok=False)
        for source_file, destination_file, before in copy_plan:
            _assert_isolated_destination_safe(
                destination_file,
                run_root=run_root,
                path_role=("xbrl_zip" if destination_file.name == "xbrl.zip" else "sidecar"),
            )
            shutil.copyfile(source_file, destination_file)
            if _sha256_file(destination_file) != before[2]:
                raise RuntimeError(
                    f"{_ISOLATED_SEED_COPY_MISMATCH}: {source_file.name}"
                )
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(
            f"{_ISOLATED_SEED_COPY_MISMATCH}: cache copy failed"
        ) from exc
    finally:
        _assert_cache_sources_unchanged(copy_plan)


def _prepare_isolated_seeds(
    *,
    decision_db_source: str | None,
    cache_root_source: str | None,
    decision_db_destination: Path,
    cache_destination_root: Path,
    run_root: Path,
    filing_list_path: str,
) -> dict:
    summary = {
        "isolated_seed_decision_db_used": False,
        "isolated_seed_decision_db_source_sha256": None,
        "isolated_seed_decision_db_destination_sha256": None,
        "isolated_seed_cache_used": False,
        "isolated_seed_cache_filing_count": 0,
        "isolated_seed_cache_requested_ids": [],
        "isolated_seed_copy_verified": False,
    }
    if not decision_db_source and not cache_root_source:
        return summary

    requested_ids = _manifest_requested_ids(filing_list_path) if cache_root_source else []
    decision_seed = (
        _validate_isolated_decision_db_seed(decision_db_source, run_root=run_root)
        if decision_db_source
        else None
    )
    cache_copy_plan = (
        _validate_isolated_cache_seed(
            cache_root_source,
            destination_root=cache_destination_root,
            run_root=run_root,
            requested_ids=requested_ids,
        )
        if cache_root_source
        else None
    )

    if decision_db_source:
        source_sha, destination_sha = _copy_isolated_decision_db(
            decision_seed[0],
            decision_seed[1],
            destination=decision_db_destination,
            run_root=run_root,
        )
        summary.update({
            "isolated_seed_decision_db_used": True,
            "isolated_seed_decision_db_source_sha256": source_sha,
            "isolated_seed_decision_db_destination_sha256": destination_sha,
        })
    if cache_root_source:
        _copy_isolated_cache(
            cache_copy_plan,
            destination_root=cache_destination_root,
            requested_ids=requested_ids,
            run_root=run_root,
        )
        summary.update({
            "isolated_seed_cache_used": True,
            "isolated_seed_cache_filing_count": len(requested_ids),
            "isolated_seed_cache_requested_ids": requested_ids,
        })
    if decision_seed:
        _assert_sqlite_source_unchanged(decision_seed[0], decision_seed[1])
    if cache_copy_plan:
        _assert_cache_sources_unchanged(cache_copy_plan)
    summary["isolated_seed_copy_verified"] = True
    return summary


def _validate_isolated_write_paths(
    *,
    run_root: str,
    decision_db_path: str,
    state_db_path: str,
    log_jsonl_path: str,
    filing_list_path: str,
) -> dict[str, Path]:
    """Resolve every mutable isolated path and reject escapes before opening it."""
    if not all((run_root, decision_db_path, state_db_path, log_jsonl_path, filing_list_path)):
        raise RuntimeError(f"{_ISOLATED_PATH_ERROR}: missing isolated path")

    raw_root = Path(run_root)
    if not raw_root.is_absolute():
        raise RuntimeError(f"{_ISOLATED_SEED_UNSAFE_PATH}: path_role=run_root; reason=not_absolute")
    _assert_isolated_path_safe(raw_root, path_role="run_root")
    root = raw_root.resolve(strict=False)
    project_root = Path(_PROJECT_ROOT).resolve(strict=False)
    production_roots = (
        (project_root / "logs").resolve(strict=False),
        (project_root / "data").resolve(strict=False),
    )
    if root in production_roots or any(prod in root.parents for prod in production_roots):
        raise RuntimeError(f"{_ISOLATED_PATH_ERROR}: run-root is under production storage")

    raw_paths = {
        "decision_db": Path(decision_db_path),
        "state_db": Path(state_db_path),
        "log_jsonl": Path(log_jsonl_path),
        "filing_list": Path(filing_list_path),
    }
    for label, raw_path in raw_paths.items():
        _assert_isolated_path_safe(raw_path, path_role=label)

    resolved = {
        "run_root": root,
        "decision_db": Path(decision_db_path).resolve(strict=False),
        "state_db": Path(state_db_path).resolve(strict=False),
        "log_jsonl": Path(log_jsonl_path).resolve(strict=False),
        "filing_list": Path(filing_list_path).resolve(strict=False),
    }
    for label in ("decision_db", "state_db", "log_jsonl"):
        if root not in resolved[label].parents:
            raise RuntimeError(f"{_ISOLATED_PATH_ERROR}: {label} escapes run-root")

    input_root = (root / "input").resolve(strict=False)
    if input_root not in resolved["filing_list"].parents:
        raise RuntimeError(f"{_ISOLATED_PATH_ERROR}: filing-list escapes run-root/input")

    production_decision_db = (project_root / "decision_db.db").resolve(strict=False)
    production_state_db = (project_root / "data" / "backfill_state.db").resolve(strict=False)
    if resolved["decision_db"] == production_decision_db:
        raise RuntimeError(f"{_ISOLATED_PATH_ERROR}: production decision DB")
    if resolved["state_db"] == production_state_db:
        raise RuntimeError(f"{_ISOLATED_PATH_ERROR}: production state DB")
    return resolved


# ============================================================
# PRO Market 除外判定
# ============================================================

def _is_pro_market_filing(filing) -> bool:
    """TOKYO PRO Market 等 PRO Market 銅柀4の開示か判定する。"""
    market = ""
    if isinstance(filing, dict):
        market = (
            filing.get("market")
            or filing.get("market_name")
            or filing.get("market_segment")
            or filing.get("exchange")
            or ""
        )
    else:
        market = (
            getattr(filing, "market", None)
            or getattr(filing, "market_name", None)
            or getattr(filing, "market_segment", None)
            or getattr(filing, "exchange", None)
            or ""
        )
    s = str(market).replace("\u3000", " ").strip().upper()
    if not s:
        return False
    return (
        "PRO MARKET" in s
        or "TOKYO PRO MARKET" in s
        or s == "TPM"
        or "TPM " in s
    )


# ============================================================
# Filing list / manifest helpers
# ============================================================

def _load_filing_list(path: str) -> list:
    """JSON / JSONL / CSV の manifest ファイルを読み込み、FilingInfo リストを返す。

    サポート形式:
      - .json: [{"filing_id": ..., "ticker": ..., ...}, ...]
      - .jsonl: 1行1 JSON オブジェクト
      - .csv: ヘッダ付き CSV

    必須フィールド: filing_id, ticker, title, disclosure_date, doc_url
    """
    from lib.backfill.listing_sources.base import FilingInfo

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"filing-list not found: {path}")

    raw_records: list[dict] = []

    if p.suffix == ".json":
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                raw_records = data
            elif isinstance(data, dict) and "filings" in data:
                raw_records = data["filings"]
            else:
                raise ValueError(f"Unsupported JSON structure in {path}")
    elif p.suffix == ".jsonl":
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw_records.append(json.loads(line))
    elif p.suffix == ".csv":
        with open(p, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_records.append(row)
    else:
        raise ValueError(f"Unsupported file type: {p.suffix} (use .json/.jsonl/.csv)")

    filings = []
    for rec in raw_records:
        fi = FilingInfo(
            filing_id=rec["filing_id"],
            requested_disclosure_no=rec.get("requested_disclosure_no", ""),
            expected_period=rec.get("expected_period", ""),
            expected_quarter=rec.get("expected_quarter", ""),
            ticker=rec.get("ticker", ""),
            title=rec.get("title", ""),
            disclosure_date=rec.get("disclosure_date", ""),
            doc_url=rec.get("doc_url", ""),
            xbrl_url=rec.get("xbrl_url") or None,
            doc_type=rec.get("doc_type", "financial_statement"),
            company_name=rec.get("company_name", ""),
            published_at=rec.get("published_at", ""),
            listing_source=rec.get("listing_source", "manifest"),
            has_xbrl=bool(rec.get("has_xbrl", False)),
        )
        filings.append(fi)

    logger.info(f"[backfill] loaded {len(filings)} filings from manifest: {path}")
    print(f"[backfill] loaded {len(filings)} filings from manifest: {path}")
    return filings


def _manifest_replay_targets(filings: list) -> tuple[list[str], list[str]]:
    """Validate manifest scope and return (state filing IDs, requested IDs)."""
    filing_ids = [str(getattr(filing, "filing_id", "") or "") for filing in filings]
    requested_ids = [str(getattr(filing, "requested_disclosure_no", "") or "") for filing in filings]
    valid_requested = all(
        requested_id
        and Path(requested_id).name == requested_id
        and requested_id not in {".", ".."}
        for requested_id in requested_ids
    )
    if (
        not filing_ids
        or not valid_requested
        or any(not filing_id for filing_id in filing_ids)
        or len(set(filing_ids)) != len(filing_ids)
        or len(set(requested_ids)) != len(requested_ids)
    ):
        raise ManifestReplayStop(
            _MANIFEST_REPLAY_MANIFEST_INVALID,
            {"requested_count": len(requested_ids)},
        )
    return filing_ids, requested_ids


def _manifest_replay_non_target_digest(conn: sqlite3.Connection, filing_ids: list[str]) -> tuple[int, str]:
    """Digest state metadata outside the explicit manifest scope."""
    placeholders = ",".join("?" * len(filing_ids))
    rows = conn.execute(
        "SELECT filing_id, status, stage, attempt_count, "
        "COALESCE(last_error, ''), COALESCE(last_error_stage, '') "
        f"FROM filing_state WHERE filing_id NOT IN ({placeholders}) ORDER BY filing_id",
        filing_ids,
    ).fetchall()
    payload = json.dumps([tuple(row) for row in rows], ensure_ascii=False, separators=(",", ":"))
    return len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _replay_manifest_done(store: BackfillStateStore, filings: list) -> dict[str, object]:
    """Atomically requeue only manifest rows already at done/extracted."""
    filing_ids, requested_ids = _manifest_replay_targets(filings)
    conn = store.conn
    summary: dict[str, object] = {
        "manifest_replay_enabled": True,
        "manifest_replay_requested_count": len(requested_ids),
        "manifest_replay_matched_count": 0,
        "manifest_replay_requeued_count": 0,
        "manifest_replay_pending_count": 0,
        "manifest_replay_non_target_changed_count": 0,
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" * len(filing_ids))
        rows = conn.execute(
            "SELECT filing_id, status, stage FROM filing_state "
            f"WHERE filing_id IN ({placeholders}) ORDER BY filing_id",
            filing_ids,
        ).fetchall()
        status_counts: dict[str, int] = {}
        stage_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            stage_counts[row["stage"]] = stage_counts.get(row["stage"], 0) + 1
        summary["manifest_replay_matched_count"] = len(rows)
        if len(rows) != len(filing_ids) or any(
            row["status"] != "done" or row["stage"] != "extracted" for row in rows
        ):
            conn.rollback()
            raise ManifestReplayStop(
                _MANIFEST_REPLAY_STATE_MISMATCH,
                {
                    "requested_count": len(requested_ids),
                    "matched_count": len(rows),
                    "mismatched_count": len(filing_ids) - len(rows) + sum(
                        row["status"] != "done" or row["stage"] != "extracted" for row in rows
                    ),
                    "status_counts": status_counts,
                    "stage_counts": stage_counts,
                },
            )
        before_count, before_digest = _manifest_replay_non_target_digest(conn, filing_ids)
        conn.execute(
            "UPDATE filing_state SET status = 'queued', stage = 'listing' "
            f"WHERE filing_id IN ({placeholders}) AND status = 'done' AND stage = 'extracted'",
            filing_ids,
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]
        if changed != len(filing_ids):
            conn.rollback()
            raise ManifestReplayStop(
                _MANIFEST_REPLAY_TRANSACTION_FAILED,
                {"requested_count": len(requested_ids), "matched_count": len(rows)},
            )
        readback = conn.execute(
            "SELECT filing_id, status, stage FROM filing_state "
            f"WHERE filing_id IN ({placeholders}) ORDER BY filing_id",
            filing_ids,
        ).fetchall()
        if len(readback) != len(filing_ids) or any(
            row["status"] != "queued" or row["stage"] != "listing" for row in readback
        ):
            conn.rollback()
            raise ManifestReplayStop(
                _MANIFEST_REPLAY_TRANSACTION_FAILED,
                {"requested_count": len(requested_ids), "matched_count": len(rows)},
            )
        after_count, after_digest = _manifest_replay_non_target_digest(conn, filing_ids)
        if (before_count, before_digest) != (after_count, after_digest):
            conn.rollback()
            raise ManifestReplayStop(
                _MANIFEST_REPLAY_SCOPE_BREACH,
                {"requested_count": len(requested_ids), "matched_count": len(rows)},
            )
        conn.commit()
        summary["manifest_replay_requeued_count"] = changed
        return summary
    except ManifestReplayStop:
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise ManifestReplayStop(
            _MANIFEST_REPLAY_TRANSACTION_FAILED,
            {"requested_count": len(requested_ids), "reason": type(exc).__name__},
        ) from exc


def _run_requeue_only(
    *,
    filing_list_path: str,
    state_db_path: str,
    requested_disclosure_no: str,
    expected_stage: str | None = None,
    expected_error: str | None = None,
) -> dict:
    """Requeue exactly one existing manifest filing without starting workers."""
    filings = _load_filing_list(filing_list_path)
    matches = [
        filing for filing in filings
        if str(filing.requested_disclosure_no) == str(requested_disclosure_no)
    ]
    if not matches:
        raise RuntimeError("STOP_REQUEUE_REQUESTED_ID_NOT_IN_MANIFEST")
    if len(matches) != 1:
        raise RuntimeError("STOP_REQUEUE_REQUESTED_ID_NOT_UNIQUE")

    filing = matches[0]
    store = BackfillStateStore(state_db_path)
    try:
        result = store.requeue_single_filing(
            filing.filing_id,
            expected_status="quarantined",
            expected_stage=expected_stage,
            expected_error=expected_error,
        )
    finally:
        store.close()

    detail = {
        "event": "requeue_only",
        "requested_disclosure_no": str(requested_disclosure_no),
        "filing_id": filing.filing_id,
        "before_status": result["before"]["status"],
        "after_status": result["after"]["status"],
        "changed_fields": [
            "status", "stage", "last_error", "last_error_stage", "review_hint",
            "started_at", "finished_at", "duration_ms",
        ],
        "preserved_attempt_count": result["after"]["attempt_count"],
    }
    logger.info("[backfill] requeue-only %s", json.dumps(detail, ensure_ascii=False))
    print(json.dumps(detail, ensure_ascii=False))
    return detail


def _save_manifest(filings: list, run_id: str, log_dir: str = "logs") -> str:
    """対象 filing の manifest を JSON で保存する。

    Returns:
        保存先パス
    """
    Path(log_dir).mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = f"{log_dir}/filing_manifest_{run_id}_{ts}.json"

    records = []
    for fi in filings:
        records.append({
            "filing_id": fi.filing_id,
            "requested_disclosure_no": fi.requested_disclosure_no,
            "expected_period": fi.expected_period,
            "expected_quarter": fi.expected_quarter,
            "ticker": fi.ticker,
            "disclosure_date": fi.disclosure_date,
            "title": fi.title,
            "doc_url": fi.doc_url,
            "xbrl_url": fi.xbrl_url or "",
            "doc_type": fi.doc_type,
            "company_name": fi.company_name,
            "published_at": fi.published_at,
            "listing_source": fi.listing_source,
            "has_xbrl": fi.has_xbrl,
        })

    manifest = {
        "run_id": run_id,
        "timestamp": ts,
        "filing_count": len(records),
        "filings": records,
    }
    Path(manifest_path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"[backfill] manifest saved: {manifest_path} ({len(records)} filings)")
    print(f"[backfill] manifest saved: {manifest_path}")
    return manifest_path


def _build_provider(name: str, listing_log_dir: str | None = None):
    if name == "tdnet_html":
        return TdnetHtmlListingProvider(listing_log_dir=listing_log_dir)
    elif name == "auto":
        return CompositeListingProvider([
            TdnetHtmlListingProvider(listing_log_dir=listing_log_dir),
        ])
    else:
        raise ValueError(f"unknown listing provider: {name}")


def _compute_date_range(args) -> tuple[str, str]:
    if args.date_from and args.date_to:
        return args.date_from, args.date_to
    years = args.years or 1
    end = datetime.now()
    start = end - timedelta(days=365 * years)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _validation_rejection_summary(metrics) -> dict:
    filing_ids = list(getattr(metrics, "_validation_rejected_filing_ids", []))
    return {
        "validation_rejected_record_count": getattr(
            metrics, "_validation_rejected_record_count", 0
        ),
        "validation_rejected_filing_count": len(filing_ids),
        "validation_rejected_filing_ids": filing_ids,
        "validation_reasons_by_filing": {
            filing_id: dict(reason_counts)
            for filing_id, reason_counts in getattr(
                metrics, "_validation_reasons_by_filing", {}
            ).items()
        },
        "validation_rejection_filing_unresolved": bool(
            getattr(metrics, "_validation_rejection_filing_unresolved", False)
        ),
    }


def _summary_with_validation_rejections(metrics) -> dict:
    summary = metrics.summary_dict()
    summary.update(_validation_rejection_summary(metrics))
    summary.update(_canonical_sync_failure_summary(metrics))
    summary.update(getattr(metrics, "_isolated_seed_summary", {}))
    summary.update(getattr(metrics, "_manifest_replay_summary", {
        "manifest_replay_enabled": False,
        "manifest_replay_requested_count": 0,
        "manifest_replay_matched_count": 0,
        "manifest_replay_requeued_count": 0,
        "manifest_replay_pending_count": 0,
        "manifest_replay_non_target_changed_count": 0,
    }))
    return summary


def _canonical_sync_failure_summary(metrics) -> dict:
    filing_ids = list(getattr(metrics, "_canonical_sync_failed_filing_ids", []))
    return {
        "canonical_sync_failed_record_count": int(
            getattr(metrics, "_canonical_sync_failed_record_count", 0) or 0
        ),
        "canonical_sync_failed_filing_count": len(filing_ids),
        "canonical_sync_failed_filing_ids": filing_ids,
        "canonical_sync_failures_by_filing": {
            filing_id: dict(reason_counts)
            for filing_id, reason_counts in getattr(
                metrics, "_canonical_sync_failures_by_filing", {}
            ).items()
        },
        "canonical_sync_filing_unresolved": bool(
            getattr(metrics, "_canonical_sync_filing_unresolved", False)
        ),
    }


def _record_canonical_sync_failure(
    metrics, filing_id: str, record_count: int, reason: str
) -> None:
    filing_ids = getattr(metrics, "_canonical_sync_failed_filing_ids", None)
    if filing_ids is None:
        filing_ids = []
        metrics._canonical_sync_failed_filing_ids = filing_ids
    if filing_id not in filing_ids:
        filing_ids.append(filing_id)
    metrics._canonical_sync_failed_record_count = int(
        getattr(metrics, "_canonical_sync_failed_record_count", 0) or 0
    ) + int(record_count)
    failures = getattr(metrics, "_canonical_sync_failures_by_filing", None)
    if failures is None:
        failures = {}
        metrics._canonical_sync_failures_by_filing = failures
    reason_counts = failures.setdefault(filing_id, {})
    reason_counts[reason] = reason_counts.get(reason, 0) + int(record_count)


def _extend_filing_id_map_from_records(
    records: list[dict], filing_id_map: dict[str, str]
) -> dict[str, str]:
    """Connect requested/internal IDs using only identifiers carried by one record."""
    mapping = dict(filing_id_map)
    identifier_fields = (
        "filing_id",
        "requested_disclosure_no",
        "_requested_disclosure_no",
        "internal_document_id",
        "_internal_document_id",
        "tdnet_doc_id",
    )
    for record in records:
        identifiers = [
            str(record[field]).strip()
            for field in identifier_fields
            if record.get(field) is not None and str(record[field]).strip()
        ]
        owners = {mapping[value] for value in identifiers if value in mapping}
        if len(owners) > 1:
            raise RuntimeError("canonical_sync_filing_unresolved")
        if not owners:
            continue
        filing_id = owners.pop()
        for identifier in identifiers:
            existing = mapping.get(identifier)
            if existing is not None and existing != filing_id:
                raise RuntimeError("canonical_sync_filing_unresolved")
            mapping[identifier] = filing_id
    return mapping


def _canonical_sync_ids_by_filing(
    db,
    canonical_sync_ids: list[int],
    records: list[dict],
    filing_id_map: dict[str, str],
    fid_buffer: list[str],
) -> dict[str, list[int]]:
    """Resolve accepted SQLite row IDs to manifest filings via formal document IDs."""
    row_ids = [int(value) for value in canonical_sync_ids]
    if not row_ids:
        return {}
    mapping = _extend_filing_id_map_from_records(records, filing_id_map)
    unique_fids = list(dict.fromkeys(str(value) for value in fid_buffer))
    conn = getattr(db, "_conn", None)
    if conn is None:
        if len(unique_fids) == 1:
            return {unique_fids[0]: row_ids}
        raise RuntimeError("canonical_sync_filing_unresolved")
    placeholders = ",".join("?" for _ in row_ids)
    rows = conn.execute(
        f"SELECT id, tdnet_doc_id FROM segment_financials WHERE id IN ({placeholders})",
        row_ids,
    ).fetchall()
    doc_id_by_row_id = {int(row_id): str(doc_id).strip() for row_id, doc_id in rows}
    if set(doc_id_by_row_id) != set(row_ids):
        raise RuntimeError("canonical_sync_filing_unresolved")
    grouped: dict[str, list[int]] = {}
    for row_id in row_ids:
        filing_id = mapping.get(doc_id_by_row_id[row_id])
        if filing_id is None:
            raise RuntimeError("canonical_sync_filing_unresolved")
        grouped.setdefault(filing_id, []).append(row_id)
    return grouped


def _build_validation_filing_id_map(filings) -> dict[str, str]:
    """Map existing worker identifiers to the caller's manifest filing ID."""
    mapping: dict[str, str] = {}
    for filing in filings:
        filing_id = str(filing.filing_id).strip()
        for value in (
            filing_id,
            getattr(filing, "requested_disclosure_no", None),
            getattr(filing, "internal_document_id", None),
            getattr(filing, "tdnet_doc_id", None),
        ):
            if value is None or not str(value).strip():
                continue
            identifier = str(value).strip()
            existing = mapping.get(identifier)
            if existing is not None and existing != filing_id:
                raise RuntimeError(
                    "STOP_BACKFILL_REJECTION_CALLER_ID_MAPPING_UNRESOLVED"
                )
            mapping[identifier] = filing_id
    return mapping


def _merge_validation_rejections(
    metrics,
    stats,
    filing_id_map: dict[str, str],
) -> tuple[list[str], dict[str, dict[str, int]]]:
    record_count = int(getattr(stats, "validation_rejected_record_count", 0) or 0)
    raw_filing_ids = [
        str(value) for value in getattr(stats, "validation_rejected_filing_ids", [])
    ]
    raw_reasons = getattr(stats, "validation_reasons_by_filing", {}) or {}
    reported_filing_count = int(
        getattr(stats, "validation_rejected_filing_count", len(raw_filing_ids)) or 0
    )

    metrics._validation_rejected_record_count = getattr(
        metrics, "_validation_rejected_record_count", 0
    ) + record_count

    reason_record_count = sum(
        int(count)
        for reason_counts in raw_reasons.values()
        for count in reason_counts.values()
    )
    has_rejection_info = bool(
        record_count or raw_filing_ids or raw_reasons or reported_filing_count
    )
    if (
        has_rejection_info
        and (
            record_count <= 0
            or not raw_filing_ids
            or reported_filing_count != len(set(raw_filing_ids))
            or set(raw_reasons) != set(raw_filing_ids)
            or reason_record_count != record_count
        )
    ):
        metrics._validation_rejection_filing_unresolved = True
        raise RuntimeError("validation_rejection_filing_unresolved")

    aggregate_ids = getattr(metrics, "_validation_rejected_filing_ids", None)
    if aggregate_ids is None:
        aggregate_ids = []
        metrics._validation_rejected_filing_ids = aggregate_ids
    aggregate_reasons = getattr(metrics, "_validation_reasons_by_filing", None)
    if aggregate_reasons is None:
        aggregate_reasons = {}
        metrics._validation_reasons_by_filing = aggregate_reasons

    flush_ids: list[str] = []
    flush_reasons: dict[str, dict[str, int]] = {}
    for raw_filing_id in raw_filing_ids:
        filing_id = filing_id_map.get(raw_filing_id)
        if filing_id is None:
            metrics._validation_rejection_filing_unresolved = True
            raise RuntimeError("validation_rejection_filing_unresolved")
        if filing_id not in flush_ids:
            flush_ids.append(filing_id)
        if filing_id not in aggregate_ids:
            aggregate_ids.append(filing_id)
        filing_aggregate = aggregate_reasons.setdefault(filing_id, {})
        filing_flush = flush_reasons.setdefault(filing_id, {})
        for reason, count in raw_reasons[raw_filing_id].items():
            filing_aggregate[reason] = filing_aggregate.get(reason, 0) + int(count)
            filing_flush[reason] = filing_flush.get(reason, 0) + int(count)
    return flush_ids, flush_reasons


def _make_result(metrics, run_id, start_date, end_date, phase2, xbrl_workers, pdf_workers, workers):
    """統一戻り値を構築。early return でも benchmark でも同じ形式。"""
    return {
        "summary": _summary_with_validation_rejections(metrics),
        "metrics": metrics,
        "run_id": run_id,
        "date_range": f"{start_date}~{end_date}",
        "phase2": phase2,
        "xbrl_workers": xbrl_workers,
        "pdf_workers": pdf_workers,
        "workers": workers,
    }


def _apply_canonical_metadata_to_filings(filings, canonical_index):
    canonical_counts = {
        "canonical_metadata_matched": 0,
        "canonical_metadata_not_found": 0,
        "canonical_metadata_duplicate": 0,
        "canonical_metadata_invalid": 0,
        "canonical_metadata_conflict": 0,
        "canonical_metadata_ticker_conflict": 0,
    }
    for fi in filings:
        metadata = canonical_index.get(fi.requested_disclosure_no)
        if metadata is None:
            canonical_counts["canonical_metadata_not_found"] += 1
            continue
        if metadata.match_status == "duplicate":
            canonical_counts["canonical_metadata_duplicate"] += 1
            continue
        if metadata.match_status.startswith("invalid"):
            canonical_counts["canonical_metadata_invalid"] += 1
            continue
        if metadata.normalized_ticker != fi.ticker:
            canonical_counts["canonical_metadata_ticker_conflict"] += 1
            continue
        if fi.expected_period or fi.expected_quarter:
            if (fi.expected_period, fi.expected_quarter) != (metadata.expected_period, metadata.expected_quarter):
                canonical_counts["canonical_metadata_conflict"] += 1
                continue
        fi.expected_period = metadata.expected_period
        fi.expected_quarter = metadata.expected_quarter
        canonical_counts["canonical_metadata_matched"] += 1
    logger.info("[backfill] canonical metadata: %s", canonical_counts)
    return canonical_counts



def run_backfill(
    *,
    start_date: str,
    end_date: str,
    tickers: list[str] | None = None,
    limit: int | None = None,
    workers: int = 4,
    cache_root: str = "data/tdnet_cache",
    state_db: str = "data/backfill_state.db",
    db_batch_size: int = 200,
    listing_provider_name: str = "tdnet_html",
    skip_pdf: bool = False,
    only_xbrl: bool = False,
    listing_log_dir: str | None = "data/backfill_listing_logs",
    decision_db_path: str | None = None,
    resume: bool = False,
    retry_quarantine: bool = False,
    retry_failed: bool = False,
    retry_download: int = 3,
    retry_xbrl: int = 2,
    retry_pdf: int = 1,
    timeout_download: int = 30,
    timeout_xbrl: int = 60,
    timeout_pdf: int = 120,
    log_jsonl_path: str | None = None,
    flush_every_seconds: int = 300,
    phase2: bool = False,
    xbrl_workers: int = 6,
    pdf_workers: int = 3,
    repair_extracted: bool = False,
    only_earnings_summary: bool = True,
    exclude_corrections: bool = True,
    worker_version: str = "v4",
    filing_list_path: str | None = None,
    reset_target: bool = False,
    force_done: bool = False,
    dry_run_only: bool = True,
    manifest_dir: str = "logs",
    isolated_worker_dry_run: bool = False,
    isolated_run_root: str | None = None,
    scope_pending_to_manifest: bool = False,
    require_all_manifest_pending: bool = False,
    replay_manifest_done: bool = False,
    isolated_seed_summary: dict | None = None,
) -> dict:
    """バックフィルを実行する (Phase 1 / Phase 2 自動選択)。"""
    if replay_manifest_done:
        scope_pending_to_manifest = True
        require_all_manifest_pending = True
    if scope_pending_to_manifest and not filing_list_path:
        raise RuntimeError(f"{_PENDING_SET_MISMATCH}: scoped pending requires --filing-list")
    if require_all_manifest_pending and not scope_pending_to_manifest:
        raise RuntimeError(f"{_PENDING_SET_MISMATCH}: require-all requires scoped pending")
    if require_all_manifest_pending and limit:
        raise RuntimeError(f"{_PENDING_SET_MISMATCH}: require-all forbids --limit")
    if scope_pending_to_manifest and any((
        retry_quarantine, retry_failed, reset_target, force_done,
        resume, repair_extracted,
    )):
        raise RuntimeError(
            f"{_PENDING_SET_MISMATCH}: scoped pending cannot use global retry/reset modes"
        )
    if isolated_worker_dry_run:
        _validate_isolated_write_paths(
            run_root=isolated_run_root or "",
            decision_db_path=decision_db_path or "",
            state_db_path=state_db,
            log_jsonl_path=log_jsonl_path or "",
            filing_list_path=filing_list_path or "",
        )
    run_id = generate_run_id()
    use_v2 = worker_version == "v2"
    use_v4 = worker_version == "v4"
    metrics = BackfillMetricsV2() if (use_v2 or use_v4) else BackfillMetrics()
    metrics._isolated_seed_summary = dict(isolated_seed_summary or {})
    metrics._manifest_replay_summary = {
        "manifest_replay_enabled": bool(replay_manifest_done),
        "manifest_replay_requested_count": 0,
        "manifest_replay_matched_count": 0,
        "manifest_replay_requeued_count": 0,
        "manifest_replay_pending_count": 0,
        "manifest_replay_non_target_changed_count": 0,
    }

    if log_jsonl_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_jsonl_path = f"logs/backfill_segments_tdnet_{worker_version}_{ts}.jsonl"
    run_logger = RunLogger(log_jsonl_path, run_id=run_id)

    mode = "phase2" if phase2 else "phase1"
    logger.info(
        f"[backfill] ===== RUN START ====="
    )
    applied_limit = limit if limit and limit > 0 else None
    logger.info(
        f"[backfill] run_id={run_id} mode={mode} range={start_date}~{end_date} "
        f"limit={applied_limit or 'unlimited'} resume={resume} state_db={state_db} "
        f"only_earnings_summary={only_earnings_summary} exclude_corrections={exclude_corrections}"
    )
    print(f"[backfill] run_id={run_id} mode={mode} range={start_date}~{end_date} limit={applied_limit or 'unlimited'}")

    def _result():
        return _make_result(metrics, run_id, start_date, end_date, phase2, xbrl_workers, pdf_workers, workers)

    # ── 1. Listing ──
    if filing_list_path:
        # 固定母集団モード: manifest から直接読み込み、listing provider skip
        filings = _load_filing_list(filing_list_path)
        if applied_limit:
            filings = filings[:applied_limit]
        logger.info(f"[backfill] FIXED POPULATION mode: {len(filings)} filings from manifest")
        print(f"[backfill] FIXED POPULATION mode: {len(filings)} filings")
    else:
        logger.info(f"[backfill] listing provider start: {listing_provider_name}")
        print(f"[backfill] listing provider start: {listing_provider_name}")
        provider = _build_provider(listing_provider_name, listing_log_dir)
        filings = provider.list_filings(
            start_date, end_date, tickers=tickers, doc_types=["financial_statement"],
        )
        logger.info(f"[backfill] listing done (pre-selector): total={len(filings)}")
        print(f"[backfill] listing done (pre-selector): total={len(filings)}")

    from lib.backfill.canonical_filing_metadata import load_canonical_filing_metadata_index
    try:
        canonical_index = load_canonical_filing_metadata_index()
    except Exception as exc:
        logger.warning("[backfill] canonical metadata index unavailable: %s", exc)
        canonical_index = {}
    canonical_counts = _apply_canonical_metadata_to_filings(filings, canonical_index)


    # ── 1b. filing_selector による最終判定 ──
    # 固定母集団モードでは selector をスキップ (manifest は既にフィルタ済み)
    if filing_list_path:
        accepted = filings
    else:
        accepted = []
        excluded_reasons: dict[str, int] = {}
        excluded_samples: dict[str, list[str]] = {}
        for fi in filings:
            ok, reason = should_process_for_segment_backfill(
                fi.title,
                exclude_corrections=exclude_corrections,
                only_earnings_summary=only_earnings_summary,
            )
            if ok and _is_pro_market_filing(fi):
                ok = False
                reason = "pro_market"
            if ok:
                accepted.append(fi)
            else:
                excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
                if reason not in excluded_samples:
                    excluded_samples[reason] = []
                if len(excluded_samples[reason]) < 10:
                    excluded_samples[reason].append(f"[{fi.ticker}] {fi.title}")

        logger.info(
            f"[backfill] selector done: accepted={len(accepted)} excluded={len(filings) - len(accepted)} "
            f"reasons={excluded_reasons}"
        )
        print(f"[backfill] selector done: accepted={len(accepted)} excluded={len(filings) - len(accepted)}")
        for reason, count in sorted(excluded_reasons.items()):
            print(f"  {reason}: {count}")

    filings = accepted

    manifest_filing_ids: list[str] = []
    if scope_pending_to_manifest:
        manifest_filing_ids = [str(filing.filing_id) for filing in filings]
        if not manifest_filing_ids or len(manifest_filing_ids) != len(set(manifest_filing_ids)):
            raise RuntimeError(f"{_PENDING_SET_MISMATCH}: manifest filing IDs must be unique")

    # ── 1c. Manifest 保存 ──
    _save_manifest(filings, run_id, log_dir=manifest_dir)

    if not filings:
        logger.warning("[backfill] listing returned 0 filings — nothing to do")
        print("[backfill] WARNING: listing returned 0 filings")
        metrics.finalize()
        run_logger.log_summary(_summary_with_validation_rejections(metrics))
        run_logger.close()
        return _result()

    # ── 2. State Store ──
    logger.info(f"[backfill] register_filings start: input_count={len(filings)}")
    print(f"[backfill] register_filings start: input_count={len(filings)}")
    store = BackfillStateStore(state_db)
    reg = store.register_filings(filings)
    logger.info(
        f"[backfill] register_filings done: new={reg['new']} existing={reg['existing']} "
        f"total={reg['new'] + reg['existing']}"
    )
    print(
        f"[backfill] register_filings done: new={reg['new']} existing={reg['existing']} "
        f"total={reg['new'] + reg['existing']}"
    )

    # Invariant: listing kept>0 なのに register total==0 はおかしい
    reg_total = reg['new'] + reg['existing']
    if len(filings) > 0 and reg_total == 0:
        msg = f"INVARIANT VIOLATION: listing kept={len(filings)} but register total=0"
        logger.error(f"[backfill] {msg}")
        print(f"[backfill] ERROR: {msg}")
        run_logger.log_fatal(msg)
        metrics.finalize()
        run_logger.log_summary(_summary_with_validation_rejections(metrics))
        run_logger.close()
        store.close()
        raise RuntimeError(msg)

    if replay_manifest_done:
        try:
            metrics._manifest_replay_summary = _replay_manifest_done(store, filings)
        except ManifestReplayStop:
            store.close()
            run_logger.close()
            raise

    stale = 0 if scope_pending_to_manifest else store.reset_stale_running(max_age_hours=2)
    if stale > 0:
        logger.info(f"[backfill] reset {stale} stale running entries")

    # retry_quarantine / retry_failed は --resume なしでも単独で動作する
    # quarantined/failed -> queued にリセットすることで get_pending() に載る
    if retry_quarantine:
        _rq = store.reset_for_retry(statuses=["quarantined"])
        logger.info(f"[backfill] retry_quarantine: {_rq} quarantined -> queued")
        print(f"[backfill] retry_quarantine: {_rq} filings reset quarantined -> queued")
    if retry_failed:
        _rf = store.reset_for_retry(statuses=["failed"])
        logger.info(f"[backfill] retry_failed: {_rf} failed -> queued")
        print(f"[backfill] retry_failed: {_rf} filings reset failed -> queued")

    # done/extracted repair: resume または --repair-extracted 時に自動リセット
    if resume or repair_extracted:
        done_count = store.reset_done_to_queued()
        if done_count > 0:
            logger.info(f"[backfill] repaired {done_count} done/extracted filings -> queued")
            print(f"[backfill] repaired {done_count} done/extracted -> queued")

    # --reset-target: 対象 filing だけ強制リセット (固定母集団テスト用)
    if reset_target:
        target_fids = [f.filing_id for f in filings]
        reset_count = 0
        for fid in target_fids:
            try:
                store.reset_filing(fid)
                reset_count += 1
            except Exception:
                pass
        if reset_count > 0:
            logger.info(f"[backfill] reset-target: {reset_count} filings -> queued")
            print(f"[backfill] reset-target: {reset_count} filings -> queued")

    # --force-done: done / partial / skipped_normal / quarantined を全て再実行対象にする
    # (retryable 制約を無視して強制リセット)
    if force_done:
        logger.info("[reprocess] force rerun enabled")
        print("[reprocess] force rerun enabled")
        _force_statuses = ["done", "partial", "skipped_normal", "quarantined"]
        _ph = ",".join("?" * len(_force_statuses))
        store.conn.execute(
            f"UPDATE filing_state "
            f"SET status='queued', stage='listing', "
            f"    last_error=NULL, last_error_stage=NULL "
            f"WHERE status IN ({_ph})",
            _force_statuses,
        )
        _fd = store.conn.execute("SELECT changes()").fetchone()[0]
        store.conn.commit()
        logger.info(f"[reprocess] force_done: {_fd} filings reset -> queued")
        print(f"[reprocess] force_done: {_fd} filings reset -> queued")

    # ── 3. Pending ──
    _limit_for_query = applied_limit or 0  # 0 = unlimited in state store
    logger.info(
        f"[backfill] get_candidates start: resume={resume} "
        f"retry_quarantine={retry_quarantine} applied_limit={applied_limit or 'unlimited'}"
    )
    if scope_pending_to_manifest:
        statuses = ["queued", "running", "needs_pdf", "done"] if resume else ["queued"]
        if retry_quarantine:
            statuses.append("quarantined")
        if retry_failed:
            statuses.append("failed")
        pending = store.get_pending_for_filing_ids(
            manifest_filing_ids,
            limit=_limit_for_query,
            tickers=tickers,
            statuses=statuses,
        )
    elif resume:
        # resume モード: queued/running/needs_pdf + オプションの quarantined/failed も含める
        pending = store.get_resume_candidates(
            limit=_limit_for_query, tickers=tickers,
            include_quarantined=retry_quarantine,
            include_failed=retry_failed,
        )
    else:
        # 通常モード: queued のみ（retry_quarantine 時は上で reset_for_retry 済み）
        pending = store.get_pending(limit=_limit_for_query, tickers=tickers)

    if scope_pending_to_manifest:
        manifest_id_set = set(manifest_filing_ids)
        pending_ids = [str(row["filing_id"]) for row in pending]
        pending_id_set = set(pending_ids)
        missing_ids = sorted(manifest_id_set - pending_id_set)
        outside_ids = sorted(pending_id_set - manifest_id_set)
        duplicate_count = len(pending_ids) - len(pending_id_set)
        logger.info(
            "[backfill] manifest pending scope: manifest=%s pending=%s missing=%s outside=%s duplicates=%s",
            len(manifest_id_set), len(pending_id_set), len(missing_ids),
            len(outside_ids), duplicate_count,
        )
        print(
            f"[backfill] manifest pending scope: manifest={len(manifest_id_set)} "
            f"pending={len(pending_id_set)} missing={len(missing_ids)} "
            f"outside={len(outside_ids)} duplicates={duplicate_count}"
        )
        if outside_ids or duplicate_count or (
            require_all_manifest_pending and pending_id_set != manifest_id_set
        ):
            store.close()
            run_logger.close()
            if replay_manifest_done:
                raise ManifestReplayStop(
                    _MANIFEST_REPLAY_PENDING_SCOPE_MISMATCH,
                    {
                        "requested_count": len(filings),
                        "matched_count": len(pending_id_set),
                        "mismatched_count": len(manifest_id_set - pending_id_set),
                    },
                )
            raise RuntimeError(
                f"{_PENDING_SET_MISMATCH}: missing={missing_ids} "
                f"outside={outside_ids} duplicates={duplicate_count}"
            )

    if replay_manifest_done:
        metrics._manifest_replay_summary["manifest_replay_pending_count"] = len(pending)

    metrics.total_filings = len(pending)
    logger.info(
        f"[backfill] get_candidates done: candidate_count={len(pending)} "
        f"applied_limit={applied_limit or 'unlimited'}"
    )
    print(f"[backfill] pending candidates: {len(pending)} (limit={applied_limit or 'unlimited'})")

    if not pending:
        logger.info("[backfill] no pending filings, run complete (all previously processed)")
        print("[backfill] no pending filings (all done or already processed)")
        store_stats = store.stats()
        logger.info(f"[backfill] state_store stats: {store_stats}")
        metrics.finalize()
        run_logger.log_summary(_summary_with_validation_rejections(metrics))
        run_logger.close()
        store.close()
        return _result()

    filing_map = {f.filing_id: f for f in filings}
    validation_filing_id_map = _build_validation_filing_id_map(filings)

    # ── 4. 実行 (Phase 1 or Phase 2) ──
    segment_buffer: list[dict] = []
    fid_buffer: list[str] = []

    def _flush(buf, fid_buf):
        _flush_buffer(
            buf, fid_buf, decision_db_path, db_batch_size, metrics, store, run_logger,
            dry_run_only=dry_run_only,
            isolated_worker_dry_run=isolated_worker_dry_run,
            isolated_run_root=isolated_run_root,
            state_db_path=state_db,
            log_jsonl_path=log_jsonl_path,
            filing_list_path=filing_list_path,
            validation_filing_id_map=validation_filing_id_map,
        )

    logger.info(f"[backfill] phase={mode} stage start: input_count={len(pending)}")
    print(f"[backfill] {mode} start: {len(pending)} filings")

    if use_v4:
        from lib.backfill.phase2_runner import run_phase2_v4
        run_phase2_v4(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=run_logger, run_id=run_id,
            cache_root=cache_root,
            workers=workers,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            skip_pdf=skip_pdf,
            segment_buffer=segment_buffer, fid_buffer=fid_buffer,
            db_batch_size=db_batch_size,
            flush_every_seconds=flush_every_seconds,
            flush_callback=_flush,
            dry_run_only=dry_run_only,
            isolated_worker_dry_run=isolated_worker_dry_run,
        )
    elif use_v2:
        from lib.backfill.phase2_runner import run_phase2_v2
        run_phase2_v2(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=run_logger, run_id=run_id,
            cache_root=cache_root,
            workers=workers,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            segment_buffer=segment_buffer, fid_buffer=fid_buffer,
            db_batch_size=db_batch_size,
            flush_every_seconds=flush_every_seconds,
            flush_callback=_flush,
        )
    elif phase2:
        from lib.backfill.phase2_runner import run_phase2
        run_phase2(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=run_logger, run_id=run_id,
            cache_root=cache_root,
            xbrl_workers=xbrl_workers, pdf_workers=pdf_workers,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            segment_buffer=segment_buffer, fid_buffer=fid_buffer,
            db_batch_size=db_batch_size, decision_db_path=decision_db_path,
            flush_every_seconds=flush_every_seconds,
            flush_callback=_flush,
        )
    else:
        _run_phase1(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=run_logger, run_id=run_id,
            cache_root=cache_root, workers=workers,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            skip_pdf=skip_pdf, only_xbrl=only_xbrl,
            segment_buffer=segment_buffer, fid_buffer=fid_buffer,
            db_batch_size=db_batch_size, decision_db_path=decision_db_path,
            flush_every_seconds=flush_every_seconds,
            dry_run_only=dry_run_only,
            validation_filing_id_map=validation_filing_id_map,
        )

    logger.info(f"[backfill] {mode} done")
    print(f"[backfill] {mode} done")

    # ── 5. 残りバッファ flush ──
    if segment_buffer:
        logger.info(f"[backfill] flush start: record_count={len(segment_buffer)}")
        _flush(segment_buffer, fid_buffer)
        logger.info("[backfill] flush done")

    # ── 6. サマリ ──
    logger.info("[backfill] summary start")
    metrics.finalize()
    store_stats = store.stats()
    store.close()

    logger.info(f"[backfill] state_store stats: {store_stats}")
    run_logger.log_summary(_summary_with_validation_rejections(metrics))
    run_logger.close()
    metrics.print_summary()
    logger.info(f"[backfill] summary done, report={log_jsonl_path}")
    print(f"[backfill] summary done, JSONL={log_jsonl_path}")
    return _result()


def _run_phase1(
    pending, filing_map, *, store, metrics, run_logger, run_id, cache_root,
    workers, retry_download, retry_xbrl, retry_pdf,
    timeout_download, timeout_xbrl, timeout_pdf,
    skip_pdf, only_xbrl,
    segment_buffer, fid_buffer, db_batch_size, decision_db_path,
    flush_every_seconds,
    dry_run_only: bool = True,
    validation_filing_id_map: dict[str, str] | None = None,
):
    """Phase 1: 従来の ThreadPoolExecutor。"""
    last_flush = time.monotonic()

    def _process(fi):
        return process_one_filing(
            fi, cache_root=cache_root, state_store=store,
            skip_pdf=skip_pdf, only_xbrl=only_xbrl,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            run_id=run_id,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for row in pending:
            fid = row["filing_id"]
            fi = filing_map.get(fid)
            if not fi:
                continue
            futures[executor.submit(_process, fi)] = (fid, fi)

        for i, fut in enumerate(as_completed(futures), 1):
            fid, fi = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.error(f"[backfill] {fid} exception: {e}")
                result = None
                try:
                    store.mark_failed(fid, error=str(e), stage="worker_exception")
                except Exception:
                    pass

            if result is None:
                metrics.failed_count += 1
                metrics.completed_filings += 1
                continue

            metrics.record_result(result)
            run_logger.log_filing_result(result, fi)

            try:
                if result.status == "ok":
                     store.mark_done(fid, via=result.via, segment_count=len(result.segment_records), result_fingerprint=result.result_fingerprint, duration_ms=result.metrics.get("total_ms", 0))
                elif result.status == "quarantined":
                     store.mark_quarantined(fid, error=(result.quarantine or {}).get("error_message", ""), stage=(result.quarantine or {}).get("stage", "unknown"), review_hint=(result.quarantine or {}).get("review_hint", ""))
                elif result.status == "failed":
                     store.mark_failed(fid, error=(result.quarantine or {}).get("error_message", "unknown"), stage="worker")
            except Exception:
                pass

            if result.segment_records:
                segment_buffer.extend(result.segment_records)
                fid_buffer.append(fid)

            now_t = time.monotonic()
            if (len(segment_buffer) >= db_batch_size or (now_t - last_flush > flush_every_seconds and segment_buffer)):
                _flush_buffer(
                    segment_buffer, fid_buffer, decision_db_path, db_batch_size,
                    metrics, store, run_logger, dry_run_only=dry_run_only,
                    isolated_worker_dry_run=isolated_worker_dry_run,
                    validation_filing_id_map=validation_filing_id_map,
                )
                last_flush = time.monotonic()

            if i % 10 == 0 or i == len(futures):
                logger.info(f"[backfill] progress: {i}/{len(futures)} ok={metrics.ok_count} q={metrics.quarantined_count} f={metrics.failed_count}")


def _flush_buffer(
    buffer, fid_buffer, decision_db_path, batch_size, metrics, store, run_logger,
    dry_run_only: bool = True,
    isolated_worker_dry_run: bool = False,
    isolated_run_root: str | None = None,
    state_db_path: str | None = None,
    log_jsonl_path: str | None = None,
    filing_list_path: str | None = None,
    validation_filing_id_map: dict[str, str] | None = None,
):
    """segment バッファを DB に flush し、state を mark_upserted する。"""
    if isolated_worker_dry_run:
        _validate_isolated_write_paths(
            run_root=isolated_run_root or "",
            decision_db_path=decision_db_path or "",
            state_db_path=state_db_path or "",
            log_jsonl_path=log_jsonl_path or "",
            filing_list_path=filing_list_path or "",
        )
    report_only = dry_run_only and not isolated_worker_dry_run
    if report_only or not decision_db_path:
        # DB書き込みが指定されていない(dry-runモード)場合でも、
        # 本番のDBを読み取り専用で開いて検証レポートをコンソールに表示する。
        try:
            from src.migration.migration_db import MigrationDB
            from lib.backfill.batch_upsert import dry_run_upsert_segments
            real_db_path = decision_db_path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "decision_db.db")
            db = MigrationDB(real_db_path)
            dry_run_upsert_segments(buffer, db)
            db.close()
        except Exception as e:
            logger.error(f"[dry-run] verification verification failed: {e}")
        buffer.clear()
        fid_buffer.clear()
        return
    try:
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(decision_db_path)
        stats = batch_upsert_segments(buffer, db, batch_size=batch_size)
        metrics.record_upsert(stats)
        effective_filing_id_map = dict(validation_filing_id_map or {})
        for fid in fid_buffer:
            effective_filing_id_map.setdefault(str(fid), str(fid))
        try:
            rejected_filing_ids, rejected_reasons = _merge_validation_rejections(
                metrics,
                stats,
                effective_filing_id_map,
            )
        except RuntimeError as exc:
            if str(exc) == "validation_rejection_filing_unresolved":
                for fid in dict.fromkeys(fid_buffer):
                    store.mark_failed(
                        fid,
                        error="validation_rejection_filing_unresolved",
                        stage="validation_rejected",
                    )
            raise
        upsert_detail = {
            "records": len(buffer),
            "inserted": stats.inserted,
            "updated": stats.updated,
            "no_change": stats.no_change,
            "rejected_lower_priority": stats.rejected_lower_priority,
            "rejected_filing_conflict": stats.rejected_filing_conflict,
            "rejected_filing_identity_unresolved": stats.rejected_filing_identity_unresolved,
            "failed_batches": stats.failed_batches,
            "canonical_sync_ids": stats.canonical_sync_ids,
            "validation_rejected_record_count": getattr(
                stats, "validation_rejected_record_count", 0
            ),
            "validation_rejected_filing_count": len(rejected_filing_ids),
            "validation_rejected_filing_ids": rejected_filing_ids,
            "validation_reasons_by_filing": rejected_reasons,
        }
        canonical_sync_enabled = not dry_run_only and not isolated_worker_dry_run
        if stats.failed_batches == 0 and stats.canonical_sync_ids and canonical_sync_enabled:
            try:
                sync_ids_by_filing = _canonical_sync_ids_by_filing(
                    db,
                    stats.canonical_sync_ids,
                    buffer,
                    effective_filing_id_map,
                    fid_buffer,
                )
            except Exception:
                metrics._canonical_sync_filing_unresolved = True
                metrics._canonical_sync_failed_record_count = int(
                    getattr(metrics, "_canonical_sync_failed_record_count", 0) or 0
                ) + len(stats.canonical_sync_ids)
                aggregate_ids = getattr(
                    metrics, "_canonical_sync_failed_filing_ids", None
                )
                if aggregate_ids is None:
                    aggregate_ids = []
                    metrics._canonical_sync_failed_filing_ids = aggregate_ids
                for fid in dict.fromkeys(str(value) for value in fid_buffer):
                    already_failed = fid in aggregate_ids
                    if fid not in aggregate_ids:
                        aggregate_ids.append(fid)
                    if not already_failed:
                        store.mark_failed(
                            fid,
                            error="canonical_sync_filing_unresolved",
                            stage="canonical_sync_failed",
                        )
                sync_ids_by_filing = {}
            if sync_ids_by_filing:
                from lib.pipeline.db import load_env, get_supabase_write_config
                from tools.sync_segments import sync_sqlite_segment_ids
                load_env()
                config = get_supabase_write_config()
                for filing_id, filing_sync_ids in sync_ids_by_filing.items():
                    reason = None
                    if not config:
                        reason = "canonical_sync_exception"
                    else:
                        try:
                            sync_result = sync_sqlite_segment_ids(
                                decision_db_path,
                                filing_sync_ids,
                                config["rest_url"],
                                config["headers"],
                                dry_run=False,
                            )
                            if sync_result.get("sync_error"):
                                reason = "canonical_sync_error"
                            elif set(sync_result.get("synced_segment_ids", [])) != set(
                                filing_sync_ids
                            ):
                                reason = "canonical_sync_readback_mismatch"
                        except Exception:
                            logger.exception(
                                "[backfill] canonical sync exception: "
                                f"filing_id={filing_id} "
                                f"record_count={len(filing_sync_ids)}"
                            )
                            reason = "canonical_sync_exception"
                    if reason is None:
                        continue
                    already_failed = filing_id in getattr(
                        metrics, "_canonical_sync_failed_filing_ids", []
                    )
                    _record_canonical_sync_failure(
                        metrics, filing_id, len(filing_sync_ids), reason
                    )
                    if not already_failed:
                        store.mark_failed(
                            filing_id,
                            error=reason,
                            stage="canonical_sync_failed",
                        )
                    logger.error(
                        "[backfill] canonical sync failed: "
                        f"filing_id={filing_id} record_count={len(filing_sync_ids)} "
                        f"reason={reason}"
                    )
        upsert_detail.update(_canonical_sync_failure_summary(metrics))
        run_logger.log_upsert("batch", upsert_detail)
        for fid in rejected_filing_ids:
            store.mark_failed(
                fid,
                error=json.dumps(
                    rejected_reasons[fid],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                stage="validation_rejected",
            )
        if stats.failed_batches == 0:
            rejected_filing_id_set = set(rejected_filing_ids)
            canonical_failed_filing_id_set = set(
                getattr(metrics, "_canonical_sync_failed_filing_ids", [])
            )
            for fid in dict.fromkeys(fid_buffer):
                if (
                    fid in rejected_filing_id_set
                    or fid in canonical_failed_filing_id_set
                ):
                    continue
                try:
                    store.mark_upserted(fid)
                    metrics.upserted_count += 1
                except Exception:
                    pass
        logger.info(f"[backfill] flushed {len(buffer)} records: inserted={stats.inserted} updated={stats.updated}")
        db.close()
    except Exception as e:
        logger.error(f"[backfill] flush failed: {e}")
    buffer.clear()
    fid_buffer.clear()


def _run_benchmark_report(result: dict, args) -> None:
    """ベンチマーク後処理: estimator + reporting。"""
    from lib.backfill.estimator import estimate_full_backfill, compute_retry_factor
    from lib.backfill.reporting import (
        generate_notes, compute_percentiles, build_report,
        save_json_report, save_markdown_report,
    )

    metrics_obj = result["metrics"]
    summary = result["summary"]

    # percentiles
    pct = compute_percentiles(metrics_obj.filing_durations_ms)

    # estimate
    estimate = None
    est_total = getattr(args, "estimated_total_filings", 0) or 0
    if est_total > 0:
        # filing-based metrics を優先
        sample = summary.get("filing_completed", summary.get("completed", 0))
        avg_xbrl = summary.get("avg_xbrl_sec", 0) or (metrics_obj.avg_xbrl_sec if hasattr(metrics_obj, 'avg_xbrl_sec') else 0)
        avg_pdf = summary.get("avg_pdf_sec", 0) or (metrics_obj.avg_pdf_sec if hasattr(metrics_obj, 'avg_pdf_sec') else 0)
        # fallback: avg_pdf が 0 なら avg_sec_per_filing * 3
        if avg_pdf <= 0:
            avg_pdf = summary.get("avg_sec_per_filing", 1.0) * 3
        # fallback: avg_xbrl が 0 なら avg_sec_per_filing
        if avg_xbrl <= 0:
            avg_xbrl = summary.get("avg_sec_per_filing", 1.0)

        xbrl_rate_str = summary.get("xbrl_success_rate", "0%")
        pdf_fb_str = summary.get("pdf_fallback_rate", "0%")
        q_rate_str = summary.get("quarantine_rate", "0%")
        # parse '%' strings to float
        def _pct(s):
            if isinstance(s, (int, float)):
                return float(s)
            try:
                return float(str(s).rstrip("%")) / 100
            except (ValueError, TypeError):
                return 0.0

        est = estimate_full_backfill(
            estimated_total_filings=est_total,
            sample_filings=sample,
            avg_xbrl_sec=avg_xbrl,
            avg_pdf_sec=avg_pdf,
            xbrl_success_rate=_pct(xbrl_rate_str),
            pdf_fallback_rate=_pct(pdf_fb_str),
            quarantine_rate=_pct(q_rate_str),
            xbrl_workers=result.get("xbrl_workers", 6),
            pdf_workers=result.get("pdf_workers", 3),
            retry_factor=compute_retry_factor(
                summary.get("retried", 0),
                summary.get("filing_completed", summary.get("completed", 0)),
            ),
        )
        estimate = est.to_dict()

        # invariant check
        if sample > 0 and est.base_case_sec <= 0:
            logger.warning(
                f"[report] invariant violation: sample_filings={sample}, "
                f"avg_pdf_sec={avg_pdf}, avg_xbrl_sec={avg_xbrl}, "
                f"but base_case_sec=0"
            )

        print("\n" + "=" * 60)
        print("  3-Year Full Backfill Estimate")
        print("=" * 60)
        for k, v in estimate.items():
            print(f"  {k:30s} {v}")
        print("=" * 60)

    # notes
    notes = generate_notes(summary, estimate)
    if notes:
        print("\n  Observations:")
        for n in notes:
            print(f"    \u2022 {n}")

    # build report
    report = build_report(
        benchmark_name=getattr(args, "benchmark_name", "unnamed") or "unnamed",
        phase2=result.get("phase2", False),
        xbrl_workers=result.get("xbrl_workers", 6),
        pdf_workers=result.get("pdf_workers", 3),
        workers=result.get("workers", 4),
        metrics=summary,
        estimate=estimate,
        notes=notes,
        percentiles=pct,
        run_id=result.get("run_id", ""),
        date_range=result.get("date_range", ""),
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bn = getattr(args, "benchmark_name", "bench") or "bench"

    # JSON
    json_path = getattr(args, "report_json", None)
    if json_path is None:
        json_path = f"reports/backfill_benchmark_{bn}_{ts}.json"
    save_json_report(report, json_path)
    print(f"\n  JSON report: {json_path}")

    # Markdown
    md_path = getattr(args, "report_md", None)
    if md_path is None:
        md_path = f"reports/backfill_benchmark_{bn}_{ts}.md"
    save_markdown_report(report, md_path)
    print(f"  Markdown report: {md_path}")


def _run_dry_run(
    *,
    start_date: str,
    end_date: str,
    tickers: list[str] | None = None,
    listing_provider_name: str = "tdnet_html",
    only_earnings_summary: bool = True,
    exclude_corrections: bool = True,
) -> None:
    """dry-run: 対象母集団の集計のみ。download/extract/upsert しない。"""
    import csv
    import json as json_mod

    print("=" * 60)
    print("  SEGMENT BACKFILL — DRY RUN")
    print("=" * 60)
    print(f"  range: {start_date} ~ {end_date}")
    print(f"  only_earnings_summary: {only_earnings_summary}")
    print(f"  exclude_corrections: {exclude_corrections}")
    print()

    # 1. Listing 取得
    print("[dry-run] listing provider start ...")
    provider = _build_provider(listing_provider_name)
    filings = provider.list_filings(
        start_date, end_date, tickers=tickers, doc_types=["financial_statement"],
    )
    print(f"[dry-run] listing done: pre-selector total = {len(filings)}")

    # 2. selector 判定
    accepted: list = []
    excluded: list = []
    excluded_reasons: dict[str, int] = {}
    excluded_samples: dict[str, list[dict]] = {}
    accepted_samples: list[dict] = []

    for fi in filings:
        ok, reason = should_process_for_segment_backfill(
            fi.title,
            exclude_corrections=exclude_corrections,
            only_earnings_summary=only_earnings_summary,
        )
        if ok and _is_pro_market_filing(fi):
            ok = False
            reason = "pro_market"
        entry = {
            "ticker": fi.ticker,
            "disclosure_date": fi.disclosure_date,
            "title": fi.title,
            "reason": reason,
        }
        if ok:
            accepted.append(entry)
            if len(accepted_samples) < 10:
                accepted_samples.append(entry)
        else:
            excluded.append(entry)
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
            if reason not in excluded_samples:
                excluded_samples[reason] = []
            if len(excluded_samples[reason]) < 10:
                excluded_samples[reason].append(entry)

    # 3. 出力
    print()
    print(f"  総件数 (pre-selector):  {len(filings)}")
    print(f"  採用件数:                {len(accepted)}")
    print(f"  除外件数:                {len(excluded)}")
    print()
    print("  除外理由別件数:")
    for reason, count in sorted(excluded_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count}")
    print()

    print("  採用タイトルサンプル (最大10件):")
    for s in accepted_samples:
        print(f"    [{s['ticker']}] {s['title']}")
    print()

    for reason, samples in excluded_samples.items():
        print(f"  除外サンプル [{reason}] (最大10件):")
        for s in samples:
            print(f"    [{s['ticker']}] {s['title']}")
        print()

    # 4. ファイル保存
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    Path("logs").mkdir(exist_ok=True)

    # JSON
    report_data = {
        "timestamp": ts,
        "date_range": f"{start_date}~{end_date}",
        "only_earnings_summary": only_earnings_summary,
        "exclude_corrections": exclude_corrections,
        "total_pre_selector": len(filings),
        "accepted_count": len(accepted),
        "excluded_count": len(excluded),
        "excluded_reasons": excluded_reasons,
        "accepted_samples": accepted_samples[:10],
        "excluded_samples": {k: v[:10] for k, v in excluded_samples.items()},
    }
    json_path = f"logs/segment_backfill_dryrun_{ts}.json"
    Path(json_path).write_text(
        json_mod.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  JSON saved: {json_path}")

    # TXT
    txt_path = f"logs/segment_backfill_dryrun_{ts}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"SEGMENT BACKFILL DRY RUN - {ts}\n")
        f.write(f"Range: {start_date} ~ {end_date}\n")
        f.write(f"Total (pre-selector): {len(filings)}\n")
        f.write(f"Accepted: {len(accepted)}\n")
        f.write(f"Excluded: {len(excluded)}\n\n")
        for reason, count in sorted(excluded_reasons.items(), key=lambda x: -x[1]):
            f.write(f"  {reason}: {count}\n")
    print(f"  TXT saved: {txt_path}")

    # CSV (accepted)
    csv_accepted_path = f"logs/segment_backfill_dryrun_{ts}_accepted.csv"
    with open(csv_accepted_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "disclosure_date", "title", "reason"])
        writer.writeheader()
        writer.writerows(accepted)
    print(f"  CSV (accepted) saved: {csv_accepted_path}")

    # CSV (excluded)
    csv_excluded_path = f"logs/segment_backfill_dryrun_{ts}_excluded.csv"
    with open(csv_excluded_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "disclosure_date", "title", "reason"])
        writer.writeheader()
        writer.writerows(excluded)
    print(f"  CSV (excluded) saved: {csv_excluded_path}")

    print()
    print("=" * 60)
    print("  DRY RUN COMPLETE — no downloads, no extractions, no upserts")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="TDNET 並列バックフィル — セグメント業績抽出")
    parser.add_argument("--years", type=int, default=None)
    parser.add_argument("--date-from", type=str, default=None)
    parser.add_argument("--date-to", type=str, default=None)
    parser.add_argument("--tickers", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None, help="Max filings to process (default: unlimited)")
    parser.add_argument("--workers", type=int, default=4, help="Phase 1 並列数")
    parser.add_argument("--listing-provider", type=str, default="tdnet_html")
    parser.add_argument("--cache-root", type=str, default="data/tdnet_cache")
    parser.add_argument("--state-db", type=str, default="data/backfill_state.db")
    parser.add_argument(
        "--decision-db", type=str,
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "decision_db.db"),
        help="セグメント保存先 SQLite (default: PROJECT_ROOT/decision_db.db)",
    )
    parser.add_argument("--db-batch-size", type=int, default=200)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--only-xbrl", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-quarantine", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-download", type=int, default=3)
    parser.add_argument("--retry-xbrl", type=int, default=2)
    parser.add_argument("--retry-pdf", type=int, default=1)
    parser.add_argument("--timeout-download", type=int, default=30)
    parser.add_argument("--timeout-xbrl", type=int, default=60)
    parser.add_argument("--timeout-pdf", type=int, default=120)
    parser.add_argument("--repair-extracted", action="store_true",
                        help="done/extracted の残骸 filing を queued に戻して再抽出")
    parser.add_argument("--log-jsonl", type=str, default=None)
    parser.add_argument("--flush-every-seconds", type=int, default=300)
    # 固定母集団
    parser.add_argument("--filing-list", type=str, default=None,
                        help="固定母集団 manifest (JSON/JSONL/CSV)。listing provider をスキップ")
    parser.add_argument("--reset-target", action="store_true",
                        help="manifest内の全 filing を queued にリセットして再処理")
    parser.add_argument("--scope-pending-to-manifest", action="store_true",
                        help="pending取得を--filing-list内のfiling IDだけに限定")
    parser.add_argument("--require-all-manifest-pending", action="store_true",
                        help="worker開始前にmanifest ID集合とpending集合の完全一致を要求")
    parser.add_argument("--requeue-requested-id", type=str, default=None,
                        help="--requeue-onlyで再キューするmanifest内requested disclosure ID")
    parser.add_argument("--requeue-only", action="store_true",
                        help="指定requested IDの既存state 1行だけを再キューして終了")
    parser.add_argument("--requeue-expected-stage", type=str, default=None,
                        help="requeue元stageの完全一致条件")
    parser.add_argument("--requeue-expected-error", type=str, default=None,
                        help="requeue元last_errorの完全一致条件")
    # Step 4
    parser.add_argument("--phase2", action="store_true", help="Phase 2: XBRL/PDF 分離実行")
    parser.add_argument("--xbrl-workers", type=int, default=6, help="Phase 2 XBRL 並列数")
    parser.add_argument("--pdf-workers", type=int, default=3, help="Phase 2 PDF 並列数")
    # Step 5: ベンチマーク
    parser.add_argument("--benchmark", action="store_true", help="ベンチマークモード")
    parser.add_argument("--benchmark-name", type=str, default=None, help="ベンチ名")
    parser.add_argument("--estimated-total-filings", type=int, default=0, help="3年フル推定用の総 filing 数")
    parser.add_argument("--report-json", type=str, default=None, help="JSON レポート出力先")
    parser.add_argument("--report-md", type=str, default=None, help="Markdown レポート出力先")
    # 決算短信フィルタ
    parser.add_argument("--only-earnings-summary", action="store_true", default=True,
                        help="決算短信のみ対象 (デフォルト ON)")
    parser.add_argument("--no-only-earnings-summary", dest="only_earnings_summary", action="store_false",
                        help="決算短信以外も対象にする")
    parser.add_argument("--exclude-corrections", action="store_true", default=True,
                        help="訂正資料を除外 (デフォルト ON)")
    parser.add_argument("--no-exclude-corrections", dest="exclude_corrections", action="store_false",
                        help="訂正資料も対象にする")
    parser.add_argument("--worker-version", type=str, default="v4", choices=["v1", "v2", "v4"],
                        help="Worker version: v1 (legacy PDF-only) / v2 (XBRL-first) / v4 (XBRL-first + V4 PDF fallback, default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="集計のみ。download・extract・upsert しない")
    parser.add_argument("--isolated-worker-dry-run", action="store_true",
                        help="V4 worker をrun-root配下だけでoffline実行する")
    parser.add_argument("--run-root", type=str, default=None,
                        help="--isolated-worker-dry-run の隔離成果物ルート")
    parser.add_argument("--isolated-seed-decision-db", type=str, default=None,
                        help="隔離decision DBへread-only SQLite backupする絶対パス")
    parser.add_argument("--isolated-seed-cache-root", type=str, default=None,
                        help="manifest対象cacheだけを隔離rootへ複製する絶対パス")
    parser.add_argument("--force-done", action="store_true",
                        help="done/partial/skipped_normal/quarantined を全て再実行対象にする (upsert 更新)")
    parser.add_argument("--replay-manifest-done", action="store_true",
                        help="--apply の manifest 対象 done/extracted だけを原子的に再処理する")
    parser.add_argument("--apply", action="store_true",
                        help="実際にDBに書き込む (ALLOW_BACKFILL_XBRL_WRITE=1 環境変数も必要)")

    args = parser.parse_args()

    if args.replay_manifest_done:
        invalid_mode = (
            not args.filing_list
            or not args.apply
            or args.worker_version != "v4"
            or args.workers != 1
            or args.resume
            or args.repair_extracted
            or args.retry_failed
            or args.dry_run
            or args.isolated_worker_dry_run
            or args.isolated_seed_decision_db
            or args.isolated_seed_cache_root
        )
        if invalid_mode:
            _emit_manifest_replay_stop(
                ManifestReplayStop(_MANIFEST_REPLAY_INVALID_MODE, {"reason": "invalid_mode"})
            )
            raise SystemExit(1)

    isolated_seed_requested = bool(
        args.isolated_seed_decision_db or args.isolated_seed_cache_root
    )
    if isolated_seed_requested and (
        not args.isolated_worker_dry_run or args.apply or args.dry_run
    ):
        _emit_isolated_seed_stop(
            IsolatedSeedStop(
                _ISOLATED_SEED_INVALID_MODE,
                "isolated_seed_mode",
                {"reason": "invalid_mode"},
            )
        )
        raise SystemExit(1)

    if args.requeue_only:
        incompatible = any((
            args.apply, args.isolated_worker_dry_run, args.dry_run,
            args.retry_quarantine, args.retry_failed, args.reset_target,
            args.force_done, args.resume, args.repair_extracted,
            args.scope_pending_to_manifest, args.require_all_manifest_pending,
            args.phase2, args.benchmark,
        ))
        if incompatible:
            parser.error("--requeue-only cannot be combined with worker/apply/retry modes")
        if not args.filing_list or not args.requeue_requested_id:
            parser.error("--requeue-only requires --filing-list and --requeue-requested-id")
        _run_requeue_only(
            filing_list_path=args.filing_list,
            state_db_path=args.state_db,
            requested_disclosure_no=args.requeue_requested_id,
            expected_stage=args.requeue_expected_stage,
            expected_error=args.requeue_expected_error,
        )
        return
    if args.requeue_requested_id or args.requeue_expected_stage or args.requeue_expected_error:
        parser.error("requeue arguments require --requeue-only")
    if args.scope_pending_to_manifest:
        if not args.filing_list:
            parser.error("--scope-pending-to-manifest requires --filing-list")
        if any((
            args.retry_quarantine, args.retry_failed, args.reset_target,
            args.force_done, args.resume, args.repair_extracted,
        )):
            parser.error("manifest-scoped mode cannot be combined with global retry/reset modes")
    if args.require_all_manifest_pending and not args.scope_pending_to_manifest:
        parser.error("--require-all-manifest-pending requires --scope-pending-to-manifest")
    if args.require_all_manifest_pending and args.limit:
        parser.error("--require-all-manifest-pending cannot be combined with --limit")

    manifest_dir = "logs"
    isolated_seed_summary = None
    if args.isolated_worker_dry_run:
        try:
            if args.apply or args.dry_run or args.worker_version != "v4" or args.workers != 1:
                raise RuntimeError(_ISOLATED_SEED_INVALID_MODE)
            if not args.filing_list or not args.run_root:
                parser.error("--isolated-worker-dry-run requires --filing-list and --run-root")
            raw_run_root = Path(args.run_root)
            if not raw_run_root.is_absolute():
                parser.error("--run-root must be an absolute path in isolated mode")
            _assert_isolated_path_safe(raw_run_root, path_role="run_root")
            run_root = raw_run_root.resolve()
            production_roots = (Path("logs").resolve(), Path("data").resolve())
            if any(run_root == root or root in run_root.parents for root in production_roots):
                parser.error("--run-root must not be inside production logs or data")
            filing_list = Path(args.filing_list).resolve()
            input_dir = run_root / "input"
            if input_dir not in filing_list.parents:
                parser.error("--filing-list must be under <run-root>/input in isolated mode")
            if run_root.exists() and any(path != input_dir and path not in input_dir.parents for path in run_root.iterdir()):
                parser.error("--run-root must be empty except for its input directory")
            run_root.mkdir(parents=True, exist_ok=True)
            for directory in (input_dir, run_root / "manifest", run_root / "state", run_root / "cache", run_root / "logs", run_root / "output", run_root / "metadata"):
                directory.mkdir(parents=True, exist_ok=True)
            args.state_db = str(run_root / "state" / "state.db")
            args.decision_db = str(run_root / "state" / "decision.db")
            args.cache_root = str(run_root / "cache")
            args.log_jsonl = str(run_root / "logs" / "run.jsonl")
            manifest_dir = str(run_root / "manifest")
            _validate_isolated_write_paths(
                run_root=str(run_root),
                decision_db_path=args.decision_db,
                state_db_path=args.state_db,
                log_jsonl_path=args.log_jsonl,
                filing_list_path=args.filing_list,
            )
            isolated_seed_summary = _prepare_isolated_seeds(
                decision_db_source=args.isolated_seed_decision_db,
                cache_root_source=args.isolated_seed_cache_root,
                decision_db_destination=Path(args.decision_db),
                cache_destination_root=Path(args.cache_root),
                run_root=run_root,
                filing_list_path=args.filing_list,
            )
        except RuntimeError as exc:
            stop = _as_isolated_seed_stop(exc, stage="isolated_seed_prepare")
            if stop is None:
                raise
            _emit_isolated_seed_stop(stop)
            raise SystemExit(1) from None

    # ── 実行禁止ガード ──
    dry_run_only = True
    if args.apply:
        if os.environ.get("ALLOW_BACKFILL_XBRL_WRITE") != "1":
            print("[ERROR] backfill_xbrl write mode is disabled by default. Use dry-run or explicitly enable ALLOW_BACKFILL_XBRL_WRITE=1 after GPT approval.", file=sys.stderr)
            sys.exit(1)
        dry_run_only = False
    else:
        logging.getLogger("backfill").info("Running in default dry-run mode (no SQLite write). Verification report will be shown.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    start_date, end_date = _compute_date_range(args)
    tickers = args.tickers.split(",") if args.tickers else None

    # ── dry-run モード ──
    if args.dry_run:
        _run_dry_run(
            start_date=start_date, end_date=end_date, tickers=tickers,
            listing_provider_name=args.listing_provider,
            only_earnings_summary=args.only_earnings_summary,
            exclude_corrections=args.exclude_corrections,
        )
        return

    # JSONL logger は main 側でも保持 — fatal ログ用
    run_logger_for_fatal: RunLogger | None = None

    try:
        result = run_backfill(
            start_date=start_date, end_date=end_date, tickers=tickers, limit=args.limit,
            workers=args.workers, cache_root=args.cache_root, state_db=args.state_db,
            db_batch_size=args.db_batch_size, listing_provider_name=args.listing_provider,
            skip_pdf=args.skip_pdf, only_xbrl=args.only_xbrl, decision_db_path=args.decision_db,
            resume=args.resume, retry_quarantine=args.retry_quarantine, retry_failed=args.retry_failed,
            retry_download=args.retry_download, retry_xbrl=args.retry_xbrl, retry_pdf=args.retry_pdf,
            timeout_download=args.timeout_download, timeout_xbrl=args.timeout_xbrl, timeout_pdf=args.timeout_pdf,
            log_jsonl_path=args.log_jsonl, flush_every_seconds=args.flush_every_seconds,
            phase2=args.phase2, xbrl_workers=args.xbrl_workers, pdf_workers=args.pdf_workers,
            repair_extracted=args.repair_extracted,
            only_earnings_summary=args.only_earnings_summary,
            exclude_corrections=args.exclude_corrections,
            worker_version=args.worker_version,
            filing_list_path=args.filing_list,
            reset_target=args.reset_target,
            force_done=args.force_done,
            dry_run_only=dry_run_only,
            manifest_dir=manifest_dir,
            isolated_worker_dry_run=args.isolated_worker_dry_run,
            isolated_run_root=str(run_root) if args.isolated_worker_dry_run else None,
            scope_pending_to_manifest=args.scope_pending_to_manifest,
            require_all_manifest_pending=args.require_all_manifest_pending,
            replay_manifest_done=args.replay_manifest_done,
            isolated_seed_summary=isolated_seed_summary,
        )
    except ManifestReplayStop as stop:
        _emit_manifest_replay_stop(stop)
        raise SystemExit(1) from None
    except Exception:
        import traceback
        tb = traceback.format_exc()
        logger.exception("[backfill] FATAL: unhandled exception in run_backfill")
        print(f"[backfill] FATAL:\n{tb}", file=sys.stderr)
        # JSONL fatal ログ — run_backfill 内の RunLogger は既に close 済みかもしれないが、
        # main 側で別途 fatal を書く
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fatal_path = args.log_jsonl or f"logs/backfill_fatal_{ts}.jsonl"
        try:
            run_logger_for_fatal = RunLogger(fatal_path)
            run_logger_for_fatal.log_fatal(tb[:2000])
            run_logger_for_fatal.close()
        except Exception:
            pass
        sys.exit(1)

    if args.benchmark:
        try:
            _run_benchmark_report(result, args)
        except Exception:
            logger.exception("[backfill] benchmark report failed")
            print("[backfill] WARNING: benchmark report failed", file=sys.stderr)

    summary = result.get("summary", result) if isinstance(result, dict) else result
    if (
        summary.get("failed", 0) > 0
        or summary.get("upsert_failed_batches", 0) > 0
        or summary.get("validation_rejected_filing_count", 0) > 0
        or summary.get("validation_rejection_filing_unresolved", False)
        or summary.get("canonical_sync_failed_filing_count", 0) > 0
        or summary.get("canonical_sync_filing_unresolved", False)
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
