from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from lib.backfill.fresh_seed_cache_builder import build_record, canonical_records_bytes, semantic_sha256


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build trusted isolated seed caches from Fresh artifacts.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--source-cache-root", required=True)
    parser.add_argument("--output-cache-root", required=True)
    parser.add_argument("--output-summary", required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--confirm-git-head", required=True)
    parser.add_argument("--confirm-source-cache-root", required=True)
    parser.add_argument("--confirm-output-cache-root", required=True)
    parser.add_argument("--selection-manifest")
    parser.add_argument("--selection-sha256", help="Deprecated alias for raw selection manifest SHA-256.")
    parser.add_argument("--selection-byte-sha256", help="Raw selection manifest file SHA-256.")
    parser.add_argument("--selection-semantic-sha256", help="Canonical semantic digest of parsed selection records.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--materialize", action="store_true")
    parser.add_argument("--write-output", action="store_true")
    parser.add_argument("--confirm-write-output", action="store_true")
    return parser


def _safe_output(path: Path) -> bool:
    resolved = path.resolve()
    temp_root = Path("C:/tmp").resolve()
    try:
        resolved.relative_to(temp_root)
        try:
            resolved.relative_to(Path.cwd().resolve())
            return False
        except ValueError:
            return not resolved.is_symlink()
    except ValueError:
        return False


def _validate_selection_hashes(
    raw: bytes,
    rows: list[dict],
    *,
    alias_sha256: str | None,
    byte_sha256: str | None,
    semantic_digest: str | None,
) -> None:
    if alias_sha256 and byte_sha256 and alias_sha256.lower() != byte_sha256.lower():
        raise SystemExit("selection byte hash arguments conflict")
    expected_byte = byte_sha256 or alias_sha256
    if not expected_byte or hashlib.sha256(raw).hexdigest() != expected_byte.lower():
        raise SystemExit("selection byte sha mismatch")
    if not semantic_digest or semantic_sha256(rows) != semantic_digest.lower():
        raise SystemExit("selection semantic sha mismatch")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest_path = Path(args.selection_manifest or args.manifest)
    raw = manifest_path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    byte_digest = hashlib.sha256(raw).hexdigest()
    if args.selection_manifest:
        _validate_selection_hashes(
            raw,
            rows,
            alias_sha256=args.selection_sha256,
            byte_sha256=args.selection_byte_sha256,
            semantic_digest=args.selection_semantic_sha256,
        )
    elif byte_digest != args.manifest_sha256.lower():
        raise SystemExit("manifest sha mismatch")
    if len(rows) != args.expected_count:
        raise SystemExit("expected count mismatch")
    requested = [str(row["requested_disclosure_no"]) for row in rows]
    filing_ids = [str(row["filing_id"]) for row in rows]
    if len(set(requested)) != len(requested) or len(set(filing_ids)) != len(filing_ids):
        raise SystemExit("duplicate selection identity")
    output_root = Path(args.output_cache_root).resolve()
    if str(output_root) != str(Path(args.confirm_output_cache_root).resolve()) or not _safe_output(output_root):
        raise SystemExit("STOP_V4_SEED_BUILDER_OUTPUT_ROOT_UNSAFE")
    summary_path = Path(args.output_summary).resolve()
    if not _safe_output(summary_path):
        raise SystemExit("STOP_V4_SEED_BUILDER_OUTPUT_ROOT_UNSAFE")
    source_root = Path(args.source_cache_root).resolve()
    if source_root != Path(args.confirm_source_cache_root).resolve():
        raise SystemExit("source cache root confirmation mismatch")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != args.confirm_git_head:
        raise SystemExit("git head confirmation mismatch")
    do_write = args.materialize and args.write_output and args.confirm_write_output
    if args.materialize and not do_write:
        raise SystemExit("materialize requires both write confirmations")
    records = []
    checkpoint = output_root.parent / "checkpoints" / "plan-progress.json"
    for index, row in enumerate(rows, 1):
        records.append(build_record(row, source_root, output_root, do_write))
        if index % 250 == 0 or index == len(rows):
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temp = checkpoint.with_suffix(".tmp")
            temp.write_text(json.dumps({"completed": index, "next_offset": index, "failed": 0}, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp, checkpoint)
    summary = {
        "count": len(records), "created": sum(r["materialization"] == "created" for r in records),
        "reused": sum(r["materialization"] == "reused" for r in records),
        "planned": sum(r["materialization"] == "planned" for r in records),
        "unresolved": 0, "records_semantic_sha256": semantic_sha256(records),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(canonical_records_bytes([summary]))
    plan_path = summary_path.with_name("materialization-plan.jsonl")
    plan_path.write_bytes(canonical_records_bytes(records))
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
