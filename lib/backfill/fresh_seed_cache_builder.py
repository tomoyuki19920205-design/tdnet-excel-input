"""Build trusted, isolated TDNET seed caches from Fresh campaign artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from src.segment.zip_identity_verifier import extract_actual_metadata_from_zip

SIDECAR_FIELDS = (
    "schema_version", "source", "requested_disclosure_no", "requested_file_type",
    "internal_document_id", "zip_sha256", "downloaded_size", "ticker", "period",
    "quarter", "document_type", "fetched_at", "resolved_by_function",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_records_bytes(rows: list[dict]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")


def semantic_sha256(rows: list[dict]) -> str:
    return hashlib.sha256(canonical_records_bytes(rows)).hexdigest()


def canonical_sidecar_bytes(sidecar: dict) -> bytes:
    return (json.dumps(sidecar, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def build_record(row: dict, source_root: Path, output_root: Path, materialize: bool = False) -> dict:
    requested_id = str(row["requested_disclosure_no"])
    if Path(requested_id).name != requested_id or requested_id in {".", ".."}:
        raise ValueError("unsafe requested disclosure ID")
    source = Path(row["zip_path"]).resolve()
    source_root = source_root.resolve()
    if not _within(source, source_root):
        raise ValueError("source ZIP is outside confirmed source cache root")
    provenance_path = source.parent / "provenance.json"
    if not source.is_file() or not provenance_path.is_file():
        raise ValueError("source artifact missing")
    source_sha = sha256(source)
    fresh = json.loads(provenance_path.read_text(encoding="utf-8"))
    actual = extract_actual_metadata_from_zip(
        str(source), expected_period=row["expected_period"], expected_quarter=row["expected_quarter"]
    )
    valid = (
        fresh.get("source_route") == "JQUANTS_TD_FILES"
        and fresh.get("requested_disclosure_no") == requested_id
        and fresh.get("zip_sha256") == source_sha
        and actual["ticker"] == row["ticker"]
        and actual["period"] == row["expected_period"]
        and actual["quarter"] == row["expected_quarter"]
    )
    if not valid:
        raise ValueError("source identity conflict")
    sidecar = {
        "schema_version": "1", "source": "jquants", "requested_disclosure_no": requested_id,
        "requested_file_type": "x", "internal_document_id": actual["internal_document_id"],
        "zip_sha256": source_sha, "downloaded_size": source.stat().st_size,
        "ticker": actual["ticker"], "period": actual["period"], "quarter": actual["quarter"],
        "document_type": actual["document_type"], "fetched_at": fresh.get("downloaded_at", ""),
        "resolved_by_function": "fresh_seed_cache_builder",
    }
    destination = output_root / requested_id
    record = {
        "manifest_row_id": source.parent.name, "filing_id": row["filing_id"],
        "requested_disclosure_no": requested_id, "source_zip": str(source),
        "source_provenance": str(provenance_path), "source_zip_sha256": source_sha,
        "output_relative_path": f"{requested_id}/xbrl.zip", "sidecar": sidecar,
        "validation_status": "READY",
    }
    record["materialization"] = "planned"
    if materialize:
        zip_target = destination / "xbrl.zip"
        sidecar_target = destination / "xbrl.zip.provenance.json"
        expected_sidecar = canonical_sidecar_bytes(sidecar)
        if destination.exists():
            if not zip_target.is_file() or not sidecar_target.is_file():
                raise FileExistsError("existing output is incomplete")
            if sha256(zip_target) != source_sha or sidecar_target.read_bytes() != expected_sidecar:
                raise FileExistsError("existing output conflicts with source")
            record["materialization"] = "reused"
        else:
            destination.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(source, zip_target)
            if sha256(zip_target) != source_sha:
                raise OSError("copied ZIP digest mismatch")
            _atomic_write(sidecar_target, expected_sidecar)
            record["materialization"] = "created"
    return record
