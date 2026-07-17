"""Read-only identity-resolution planning for V4 backfill campaigns."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from src.common_ticker import normalize_ticker
from src.period_normalizer import _is_invalid, _normalize_quarter
from src.segment.segment_zip_resolver import _load_sidecar_provenance
from src.segment.zip_identity_verifier import extract_actual_metadata_from_zip


CLASSIFICATIONS = (
    "READY_IDENTITY_VERIFIED",
    "TARGET_ZIP_NEEDS_SIDECAR",
    "LEGACY_CACHE_COPY_CANDIDATE",
    "METADATA_RESOLVED_CACHE_MISSING",
    "METADATA_INCOMPLETE_CACHE_MISSING",
    "CACHE_IDENTITY_MISMATCH",
    "TARGET_CACHE_CONFLICT",
    "LEGACY_STATE_AMBIGUOUS",
    "JQUANTS_METADATA_AMBIGUOUS",
    "INVALID_OR_UNSUPPORTED_URL",
    "NOT_APPLICABLE",
    "OTHER_UNRESOLVED",
)

OUTPUT_FILES = {
    "identity-plan-results-46218.jsonl": None,
    "identity-plan-summary.json": None,
    "classification-counts.json": None,
    "ready-identity-verified.jsonl": {"READY_IDENTITY_VERIFIED"},
    "target-zip-needs-sidecar.jsonl": {"TARGET_ZIP_NEEDS_SIDECAR"},
    "legacy-copy-candidates.jsonl": {"LEGACY_CACHE_COPY_CANDIDATE"},
    "metadata-resolved-cache-missing.jsonl": {"METADATA_RESOLVED_CACHE_MISSING"},
    "identity-mismatches.jsonl": {"CACHE_IDENTITY_MISMATCH", "TARGET_CACHE_CONFLICT"},
    "ambiguous-groups.json": None,
    "unsupported-or-unresolved.jsonl": {
        "METADATA_INCOMPLETE_CACHE_MISSING",
        "INVALID_OR_UNSUPPORTED_URL",
        "NOT_APPLICABLE",
        "OTHER_UNRESOLVED",
    },
    "source-schema.json": None,
    "execution.json": None,
}

_REQUESTED_ID_RE = re.compile(r"(?:0812|1401)(20\d{12})|(?<!\d)(20\d{12})(?!\d)")


class IdentityPlanStop(RuntimeError):
    """Structured fail-closed stop raised before producing a partial plan."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_absolute() or not path.is_file():
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_INPUT_INVALID")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_schema(conn: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    return [dict(row) for row in conn.execute(f"PRAGMA table_info([{table}])")]


def _official_zip_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlparse(text)
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"www.release.tdnet.info", "release.tdnet.info"}
        and parsed.path.lower().endswith(".zip")
    ):
        return text
    return ""


def _requested_id_from_url(value: object) -> str:
    match = _REQUESTED_ID_RE.search(str(value or ""))
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[dict[str, object]]) -> bytes:
    return b"".join(_json_bytes(row) for row in rows)


def load_campaign(
    path: Path,
    campaign_id: str,
    *,
    expected_count: int | None,
    expected_sha256: str | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if expected_sha256 and sha256_file(path).lower() != expected_sha256.lower():
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_CAMPAIGN_CHANGED")
    conn = _connect_ro(path)
    try:
        schema = _table_schema(conn, "campaign_filings")
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id",
            (campaign_id,),
        )]
    finally:
        conn.close()
    if expected_count is not None and len(rows) != expected_count:
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_CAMPAIGN_CHANGED")
    if len({str(row["manifest_row_id"]) for row in rows}) != len(rows):
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_CAMPAIGN_CHANGED")
    return rows, schema


def load_jquants(
    path: Path,
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    conn = _connect_ro(path)
    try:
        schema = _table_schema(conn, "jquants_financials_normalized")
        columns = {str(item["name"]) for item in schema}
        required = {
            "local_code", "disclosed_date", "current_fiscal_year_end_date",
            "type_of_current_period", "type_of_document", "raw_json",
        }
        if not required.issubset(columns):
            raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_JQUANTS_SCHEMA_UNRESOLVED")
        source = conn.execute(
            "SELECT local_code, disclosed_date, current_fiscal_year_end_date, "
            "type_of_current_period, type_of_document, raw_json "
            "FROM jquants_financials_normalized WHERE raw_json IS NOT NULL"
        )
        grouped: dict[str, dict[tuple[object, ...], dict[str, object]]] = defaultdict(dict)
        for row in source:
            try:
                raw = json.loads(row["raw_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            requested = raw.get("DiscNo")
            if not isinstance(requested, str) or len(requested) != 14 or not requested.isdigit():
                continue
            quarter = _normalize_quarter(str(row["type_of_current_period"] or ""))
            period = str(row["current_fiscal_year_end_date"] or "").strip()
            ticker = normalize_ticker(str(row["local_code"] or ""))
            valid_period = not _is_invalid(period)
            valid_quarter = quarter in {"1Q", "2Q", "3Q", "FY"}
            item = {
                "requested_disclosure_no": requested,
                "company_code": str(row["local_code"] or ""),
                "normalized_company_code": ticker,
                "disclosed_date": str(row["disclosed_date"] or ""),
                "expected_period": period if valid_period else "",
                "expected_quarter": quarter if valid_quarter else "",
                "period_type": str(row["type_of_current_period"] or ""),
                "document_type": str(row["type_of_document"] or raw.get("DocType") or ""),
                "disclosed_time": str(raw.get("DiscTime") or ""),
                "match_status": "exact" if valid_period and valid_quarter and ticker else "incomplete",
            }
            key = tuple(item[name] for name in (
                "normalized_company_code", "disclosed_date", "expected_period",
                "expected_quarter", "document_type",
            ))
            grouped[requested][key] = item
    finally:
        conn.close()
    return {key: list(values.values()) for key, values in grouped.items()}, schema


def load_legacy_state(
    path: Path,
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    conn = _connect_ro(path)
    try:
        schema = _table_schema(conn, "filing_state")
        rows = conn.execute(
            "SELECT filing_id, ticker, disclosure_date, doc_type, period, quarter, "
            "source_url, xbrl_url, xbrl_path, cache_dir FROM filing_state"
        )
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            requested = _requested_id_from_url(row["xbrl_url"])
            if requested:
                grouped[requested].append(dict(row))
    finally:
        conn.close()
    return grouped, schema


def inspect_zip(path: Path, expected_period: str, expected_quarter: str) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path), "exists": path.is_file(), "valid": False,
        "zip_sha256": "", "ticker": "", "period": "", "quarter": "",
        "document_type": "", "internal_document_id": "", "error": "",
    }
    if not path.is_file():
        result["error"] = "ZIP_MISSING"
        return result
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if archive.testzip() is not None:
                result["error"] = "ZIP_CORRUPT"
                return result
        result["zip_sha256"] = sha256_file(path)
        meta = extract_actual_metadata_from_zip(
            str(path), expected_period=expected_period, expected_quarter=expected_quarter,
        )
        result.update(meta)
        result["valid"] = all(meta.get(name) for name in (
            "ticker", "period", "quarter", "document_type", "internal_document_id",
        ))
        if not result["valid"]:
            result["error"] = "ZIP_METADATA_UNRESOLVED"
    except (OSError, zipfile.BadZipFile):
        result["error"] = "ZIP_CORRUPT"
    return result


def _identity_matches(actual: dict[str, object], expected: dict[str, object]) -> tuple[bool, list[str]]:
    mismatches: list[str] = []
    checks = (
        ("ticker", "normalized_company_code"),
        ("period", "expected_period"),
        ("quarter", "expected_quarter"),
    )
    for actual_key, expected_key in checks:
        expected_value = str(expected.get(expected_key) or "")
        actual_value = str(actual.get(actual_key) or "")
        if expected_value and actual_value != expected_value:
            mismatches.append(actual_key.upper() + "_MISMATCH")
    return not mismatches, mismatches


def _not_applicable(document_type: str) -> bool:
    lowered = document_type.lower()
    return any(token in lowered for token in (
        "dividendforecastrevision", "earningsforecastrevision", "forecastrevision",
    ))


def _legacy_zip_paths(
    rows: list[dict[str, object]], cache_root: Path, requested: str,
) -> list[tuple[dict[str, object], Path]]:
    result: list[tuple[dict[str, object], Path]] = []
    seen: set[str] = set()
    for row in rows:
        filing_id = str(row.get("filing_id") or "")
        if not filing_id or filing_id == requested or filing_id in seen:
            continue
        seen.add(filing_id)
        path = cache_root / filing_id / "xbrl.zip"
        if path.is_file():
            result.append((row, path))
    return result


def classify_row(
    row: dict[str, object],
    jquants_rows: list[dict[str, object]],
    state_rows: list[dict[str, object]],
    cache_root: Path,
) -> dict[str, object]:
    requested = str(row.get("requested_disclosure_no") or "")
    ticker = normalize_ticker(str(row.get("normalized_company_code") or row.get("company_code") or ""))
    target_zip = cache_root / requested / "xbrl.zip"
    target_sidecar = Path(str(target_zip) + ".provenance.json")
    legacy = _legacy_zip_paths(state_rows, cache_root, requested)
    flags: set[str] = set()
    if target_zip.is_file(): flags.add("HAS_TARGET_ZIP")
    if target_sidecar.is_file(): flags.add("HAS_TARGET_SIDECAR")
    if legacy: flags.add("HAS_LEGACY_ZIP")
    if any(char.isalpha() for char in ticker): flags.add("ALPHANUMERIC_TICKER")
    if len(state_rows) > 1: flags.add("STATE_REQUESTED_ID_DUPLICATE")

    exact_jq = [item for item in jquants_rows if item.get("match_status") == "exact"]
    if len(jquants_rows) == 1: flags.add("JQUANTS_EXACT_MATCH")
    if len(jquants_rows) > 1:
        classification, reason = "JQUANTS_METADATA_AMBIGUOUS", "MULTIPLE_DISTINCT_EXACT_METADATA"
        expected = {"normalized_company_code": ticker, "expected_period": row.get("expected_period") or "", "expected_quarter": "", "source": "manifest"}
        actual: dict[str, object] = {}
    else:
        jq = exact_jq[0] if len(exact_jq) == 1 else (jquants_rows[0] if len(jquants_rows) == 1 else None)
        expected = {
            "normalized_company_code": str((jq or {}).get("normalized_company_code") or ticker),
            "expected_period": str((jq or {}).get("expected_period") or row.get("expected_period") or ""),
            "expected_quarter": str((jq or {}).get("expected_quarter") or ""),
            "document_type": str((jq or {}).get("document_type") or row.get("document_type") or ""),
            "source": "jquants_exact" if jq and jq.get("match_status") == "exact" else "manifest",
            "quarter_status": "RESOLVED" if (jq or {}).get("expected_quarter") else "UNRESOLVED",
        }
        actual = {}
        target_info = inspect_zip(target_zip, expected["expected_period"], expected["expected_quarter"]) if target_zip.is_file() else None
        legacy_info = [
            (state, path, inspect_zip(path, expected["expected_period"], expected["expected_quarter"]))
            for state, path in legacy
        ]
        if target_info:
            actual = target_info
            if target_info.get("error") == "ZIP_CORRUPT": flags.add("ZIP_CORRUPT")
            matched, mismatches = _identity_matches(target_info, expected)
            if target_info.get("ticker") == expected["normalized_company_code"]: flags.add("TICKER_MATCH")
            if target_info.get("period") == expected["expected_period"]: flags.add("PERIOD_MATCH")
            if expected["expected_quarter"] and target_info.get("quarter") == expected["expected_quarter"]: flags.add("QUARTER_MATCH")
            if target_info.get("internal_document_id"): flags.add("INTERNAL_ID_AVAILABLE")
            conflicting_legacy = any(
                info.get("zip_sha256") and info.get("zip_sha256") != target_info.get("zip_sha256")
                for _state, _path, info in legacy_info
            )
            if conflicting_legacy:
                classification, reason = "TARGET_CACHE_CONFLICT", "TARGET_AND_LEGACY_ZIP_DIFFER"
            elif not target_info.get("valid") or not matched:
                classification, reason = "CACHE_IDENTITY_MISMATCH", str(target_info.get("error") or ",".join(mismatches))
            elif target_sidecar.is_file():
                sidecar = _load_sidecar_provenance(
                    str(target_zip), requested,
                    expected["expected_period"], expected["expected_quarter"],
                )
                if sidecar is None:
                    flags.add("SIDECAR_INVALID")
                    classification, reason = "TARGET_CACHE_CONFLICT", "SIDECAR_INVALID"
                else:
                    classification, reason = "READY_IDENTITY_VERIFIED", "TARGET_ZIP_AND_SIDECAR_VERIFIED"
            else:
                classification, reason = "TARGET_ZIP_NEEDS_SIDECAR", "TARGET_ZIP_VERIFIED_SIDECAR_MISSING"
        elif len(state_rows) > 1:
            classification, reason = "LEGACY_STATE_AMBIGUOUS", "MULTIPLE_STATE_IDENTITIES"
        elif len(legacy_info) == 1:
            _state, legacy_path, info = legacy_info[0]
            actual = info
            matched, mismatches = _identity_matches(info, expected)
            if info.get("error") == "ZIP_CORRUPT": flags.add("ZIP_CORRUPT")
            if info.get("ticker") == expected["normalized_company_code"]: flags.add("TICKER_MATCH")
            if info.get("period") == expected["expected_period"]: flags.add("PERIOD_MATCH")
            if expected["expected_quarter"] and info.get("quarter") == expected["expected_quarter"]: flags.add("QUARTER_MATCH")
            if info.get("internal_document_id"): flags.add("INTERNAL_ID_AVAILABLE")
            if not info.get("valid") or not matched:
                classification, reason = "CACHE_IDENTITY_MISMATCH", str(info.get("error") or ",".join(mismatches))
            else:
                classification, reason = "LEGACY_CACHE_COPY_CANDIDATE", "UNIQUE_LEGACY_ZIP_VERIFIED"
                actual = {**info, "path": str(legacy_path)}
        else:
            url = _official_zip_url(row.get("normalized_xbrl_url"))
            if jq and _not_applicable(str(jq.get("document_type") or "")):
                classification, reason = "NOT_APPLICABLE", "JQUANTS_DOCUMENT_TYPE_NOT_XBRL_TARGET"
            elif not url:
                classification, reason = "INVALID_OR_UNSUPPORTED_URL", "OFFICIAL_XBRL_URL_INVALID"
            elif jq and jq.get("match_status") == "exact" and all((
                expected["normalized_company_code"], expected["expected_period"],
                expected["expected_quarter"], url,
            )):
                flags.add("NETWORK_REQUIRED")
                classification, reason = "METADATA_RESOLVED_CACHE_MISSING", "EXACT_METADATA_RESOLVED_NO_CACHE"
            else:
                flags.add("NETWORK_REQUIRED")
                classification, reason = "METADATA_INCOMPLETE_CACHE_MISSING", "EXPECTED_IDENTITY_INCOMPLETE"

    sidecar_sha = sha256_file(target_sidecar) if target_sidecar.is_file() else ""
    legacy_path = str(legacy[0][1]) if len(legacy) == 1 else ""
    suggested = {
        "READY_IDENTITY_VERIFIED": "READY_FOR_V4_EXTRACTION",
        "TARGET_ZIP_NEEDS_SIDECAR": "CREATE_VERIFIED_SIDECAR",
        "LEGACY_CACHE_COPY_CANDIDATE": "COPY_VERIFIED_LEGACY_CACHE",
        "METADATA_RESOLVED_CACHE_MISSING": "DOWNLOAD_OFFICIAL_XBRL_LATER",
        "METADATA_INCOMPLETE_CACHE_MISSING": "RESOLVE_METADATA",
    }.get(classification, "MANUAL_REVIEW")
    return {
        "campaign_id": row.get("campaign_id"),
        "manifest_row_id": row.get("manifest_row_id"),
        "requested_disclosure_no": requested,
        "company_code": row.get("company_code"),
        "jquants_metadata": jquants_rows,
        "legacy_state_identities": state_rows,
        "target_cache_path": str(target_zip),
        "legacy_cache_path": legacy_path,
        "zip_sha256": str(actual.get("zip_sha256") or ""),
        "sidecar_sha256": sidecar_sha,
        "expected_identity": expected,
        "actual_identity": actual,
        "classification": classification,
        "reason_code": reason,
        "flags": sorted(flags),
        "suggested_next_action": suggested,
    }


def build_plan(
    *,
    campaign_db: Path,
    campaign_id: str,
    jquants_db: Path,
    legacy_state_db: Path,
    cache_root: Path,
    expected_count: int | None,
    campaign_db_sha256: str | None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if not cache_root.is_absolute() or not cache_root.is_dir():
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_INPUT_INVALID")
    campaign, campaign_schema = load_campaign(
        campaign_db, campaign_id, expected_count=expected_count,
        expected_sha256=campaign_db_sha256,
    )
    jquants, jquants_schema = load_jquants(jquants_db)
    state, state_schema = load_legacy_state(legacy_state_db)
    rows = [
        classify_row(
            row,
            jquants.get(str(row["requested_disclosure_no"]), []),
            state.get(str(row["requested_disclosure_no"]), []),
            cache_root,
        )
        for row in campaign
    ]
    rows.sort(key=lambda item: str(item["manifest_row_id"]))
    counts = Counter(str(row["classification"]) for row in rows)
    if sum(counts.values()) != len(campaign):
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_COUNT_MISMATCH")
    source_schema = {
        "campaign_filings": campaign_schema,
        "jquants_financials_normalized": jquants_schema,
        "filing_state": state_schema,
    }
    return rows, source_schema


def write_plan(
    *,
    output_dir: Path,
    rows: list[dict[str, object]],
    source_schema: dict[str, object],
    execution: dict[str, object],
    repo_root: Path,
) -> dict[str, object]:
    if not output_dir.is_absolute():
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_OUTPUT_UNSAFE")
    try:
        output_dir.resolve().relative_to(repo_root.resolve())
    except ValueError:
        pass
    else:
        raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_OUTPUT_UNSAFE")
    output_dir.mkdir(parents=True, exist_ok=False)
    counts = dict(sorted(Counter(str(row["classification"]) for row in rows).items()))
    summary = {
        "input_count": len(rows), "output_count": len(rows),
        "classification_counts": counts,
        "network_calls": 0, "db_writes": 0, "cache_writes": 0,
    }
    ambiguous = [row for row in rows if row["classification"] in {
        "LEGACY_STATE_AMBIGUOUS", "JQUANTS_METADATA_AMBIGUOUS",
    }]
    payloads: dict[str, bytes] = {
        "identity-plan-results-46218.jsonl": _jsonl_bytes(rows),
        "identity-plan-summary.json": _json_bytes(summary),
        "classification-counts.json": _json_bytes(counts),
        "ambiguous-groups.json": _json_bytes(ambiguous),
        "source-schema.json": _json_bytes(source_schema),
        "execution.json": _json_bytes(execution),
    }
    for name, selected in OUTPUT_FILES.items():
        if name in payloads or selected is None:
            continue
        payloads[name] = _jsonl_bytes(row for row in rows if row["classification"] in selected)
    for name, content in payloads.items():
        (output_dir / name).write_bytes(content)
    digests = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(payloads.items())}
    (output_dir / "digests.json").write_bytes(_json_bytes(digests))
    return {**summary, "output_dir": str(output_dir), "digests": digests}
