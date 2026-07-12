"""Read-only lookup of J-Quants canonical filing metadata."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from src.common_ticker import normalize_ticker
from src.period_normalizer import _is_invalid, _normalize_quarter, normalize_period_and_quarter


@dataclass(frozen=True)
class CanonicalFilingMetadata:
    requested_disclosure_no: str = ""
    expected_period: str = ""
    expected_quarter: str = ""
    normalized_ticker: str = ""
    local_code: str = ""
    disclosed_date: str = ""
    match_status: str = "not_found"
    error_reason: str = ""


def load_canonical_filing_metadata_index(db_path: str = "data/jquants.db") -> dict[str, CanonicalFilingMetadata]:
    """Load a DiscNo-only index without modifying the source SQLite database."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT local_code, disclosed_date, current_fiscal_year_end_date, "
            "type_of_current_period, raw_json FROM jquants_financials_normalized "
            "WHERE raw_json IS NOT NULL"
        )
        grouped: dict[str, list[CanonicalFilingMetadata]] = {}
        for local_code, disclosed_date, fiscal_end, period_type, raw in rows:
            try:
                payload = json.loads(raw)
                disc_no = payload.get("DiscNo")
            except (TypeError, ValueError):
                continue
            if not isinstance(disc_no, str) or len(disc_no) != 14 or not disc_no.isdigit():
                continue
            current_period_end = payload.get("CurPerEnd") or payload.get("CurrentPeriodEndDate")
            normalized_quarter = _normalize_quarter(str(period_type or ""))
            if _is_invalid(str(fiscal_end or "")) or _is_invalid(str(current_period_end or "")):
                item = CanonicalFilingMetadata(
                    requested_disclosure_no=disc_no,
                    local_code=local_code,
                    disclosed_date=disclosed_date,
                    match_status="invalid_period",
                )
            elif _is_invalid(normalized_quarter) or normalized_quarter not in {"1Q", "2Q", "3Q", "FY"}:
                item = CanonicalFilingMetadata(
                    requested_disclosure_no=disc_no,
                    local_code=local_code,
                    disclosed_date=disclosed_date,
                    match_status="invalid_quarter",
                )
            else:
                _period, expected_period, expected_quarter = normalize_period_and_quarter(
                    current_period_end,
                    fiscal_end,
                    period_type,
                    "",
                )
                item = CanonicalFilingMetadata(
                    requested_disclosure_no=disc_no,
                    expected_period=expected_period,
                    expected_quarter=expected_quarter,
                    normalized_ticker=normalize_ticker(local_code),
                    local_code=local_code,
                    disclosed_date=disclosed_date,
                    match_status="exact_requested_disclosure_match",
                )
            grouped.setdefault(disc_no, []).append(item)
        index: dict[str, CanonicalFilingMetadata] = {}
        for disc_no, items in grouped.items():
            if len(items) == 1:
                index[disc_no] = items[0]
            else:
                index[disc_no] = CanonicalFilingMetadata(
                    requested_disclosure_no=disc_no,
                    match_status="duplicate",
                    error_reason="duplicate_canonical_metadata",
                )
        return index
    finally:
        conn.close()
