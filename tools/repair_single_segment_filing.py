#!/usr/bin/env python3
"""Repair canonical segment rows for exactly one cached TDNET filing.

Dry-run is the default.  The command never downloads a ZIP and writes only to
``canonical_segments`` through the existing canonical writer when ``--apply``
is explicitly supplied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class RepairStop(RuntimeError):
    def __init__(self, judgment: str, detail: str = "") -> None:
        super().__init__(detail or judgment)
        self.judgment = judgment
        self.detail = detail


class RepairArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        option = re.search(r"--[a-z0-9-]+", message)
        field = option.group(0).removeprefix("--") if option else "arguments"
        reason = "missing_required_argument" if "required" in message else "invalid_cli_syntax"
        raise RepairStop(
            "STOP_SINGLE_SEGMENT_REPAIR_INVALID_ARGUMENT",
            f"field={field} expected=required_known_option reason={reason}",
        )


@dataclass
class RepairDeps:
    load_metadata: Callable[[argparse.Namespace], dict]
    resolve: Callable[..., Any]
    verify: Callable[..., Any]
    extract_detailed: Callable[..., Any]
    filter_detailed: Callable[..., Any]
    expand: Callable[..., tuple[list[dict], int]]
    select: Callable[..., list[dict]]
    writer: Callable[..., dict]
    read_config: Callable[[], dict]
    write_config: Callable[[], dict | None]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ids_in_value(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"requested_disclosure_no", "disclosure_no", "common_disclosure_no"}:
                text = str(item or "")
                if re.fullmatch(r"\d{14}", text):
                    found.add(text)
            found.update(_ids_in_value(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_ids_in_value(item))
    elif isinstance(value, str):
        for pdf_id in re.findall(r"(?<!\d)1401(\d{14})(?!\d)", value):
            found.add(pdf_id)
        for direct in re.findall(r"(?<!\d)(20\d{12})(?!\d)", value):
            found.add(direct)
    return found


def _default_load_metadata(args: argparse.Namespace) -> dict:
    from lib.pipeline.db import load_env, supabase_select

    load_env(str(_ROOT))
    rows = supabase_select(
        "tdnet_events",
        params={
            "source_doc_id": f"eq.{args.expected_canonical_filing_id}",
            "ticker": f"eq.{args.expected_ticker}",
            "event_type": "eq.earnings",
            "select": (
                "id,ticker,company_name,headline,disclosed_at,event_type,event_subtype,"
                "source_doc_id,source_url,pdf_url,raw_payload"
            ),
        },
    )
    exact = [row for row in rows if args.requested_id in _ids_in_value(row)]
    if not exact:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_FILING_NOT_FOUND")
    if len(exact) != 1:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_FILING_NOT_UNIQUE", f"matches={len(exact)}")
    row = dict(exact[0])
    row["requested_disclosure_no"] = args.requested_id
    row["canonical_filing_id"] = row.get("source_doc_id") or ""
    row["document_type"] = row.get("event_type") or ""
    row["cache_path"] = str(_ROOT / "data" / "tdnet_cache" / args.requested_id / "xbrl.zip")
    return row


def default_deps() -> RepairDeps:
    from lib.pipeline.canonical_writer import expand_segments_rows, write_segments_canonical
    from lib.pipeline.db import (
        get_supabase_read_config,
        get_supabase_write_config,
        load_env,
        supabase_select,
    )
    from src.events.earnings_production_pipeline import _extract_and_filter_segments_detailed
    from src.segment.segment_zip_resolver import resolve_xbrl_zip
    from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
    from src.segment.zip_identity_verifier import verify_zip_identity

    load_env(str(_ROOT))
    return RepairDeps(
        load_metadata=_default_load_metadata,
        resolve=resolve_xbrl_zip,
        verify=verify_zip_identity,
        extract_detailed=extract_segments_from_xbrl_zip_detailed,
        filter_detailed=_extract_and_filter_segments_detailed,
        expand=expand_segments_rows,
        select=supabase_select,
        writer=write_segments_canonical,
        read_config=get_supabase_read_config,
        write_config=get_supabase_write_config,
    )


def _validate_metadata(metadata: dict, args: argparse.Namespace) -> None:
    checks = {
        "requested_disclosure_no": args.requested_id,
        "ticker": args.expected_ticker,
        "canonical_filing_id": args.expected_canonical_filing_id,
    }
    mismatches = {
        key: {"expected": expected, "actual": str(metadata.get(key) or "")}
        for key, expected in checks.items()
        if str(metadata.get(key) or "") != expected
    }
    if mismatches:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_ARGUMENT_MISMATCH", json.dumps(mismatches))


def _expected_row_view(row: dict) -> dict:
    keys = (
        "source_row_key", "ticker", "period", "quarter", "segment_name", "segment_key",
        "metric", "value", "unit", "source", "source_system", "source_priority",
        "filing_id", "disclosure_datetime", "correction_flag",
    )
    return {key: row.get(key) for key in keys}


def _rows_equal(actual: dict, expected: dict) -> bool:
    keys = (
        "source_row_key", "ticker", "period", "quarter", "segment_name", "segment_key",
        "metric", "value", "unit", "source", "source_system", "source_priority",
        "filing_id", "disclosure_datetime", "correction_flag",
    )
    return all(actual.get(key) == expected.get(key) for key in keys)


def _classify_existing(existing: list[dict], payload: list[dict]) -> str:
    expected = {row["source_row_key"]: row for row in payload}
    by_key: dict[str, list[dict]] = {}
    for row in existing:
        by_key.setdefault(str(row.get("source_row_key") or ""), []).append(row)
    if any(len(rows) > 1 for rows in by_key.values()):
        return "E"
    present = set(by_key) & set(expected)
    if not present:
        return "A"
    if present != set(expected):
        return "C"
    if all(_rows_equal(by_key[key][0], expected[key]) for key in expected):
        return "B"
    return "D"


def _select_existing(deps: RepairDeps, payload: list[dict]) -> list[dict]:
    keys = [row["source_row_key"] for row in payload]
    if not keys:
        return []
    quoted = ",".join(f'"{key}"' for key in keys)
    return deps.select(
        "canonical_segments",
        params={"source_row_key": f"in.({quoted})", "select": "*"},
        config=deps.read_config(),
    )


def run_repair(args: argparse.Namespace, deps: RepairDeps | None = None) -> dict:
    deps = deps or default_deps()
    metadata = deps.load_metadata(args)
    _validate_metadata(metadata, args)

    cache_path = Path(str(metadata.get("cache_path") or ""))
    if not cache_path.is_file() or cache_path.stat().st_size <= 0:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_CACHE_MISSING")
    zip_sha = _sha256(cache_path)
    if zip_sha.lower() != args.expected_zip_sha256.lower():
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_ZIP_SHA_MISMATCH")

    resolved = deps.resolve(
        doc_id=args.requested_id,
        ticker=args.expected_ticker,
        expected_period=args.expected_period,
        expected_quarter=args.expected_quarter,
        cache_dir=str(cache_path.parent.parent),
        local_archive_dir=str(cache_path.parent / "__disabled_archive__"),
        allow_jquants_fetch=False,
        persist_provenance=False,
    )
    if not resolved.zip_path:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_CACHE_MISSING", resolved.error_reason)
    if Path(resolved.zip_path).resolve() != cache_path.resolve():
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_ARGUMENT_MISMATCH", "resolved cache path changed")
    provenance = resolved.trusted_provenance
    if provenance is None or provenance.internal_document_id != args.expected_internal_id:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_ARGUMENT_MISMATCH", "internal document ID mismatch")

    verdict = deps.verify(
        zip_path=str(cache_path),
        requested_disclosure_no=args.requested_id,
        expected_ticker=args.expected_ticker,
        expected_period=args.expected_period,
        expected_quarter=args.expected_quarter,
        trusted_provenance=provenance,
    )
    if not verdict.passed:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_REJECTED", verdict.rejection_reason)

    detailed = deps.extract_detailed(
        zip_path=str(cache_path), period=args.expected_period,
        quarter=args.expected_quarter, include_context_evidence=True,
    )
    raw_rows = list(detailed.segments if detailed else [])
    current_rows = []
    previous_count = 0
    for row in raw_rows:
        evidence = (row.raw_json or {}).get("_context_evidence") or {}
        role = evidence.get("current_or_previous") or evidence.get("context_period_type")
        if role == "previous":
            previous_count += 1
        if row.period == args.expected_period and row.quarter == args.expected_quarter and role == "current":
            if not (row.normalized_segment_name or row.raw_segment_name):
                continue
            if row.sales is None and row.profit is None:
                continue
            if not evidence:
                continue
            current_rows.append(row)

    logical, _ = deps.filter_detailed(
        str(cache_path), args.expected_period, args.expected_quarter,
        include_context_evidence=True,
    )
    current_names = {row.normalized_segment_name or row.raw_segment_name for row in current_rows}
    logical = [row for row in logical if row.get("segment_name") in current_names]
    if not logical:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_NO_CURRENT_SEGMENTS")

    payload, skipped = deps.expand(
        ticker=args.expected_ticker, period=args.expected_period, quarter=args.expected_quarter,
        segments=logical, source="xbrl", filing_id=args.expected_canonical_filing_id,
        disclosure_datetime=metadata.get("disclosed_at"), correction_flag=False,
        unit="millions_jpy",
    )
    segment_keys = [row["segment_key"] for row in payload if row.get("metric") == "sales"]
    if len(segment_keys) != len(set(segment_keys)) or len(logical) != len(set(segment_keys)):
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_DUPLICATE_SEGMENT_KEYS")

    existing = _select_existing(deps, payload)
    state = _classify_existing(existing, payload)
    stop_by_state = {
        "C": "STOP_SINGLE_SEGMENT_REPAIR_PARTIAL_EXISTING_ROWS",
        "D": "STOP_SINGLE_SEGMENT_REPAIR_CONFLICTING_EXISTING_ROWS",
        "E": "STOP_SINGLE_SEGMENT_REPAIR_DUPLICATE_EXISTING_ROWS",
    }
    if state in stop_by_state:
        raise RepairStop(stop_by_state[state])

    result = {
        "mode": "apply" if args.apply else "dry_run",
        "requested_id": args.requested_id,
        "internal_id": provenance.internal_document_id,
        "canonical_filing_id": args.expected_canonical_filing_id,
        "zip_path": str(cache_path),
        "zip_sha256": zip_sha,
        "identity_verdict": verdict.verdict,
        "extractor_total_rows": len(raw_rows),
        "current_target_rows": len(logical),
        "excluded_previous_rows": previous_count,
        "logical_segments": logical,
        "eav_rows": [_expected_row_view(row) for row in payload],
        "skipped_null_metrics": skipped,
        "existing_row_count": len(existing),
        "existing_state": state,
        "planned_upsert_count": 0 if state == "B" else len(payload),
    }
    if state == "B":
        result["final_judgment"] = "PASS_SINGLE_SEGMENT_REPAIR_ALREADY_PRESENT"
        return result
    if not args.apply:
        result["final_judgment"] = "PASS_SINGLE_SEGMENT_REPAIR_DRY_RUN_READY"
        return result

    write_config = deps.write_config()
    if not write_config:
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_ARGUMENT_MISMATCH", "write config unavailable")
    write_result = deps.writer(
        ticker=args.expected_ticker, period=args.expected_period, quarter=args.expected_quarter,
        segments=logical, source="xbrl", filing_id=args.expected_canonical_filing_id,
        disclosure_datetime=metadata.get("disclosed_at"), correction_flag=False,
        unit="millions_jpy", config=write_config,
    )
    if write_result.get("errors") or write_result.get("written") != len(payload):
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_READBACK_MISMATCH", json.dumps(write_result))
    readback = _select_existing(deps, payload)
    if _classify_existing(readback, payload) != "B":
        raise RepairStop("STOP_SINGLE_SEGMENT_REPAIR_READBACK_MISMATCH")
    result["readback_count"] = len(readback)
    result["final_judgment"] = "PASS_SINGLE_SEGMENT_REPAIR_APPLIED_AND_VERIFIED"
    return result


def _invalid_argument(field: str, expected: str, reason: str) -> None:
    raise RepairStop(
        "STOP_SINGLE_SEGMENT_REPAIR_INVALID_ARGUMENT",
        f"field={field} expected={expected} reason={reason}",
    )


def _validate_cli_args(args: argparse.Namespace) -> None:
    patterns = (
        ("requested-id", args.requested_id, r"[0-9]{14}", "14_ascii_digits"),
        ("expected-internal-id", args.expected_internal_id, r"[0-9]{14}", "14_ascii_digits"),
        ("expected-ticker", args.expected_ticker, r"[0-9A-Z]{4}", "4_uppercase_ascii_alnum"),
        ("expected-canonical-filing-id", args.expected_canonical_filing_id, r"[0-9a-fA-F]{64}", "64_hex"),
        ("expected-zip-sha256", args.expected_zip_sha256, r"[0-9a-fA-F]{64}", "64_hex"),
    )
    for field, value, pattern, expected in patterns:
        if re.fullmatch(pattern, value) is None:
            _invalid_argument(field, expected, "invalid_format")

    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", args.expected_period) is None:
        _invalid_argument("expected-period", "YYYY-MM-DD", "invalid_format")
    try:
        parsed_period = date.fromisoformat(args.expected_period)
    except ValueError:
        _invalid_argument("expected-period", "YYYY-MM-DD", "invalid_calendar_date")
    if parsed_period.isoformat() != args.expected_period:
        _invalid_argument("expected-period", "YYYY-MM-DD", "invalid_format")

    if args.expected_quarter not in {"FY", "1Q", "2Q", "3Q"}:
        _invalid_argument("expected-quarter", "FY|1Q|2Q|3Q", "unsupported_value")

    args.expected_canonical_filing_id = args.expected_canonical_filing_id.lower()
    args.expected_zip_sha256 = args.expected_zip_sha256.lower()


def build_parser() -> argparse.ArgumentParser:
    parser = RepairArgumentParser(description="Repair canonical segments for one cached filing")
    parser.add_argument("--requested-id", required=True)
    parser.add_argument("--expected-ticker", required=True)
    parser.add_argument("--expected-internal-id", required=True)
    parser.add_argument("--expected-canonical-filing-id", required=True)
    parser.add_argument("--expected-period", required=True)
    parser.add_argument("--expected-quarter", required=True)
    parser.add_argument("--expected-zip-sha256", required=True)
    parser.add_argument("--apply", action="store_true", help="write canonical_segments after all gates")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        _validate_cli_args(args)
        result = run_repair(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except RepairStop as exc:
        print(json.dumps({"final_judgment": exc.judgment, "detail": exc.detail}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({
            "final_judgment": "STOP_SINGLE_SEGMENT_REPAIR_UNEXPECTED_ERROR",
            "detail": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
