#!/usr/bin/env python3
"""既存earnings_reactionsへTDnetの正式決算開示時刻とセッションを付与する。"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.backfill.listing_sources.base import FilingInfo, canonicalize_url
from lib.backfill.listing_sources.tdnet_html import TdnetHtmlListingProvider
from src.events.env_loader import load_project_env
from src.events.tdnet_event_store import _get_supabase

EARNINGS_DATE = "2026-07-15"
JST = timezone(timedelta(hours=9))
DEFAULT_DB = PROJECT_ROOT / "data" / "jquants.db"
DEFAULT_INPUT_CSV = PROJECT_ROOT / "output" / f"earnings_reaction_{EARNINGS_DATE}.csv"
DEFAULT_OUTPUT_CSV = (
    PROJECT_ROOT / "output" / f"earnings_reaction_{EARNINGS_DATE}_with_release_time.csv"
)
RELEASE_COLUMNS = [
    "primary_event_id",
    "primary_event_title",
    "primary_earnings_published_at_jst",
    "release_time_jst",
    "release_session",
    "reaction_window_valid",
    "release_time_source",
    "release_time_status",
    "release_time_note",
]
RELEASE_COLUMN_TYPES = {
    "primary_event_id": "TEXT",
    "primary_event_title": "TEXT",
    "primary_earnings_published_at_jst": "TEXT",
    "release_time_jst": "TEXT",
    "release_session": "TEXT",
    "reaction_window_valid": "INTEGER NOT NULL DEFAULT 0",
    "release_time_source": "TEXT",
    "release_time_status": "TEXT",
    "release_time_note": "TEXT",
}
EXCLUDED_TITLE_TERMS = (
    "訂正", "再訂正", "数値データ訂正", "補足資料", "説明資料",
    "説明会資料", "決算説明", "presentation",
)


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).lower()


def is_formal_earnings_title(title: str) -> bool:
    normalized = normalize_title(title)
    return "決算短信" in normalized and not any(
        term in normalized for term in EXCLUDED_TITLE_TERMS
    )


def parse_published_at_jst(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text or not re.search(r"\d{1,2}:\d{2}", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def classify_release_session(
    published_at_jst: datetime | None,
    market_close: time | None,
) -> str:
    if published_at_jst is None or market_close is None:
        return "unknown"
    released = published_at_jst.time().replace(tzinfo=None)
    if released < time(9, 0):
        return "pre_open"
    if released < market_close:
        return "intraday"
    return "after_close"


def market_close_from_exchange(exchange: str | None) -> time | None:
    value = str(exchange or "").strip()
    if any(term in value for term in (
        "プライム", "スタンダード", "グロース", "東証", "東京証券取引所"
    )):
        return time(15, 30)
    return None


def _candidate_value(candidate: FilingInfo | dict[str, Any], field: str) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(field)
    return getattr(candidate, field)


def _candidate_description(candidate: FilingInfo | dict[str, Any]) -> dict[str, str]:
    return {
        "published_at": str(_candidate_value(candidate, "published_at") or ""),
        "title": str(_candidate_value(candidate, "title") or ""),
        "doc_url": str(_candidate_value(candidate, "doc_url") or ""),
    }


@dataclass
class CandidateSelection:
    candidate: FilingInfo | dict[str, Any] | None
    status: str
    note: str
    candidates: list[dict[str, str]]


def select_primary_candidate(
    candidates: Iterable[FilingInfo | dict[str, Any]],
) -> CandidateSelection:
    formal = [
        candidate for candidate in candidates
        if is_formal_earnings_title(str(_candidate_value(candidate, "title") or ""))
    ]
    descriptions = [_candidate_description(candidate) for candidate in formal]
    if not formal:
        return CandidateSelection(
            None, "not_found", "formal_earnings_candidate_not_found", []
        )

    valid_timed = []
    for candidate in formal:
        published = parse_published_at_jst(_candidate_value(candidate, "published_at"))
        if published is not None:
            valid_timed.append((candidate, published))
    if not valid_timed:
        status = "ambiguous" if len(formal) > 1 else "time_missing"
        return CandidateSelection(
            None, status, json.dumps(descriptions, ensure_ascii=False), descriptions
        )

    earliest = min(published for _, published in valid_timed)
    earliest_candidates = [
        candidate for candidate, published in valid_timed if published == earliest
    ]
    # 同一URL・同一タイトルの重複行は同一開示としてまとめる。
    unique: dict[tuple[str, str], FilingInfo | dict[str, Any]] = {}
    for candidate in earliest_candidates:
        key = (
            canonicalize_url(str(_candidate_value(candidate, "doc_url") or "")),
            normalize_title(str(_candidate_value(candidate, "title") or "")),
        )
        unique[key] = candidate
    if len(unique) != 1:
        return CandidateSelection(
            None, "ambiguous", json.dumps(descriptions, ensure_ascii=False), descriptions
        )

    selected = next(iter(unique.values()))
    note = ""
    if len(formal) > 1:
        note = f"selected_earliest_formal_candidate_from_{len(formal)}"
    return CandidateSelection(selected, "selected", note, descriptions)


def ensure_release_columns(conn: sqlite3.Connection) -> list[str]:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(earnings_reactions)")}
    if not existing:
        raise RuntimeError("earnings_reactions table not found")
    added: list[str] = []
    for column, sql_type in RELEASE_COLUMN_TYPES.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE earnings_reactions ADD COLUMN {column} {sql_type}"
            )
            added.append(column)
    conn.commit()
    return added


def load_reaction_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM earnings_reactions WHERE earnings_date = ? ORDER BY code",
        (EARNINGS_DATE,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_exchange_map(codes: list[str]) -> dict[str, str | None]:
    client = _get_supabase()
    result: dict[str, str | None] = {}
    for offset in range(0, len(codes), 50):
        response = (
            client.table("companies")
            .select("ticker_code,exchange")
            .in_("ticker_code", codes[offset:offset + 50])
            .execute()
        )
        for row in response.data or []:
            result[str(row["ticker_code"])] = row.get("exchange")
    return result


def build_release_updates(
    reaction_rows: list[dict[str, Any]],
    filings: list[FilingInfo | dict[str, Any]],
    exchange_map: dict[str, str | None],
) -> list[dict[str, Any]]:
    by_code: dict[str, list[FilingInfo | dict[str, Any]]] = defaultdict(list)
    for filing in filings:
        by_code[str(_candidate_value(filing, "ticker") or "")].append(filing)

    updates: list[dict[str, Any]] = []
    for row in reaction_rows:
        code = str(row["code"])
        selection = select_primary_candidate(by_code.get(code, []))
        candidate = selection.candidate
        published = (
            parse_published_at_jst(_candidate_value(candidate, "published_at"))
            if candidate is not None else None
        )
        exchange = exchange_map.get(code)
        close_time = market_close_from_exchange(exchange)

        if selection.status == "ambiguous":
            session = "ambiguous"
            status = "ambiguous"
        elif candidate is None:
            session = "unknown"
            status = selection.status
        else:
            session = classify_release_session(published, close_time)
            if published is None:
                status = "time_missing"
            elif close_time is None:
                status = "confirmed_time_market_unknown"
            else:
                status = "confirmed"

        note_parts = [selection.note] if selection.note else []
        if candidate is not None and close_time is None:
            note_parts.append(f"market_close_unknown: exchange={exchange or 'null'}")
        if candidate is not None and published is None:
            note_parts.append("published_at_has_no_confirmed_time")

        valid = bool(
            session == "after_close"
            and published is not None
            and row.get("close_2026_07_15_raw") is not None
            and row.get("open_2026_07_16_raw") is not None
        )
        updates.append({
            "source_event_id": row["source_event_id"],
            "code": code,
            "company_name": row["company_name"],
            "primary_event_id": row["source_event_id"] if candidate is not None else None,
            "primary_event_title": (
                str(_candidate_value(candidate, "title")) if candidate is not None else None
            ),
            "primary_earnings_published_at_jst": (
                published.isoformat(timespec="seconds") if published is not None else None
            ),
            "release_time_jst": (
                published.strftime("%H:%M") if published is not None else None
            ),
            "release_session": session,
            "reaction_window_valid": valid,
            "release_time_source": (
                "tdnet_html.published_at" if published is not None else None
            ),
            "release_time_status": status,
            "release_time_note": "; ".join(note_parts),
            "candidates": selection.candidates,
        })
    return updates


def save_updates(conn: sqlite3.Connection, updates: list[dict[str, Any]]) -> None:
    sql = """
        UPDATE earnings_reactions SET
          primary_event_id = ?, primary_event_title = ?,
          primary_earnings_published_at_jst = ?, release_time_jst = ?,
          release_session = ?, reaction_window_valid = ?, release_time_source = ?,
          release_time_status = ?, release_time_note = ?, updated_at = datetime('now')
        WHERE source_event_id = ? AND earnings_date = ?
    """
    before = conn.total_changes
    conn.executemany(sql, [(
        update["primary_event_id"], update["primary_event_title"],
        update["primary_earnings_published_at_jst"], update["release_time_jst"],
        update["release_session"], int(update["reaction_window_valid"]),
        update["release_time_source"], update["release_time_status"],
        update["release_time_note"], update["source_event_id"], EARNINGS_DATE,
    ) for update in updates])
    changed = conn.total_changes - before
    if changed != len(updates):
        conn.rollback()
        raise RuntimeError(
            f"DB update count mismatch: expected={len(updates)} actual={changed}"
        )
    conn.commit()


def write_enriched_csv(
    source_path: Path,
    output_path: Path,
    updates: list[dict[str, Any]],
) -> None:
    with source_path.open(encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
        original_columns = list(source_rows[0]) if source_rows else []
    update_by_code = {update["code"]: update for update in updates}
    if len(source_rows) != len(updates) or len(update_by_code) != len(updates):
        raise RuntimeError("CSV/DB count or code uniqueness mismatch")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*original_columns, *RELEASE_COLUMNS])
        writer.writeheader()
        for row in source_rows:
            update = update_by_code[row["code"]]
            enriched = dict(row)
            for column in RELEASE_COLUMNS:
                value = update[column]
                if column == "reaction_window_valid":
                    value = "true" if value else "false"
                enriched[column] = "" if value is None else value
            writer.writerow(enriched)


def summarize(updates: list[dict[str, Any]], added_columns: list[str]) -> dict[str, Any]:
    sessions = Counter(update["release_session"] for update in updates)
    missing_time = [
        {
            "code": update["code"],
            "company_name": update["company_name"],
            "reason": update["release_time_note"] or update["release_time_status"],
        }
        for update in updates
        if update["primary_earnings_published_at_jst"] is None
    ]
    ambiguous = [
        {
            "code": update["code"],
            "company_name": update["company_name"],
            "candidates": update["candidates"],
        }
        for update in updates if update["release_session"] == "ambiguous"
    ]
    unknown = [
        {
            "code": update["code"],
            "company_name": update["company_name"],
            "reason": update["release_time_note"] or update["release_time_status"],
        }
        for update in updates if update["release_session"] == "unknown"
    ]
    return {
        "input_count": len(updates),
        "after_close_count": sessions["after_close"],
        "intraday_count": sessions["intraday"],
        "pre_open_count": sessions["pre_open"],
        "unknown_count": sessions["unknown"],
        "ambiguous_count": sessions["ambiguous"],
        "reaction_window_valid_count": sum(
            update["reaction_window_valid"] for update in updates
        ),
        "release_time_missing": missing_time,
        "unknown": unknown,
        "ambiguous": ambiguous,
        "added_db_columns": added_columns,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT_CSV)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    args = parser.parse_args()

    load_project_env()
    with sqlite3.connect(args.db) as conn:
        added_columns = ensure_release_columns(conn)
        reactions = load_reaction_rows(conn)
        if len(reactions) != 121:
            raise RuntimeError(f"expected 121 reaction rows, got {len(reactions)}")
        codes = [str(row["code"]) for row in reactions]
        provider = TdnetHtmlListingProvider(rate_limit=0, max_pages_per_day=20)
        filings = provider.list_filings(
            EARNINGS_DATE,
            EARNINGS_DATE,
            tickers=codes,
            doc_types=["financial_statement"],
        )
        exchanges = fetch_exchange_map(codes)
        updates = build_release_updates(reactions, filings, exchanges)
        if len(updates) != len(reactions):
            raise RuntimeError("release update count mismatch")
        save_updates(conn, updates)

    write_enriched_csv(args.input_csv, args.output_csv, updates)
    report = summarize(updates, added_columns)
    report.update({
        "output_csv": str(args.output_csv.resolve()),
        "database": str(args.db.resolve()),
        "release_time_source": (
            "official TDnet HTML via existing TdnetHtmlListingProvider"
        ),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
