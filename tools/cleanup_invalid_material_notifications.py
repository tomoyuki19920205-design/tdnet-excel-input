#!/usr/bin/env python3
"""Audit/archive invalid clickable earnings-material notifications.

Default mode is read-only. ``--apply`` archives only definitive invalid links
(missing/relative URLs, HTTP 404/410, or a successful non-PDF response).
Transient request failures are reported but never mutated.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

import requests


EVENT_TYPES = ("earnings_material", "company_ir_material")
DEFINITIVE_HTTP_FAILURES = {404, 410}


def load_env(root: Path) -> None:
    for name in (".env", ".env.local"):
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def fetch_rows(base: str, headers: dict[str, str], since: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = requests.get(
            base.rstrip("/") + "/rest/v1/tdnet_events",
            headers=headers,
            params={
                "select": "id,ticker,company_name,event_type,event_subtype,headline,source_title,"
                          "source_url,pdf_url,disclosed_at,detected_at,status,archived_at,"
                          "dedupe_key,raw_payload",
                "event_type": "in.(" + ",".join(EVENT_TYPES) + ")",
                "disclosed_at": f"gte.{since}",
                "order": "disclosed_at.asc,id.asc",
                "limit": "1000",
                "offset": str(offset),
            },
            timeout=60,
        )
        response.raise_for_status()
        page = response.json()
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def validate_url(url: str) -> dict[str, Any]:
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"result": "invalid_format", "definitive": True}
    last: dict[str, Any] = {"result": "request_error", "definitive": False}
    user_agents = (
        "TDnetExcelInput/1.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    )
    for user_agent in user_agents:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                url,
                headers={"User-Agent": user_agent, "Range": "bytes=0-31"},
                timeout=(5, 25),
                stream=True,
                allow_redirects=True,
            )
            first = next(response.iter_content(32), b"")
            content_type = (response.headers.get("Content-Type") or "").lower()
            is_pdf = "pdf" in content_type or first.startswith(b"%PDF")
            if response.status_code in (200, 206) and is_pdf:
                return {"result": "valid_pdf", "definitive": False,
                        "status_code": response.status_code, "content_type": content_type,
                        "resolved_url": response.url}
            if response.status_code in DEFINITIVE_HTTP_FAILURES:
                return {"result": "invalid_http", "definitive": True,
                        "status_code": response.status_code, "content_type": content_type,
                        "resolved_url": response.url}
            if response.status_code in (200, 206):
                return {"result": "invalid_non_pdf", "definitive": True,
                        "status_code": response.status_code, "content_type": content_type,
                        "resolved_url": response.url}
            last = {"result": "request_error", "definitive": False,
                    "status_code": response.status_code, "content_type": content_type,
                    "resolved_url": response.url}
        except requests.RequestException as exc:
            last = {"result": "request_error", "definitive": False,
                    "error": type(exc).__name__}
        finally:
            session.close()
    return last


def patch_row(base: str, headers: dict[str, str], event_id: str, archived_at: str) -> None:
    response = requests.patch(
        base.rstrip("/") + "/rest/v1/tdnet_events",
        headers={**headers, "Content-Type": "application/json", "Prefer": "return=representation"},
        params={"id": f"eq.{event_id}", "status": "eq.active"},
        json={"status": "archived", "archived_at": archived_at},
        timeout=30,
    )
    response.raise_for_status()
    if len(response.json()) != 1:
        raise RuntimeError(f"expected one archived row for {event_id}")


def mark_local_invalid(root: Path, urls: set[str]) -> int:
    db_path = root / "decision_db.db"
    if not db_path.exists() or not urls:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in urls)
        cursor = conn.execute(
            f"UPDATE events SET status='invalid_url',updated_at=? "
            f"WHERE event_type='earnings_material' AND doc_url IN ({placeholders})",
            (datetime.now(timezone.utc).isoformat(), *sorted(urls)),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2025-08-26T00:00:00+09:00")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--report", default="artifacts/material_notification_cleanup_20260826.json")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_env(root)
    base = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        raise SystemExit("missing Supabase service configuration")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    rows = fetch_rows(base, headers, args.since)
    urls = sorted({str(row.get("pdf_url") or row.get("source_url") or "").strip() for row in rows})
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 16))) as pool:
        checks = dict(zip(urls, pool.map(validate_url, urls)))

    candidates = []
    unresolved = []
    for row in rows:
        url = str(row.get("pdf_url") or row.get("source_url") or "").strip()
        check = checks[url]
        if check["definitive"] and row.get("status") == "active":
            candidates.append({**row, "checked_url": url, "validation": check})
        elif check["result"] == "request_error" and row.get("status") == "active":
            unresolved.append({**row, "checked_url": url, "validation": check})

    archived_at = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "since": args.since,
        "scanned_rows": len(rows),
        "scanned_tickers": len({row.get("ticker") for row in rows}),
        "unique_urls": len(urls),
        "valid_rows": sum(checks[str(row.get("pdf_url") or row.get("source_url") or "").strip()]["result"] == "valid_pdf" for row in rows),
        "request_error_rows": sum(checks[str(row.get("pdf_url") or row.get("source_url") or "").strip()]["result"] == "request_error" for row in rows),
        "cleanup_candidates": len(candidates),
        "cleanup_tickers": len({row.get("ticker") for row in candidates}),
        "candidates": candidates,
        "unresolved": unresolved,
        "archived_ids": [],
        "local_rows_marked_invalid": 0,
    }
    report_path = root / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.apply:
        for row in candidates:
            patch_row(base, headers, row["id"], archived_at)
            report["archived_ids"].append(row["id"])
        report["local_rows_marked_invalid"] = mark_local_invalid(
            root, {row["checked_url"] for row in candidates},
        )
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({key: value for key, value in report.items() if key not in {"candidates", "unresolved"}}, ensure_ascii=False, indent=2))
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
