"""Deterministic, read-only V4 campaign manifest registration dry-run."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

_REQUESTED_RE = re.compile(r"(?:0812|1401)(20\d{12})")
_OUTPUT_FILES = (
    "campaign.json", "manifest-schema.json", "registration-candidates.jsonl",
    "registration-classification.jsonl", "registration-summary.json",
    "requested-id-duplicate-groups.json", "rejected-rows.jsonl",
)


def _json_bytes(value: object, *, newline: bool = False) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if newline:
        text += "\n"
    return text.encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    data = _json_bytes(value, newline=True)
    path.write_bytes(data)
    return data


def _write_jsonl(path: Path, rows: list[dict]) -> bytes:
    data = b"".join(_json_bytes(row, newline=True) for row in rows)
    path.write_bytes(data)
    return data


def _normalize_ticker(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _official_url(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.hostname not in {"www.release.tdnet.info", "release.tdnet.info"}:
        return None
    return text


def _requested_id(row: dict) -> str | None:
    for key in ("requested_disclosure_no", "disclosure_no", "disc_no"):
        value = str(row.get(key) or "").strip()
        if re.fullmatch(r"20\d{12}", value):
            return value
    for value in (row.get("xbrl_url"), row.get("doc_url")):
        match = _REQUESTED_RE.search(str(value or ""))
        if match:
            return match.group(1)
    return None


def _load_manifest(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("filings", raw.get("records"))
    if not isinstance(raw, list):
        raise ValueError("manifest must be a list or contain filings/records list")
    if not all(isinstance(row, dict) for row in raw):
        raise ValueError("manifest rows must be objects")
    return raw


def _classify(row: dict, requested_counts: Counter[str]) -> tuple[str, str | None, dict]:
    requested = _requested_id(row)
    company = _normalize_ticker(row.get("ticker") or row.get("company_code"))
    source = str(row.get("doc_url") or "").strip() or None
    xbrl = str(row.get("xbrl_url") or "").strip() or None
    if not requested:
        return "MISSING_REQUESTED_ID", "requested_disclosure_no_missing", {"requested": None, "company": company, "source_url": source, "xbrl_url": xbrl}
    if not company:
        return "MISSING_COMPANY_CODE", "company_code_missing", {"requested": requested, "company": None, "source_url": source, "xbrl_url": xbrl}
    if not source and not xbrl:
        return "MISSING_URL", "source_and_xbrl_url_missing", {"requested": requested, "company": company, "source_url": None, "xbrl_url": None}
    urls = [u for u in (source, xbrl) if u]
    if not any(_official_url(u) for u in urls):
        return "INVALID_OFFICIAL_URL", "no_official_tdnet_https_url", {"requested": requested, "company": company, "source_url": source, "xbrl_url": xbrl}
    if requested_counts[requested] > 1:
        return "REQUESTED_ID_DUPLICATE", "requested_disclosure_no_duplicate", {"requested": requested, "company": company, "source_url": source, "xbrl_url": xbrl}
    period_value = row.get("expected_period") or row.get("fiscal_period") or row.get("current_fiscal_year_end_date")
    quarter_value = row.get("expected_quarter") or row.get("quarter")
    if not period_value or not quarter_value or not row.get("doc_type"):
        reason = "MISSING_EXPECTED_QUARTER" if not quarter_value else "period_or_document_type_missing"
        return "METADATA_INCOMPLETE", reason, {"requested": requested, "company": company, "source_url": source, "xbrl_url": xbrl}
    return "REGISTERABLE", None, {"requested": requested, "company": company, "source_url": source, "xbrl_url": xbrl}


def _candidate(row: dict, row_id: str, classification: str, reason: str | None, code_sha: str, worker_version: str, campaign_id: str) -> dict:
    source = str(row.get("doc_url") or "").strip() or None
    xbrl = str(row.get("xbrl_url") or "").strip() or None
    requested = _requested_id(row)
    company = _normalize_ticker(row.get("ticker") or row.get("company_code"))
    registerable = classification in {"REGISTERABLE", "REQUESTED_ID_DUPLICATE", "METADATA_INCOMPLETE"}
    return {
        "campaign_id": campaign_id, "manifest_row_id": row_id, "state_filing_id": None,
        "requested_disclosure_no": requested, "company_code": str(row.get("ticker") or "").strip() or None,
        "normalized_company_code": company, "source_url": source, "normalized_xbrl_url": _official_url(xbrl),
        "disclosure_date": row.get("disclosure_date"), "expected_period": row.get("expected_period") or row.get("fiscal_period") or row.get("current_fiscal_year_end_date"), "expected_quarter": row.get("expected_quarter") or row.get("quarter"),
        "document_type": row.get("doc_type"), "internal_document_id": None, "zip_sha256": None,
        "zip_internal_ticker": None, "zip_internal_period": None, "zip_internal_quarter": None,
        "run_id": None, "worker_version": worker_version, "extractor_version": None,
        "extractor_route": None, "code_sha": code_sha, "registration_status": "REGISTERED" if registerable else "REJECTED",
        "identity_status": "UNVERIFIED", "cache_status": "UNKNOWN", "extraction_status": "NOT_STARTED",
        "sqlite_save_status": "NOT_STARTED", "canonical_save_status": "NOT_STARTED",
        "supabase_save_status": "NOT_STARTED", "overall_status": "REGISTERED" if registerable else "REJECTED",
        "error_code": reason, "error_stage": "registration" if reason else None,
        "error_message": reason, "retryable": True,
        "classification": classification,
    }


def _assert_output_dir(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("--output-dir must be absolute")
    repo = Path(__file__).resolve().parents[1]
    try:
        path.resolve().relative_to(repo)
    except ValueError:
        pass
    else:
        raise ValueError("--output-dir must be outside repository")


def run_dry_run(*, manifest: Path, manifest_sha256: str, campaign_id: str, campaign_name: str, code_sha: str, worker_version: str, output_dir: Path, expected_count: int | None = None, working_tree_diff_sha256: str | None = None) -> dict:
    _assert_output_dir(output_dir)
    actual_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if actual_sha.lower() != manifest_sha256.lower():
        raise ValueError("manifest SHA-256 mismatch")
    rows = _load_manifest(manifest)
    if expected_count is not None and len(rows) != expected_count:
        raise ValueError("manifest count mismatch")
    requested_counts = Counter(filter(None, (_requested_id(row) for row in rows)))
    candidates, classifications, rejected = [], [], []
    for index, row in enumerate(rows, 1):
        row_id = f"{index:010d}"
        cls, reason, _ = _classify(row, requested_counts)
        candidate = _candidate(row, row_id, cls, reason, code_sha, worker_version, campaign_id)
        candidates.append(candidate)
        classifications.append({"manifest_row_id": row_id, "classification": cls, "reason_code": reason})
        if cls not in {"REGISTERABLE", "REQUESTED_ID_DUPLICATE", "METADATA_INCOMPLETE"}:
            rejected.append(candidate)
    duplicate_groups = []
    grouped = defaultdict(list)
    for candidate in candidates:
        if candidate["requested_disclosure_no"] and requested_counts[candidate["requested_disclosure_no"]] > 1:
            grouped[candidate["requested_disclosure_no"]].append(candidate)
    for requested, group in sorted(grouped.items()):
        duplicate_groups.append({"requested_disclosure_no": requested, "manifest_row_ids": [r["manifest_row_id"] for r in group], "company_codes": sorted({r["normalized_company_code"] for r in group if r["normalized_company_code"]}), "urls": sorted({r["normalized_xbrl_url"] for r in group if r["normalized_xbrl_url"]}), "row_count": len(group)})
    output_dir.mkdir(parents=True, exist_ok=False)
    campaign = {"campaign_id": campaign_id, "campaign_name": campaign_name, "manifest_path": str(manifest), "manifest_sha256": actual_sha, "manifest_record_count": len(rows), "code_sha": code_sha, "worker_version": worker_version, "created_at": None, "dry_run": True}
    schema = {"source_keys": {"filing_id": "state_filing_id (not resolved in dry-run)", "ticker": "company_code/normalized_company_code", "doc_url": "source_url", "xbrl_url": "normalized_xbrl_url", "disclosure_date": "disclosure_date", "current_fiscal_year_end_date": "expected_period (null; no inference)", "doc_type": "document_type"}, "manifest_keys": sorted(rows[0].keys()) if rows else [], "manifest_row_id": "1-based row position, zero-padded to 10 digits"}
    counts = Counter(item["classification"] for item in classifications)
    summary = {"input_count": len(rows), "output_count": len(candidates), "classification_counts": dict(sorted(counts.items())), "requested_id_distinct_count": len(requested_counts), "duplicate_group_count": len(duplicate_groups), "duplicate_row_count": sum(len(g["manifest_row_ids"]) for g in duplicate_groups), "missing_field_counts": {k: counts.get(k, 0) for k in ("MISSING_REQUESTED_ID", "MISSING_COMPANY_CODE", "MISSING_URL")}, "registerable_count": counts.get("REGISTERABLE", 0), "registration_candidate_count": len(candidates) - len(rejected), "rejected_count": len(rejected)}
    payloads = {"campaign.json": campaign, "manifest-schema.json": schema, "registration-summary.json": summary, "requested-id-duplicate-groups.json": duplicate_groups}
    digests = {"manifest": actual_sha}
    for name, value in payloads.items():
        data = _write_json(output_dir / name, value)
        digests[name] = _sha_bytes(data)
    for name, value in (("registration-candidates.jsonl", candidates), ("registration-classification.jsonl", classifications), ("rejected-rows.jsonl", rejected)):
        data = _write_jsonl(output_dir / name, value)
        digests[name] = _sha_bytes(data)
    digests["semantic_digest"] = _sha_bytes(_json_bytes({k: digests[k] for k in sorted(digests) if k != "semantic_digest"}))
    _write_json(output_dir / "digests.json", digests)
    _write_json(output_dir / "execution.json", {"dry_run": True, "working_tree_code_present": True, "working_tree_diff_sha256": working_tree_diff_sha256, "network_calls": 0, "db_connections": 0, "cache_access": 0, "input_count": len(rows), "output_count": len(candidates)})
    return {"summary": summary, "digests": digests, "output_dir": str(output_dir)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V4 campaign manifest registration dry-run (read-only; no DB/cache/network)")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--worker-version", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--working-tree-diff-sha256", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_dry_run(manifest=Path(args.manifest), manifest_sha256=args.manifest_sha256, campaign_id=args.campaign_id, campaign_name=args.campaign_name, code_sha=args.code_sha, worker_version=args.worker_version, output_dir=Path(args.output_dir), expected_count=args.expected_count, working_tree_diff_sha256=args.working_tree_diff_sha256)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"STOP_V4_CAMPAIGN_MANIFEST_DRYRUN_FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
