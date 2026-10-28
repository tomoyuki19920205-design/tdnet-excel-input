#!/usr/bin/env python3
"""Deploy only audited NY files from formal main to the dedicated runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "config/ny_market_daily_scheduled_prompt.txt",
    "lib/ny_market.py", "lib/ny_market_market_data.py", "lib/ny_market_price_basis.py",
    "lib/ny_market_research.py", "lib/ny_market_issuer_cap.py",
    "lib/production_environment.py", "lib/pipeline/db.py",
    "tools/build_ny_market_market_data_packet.py", "tools/build_ny_market_provisional_payload.py",
    "tools/ny_market_inbox_worker.py", "tools/preflight_ny_market_runtime.py",
    "tools/rebuild_ny_market_packet_issuer_caps.py", "tools/sync_ny_market.py",
    "tools/apply_ny_market_provisional_migration.py", "tools/check_ny_automation_state.py",
    "migrations/020_ny_market_provisional.sql",
)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def _sha(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def plan(runtime: Path) -> dict:
    commit = _git("rev-parse", "HEAD")
    if commit != _git("rev-parse", "main") or commit != _git("rev-parse", "origin/main"):
        raise RuntimeError("deployment source must equal published formal main")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("deployment source has uncommitted tracked changes")
    root = runtime.resolve(strict=True)
    files = []
    for relative in FILES:
        source = ROOT / relative
        target = runtime / relative
        if not target.resolve().is_relative_to(root):
            raise RuntimeError(f"runtime path escapes root: {relative}")
        before = target.read_bytes() if target.exists() else None
        after = source.read_bytes()
        files.append({"path": relative, "before_sha256": _sha(before),
                      "after_sha256": _sha(after), "changed": before != after})
    return {"source_commit": commit, "runtime": str(root), "files": files,
            "schedule_changed": False, "other_runtime_files_changed": False}


def apply(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    runtime = Path(manifest["runtime"])
    current = plan(runtime)
    if current["source_commit"] != manifest["source_commit"] or current["files"] != manifest["files"]:
        raise RuntimeError("runtime or source changed after release plan; regenerate manifest")
    backups = manifest_path.parent / (manifest_path.stem + "-backups")
    backups.mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        if not item["changed"]:
            continue
        relative = item["path"]
        target = runtime / relative
        if target.exists():
            backup = backups / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(target.read_bytes())
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (ROOT / relative).read_bytes()
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if _sha(target.read_bytes()) != item["after_sha256"]:
            raise RuntimeError(f"post-deployment hash mismatch: {relative}")
    manifest["applied_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        manifest = apply(args.manifest)
        status = "deployed"
    else:
        manifest = plan(args.runtime)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        status = "planned"
    print(json.dumps({"status": status, "source_commit": manifest["source_commit"],
                      "changed_files": [item["path"] for item in manifest["files"] if item["changed"]]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
