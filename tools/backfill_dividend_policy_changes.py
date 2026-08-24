#!/usr/bin/env python3
"""Dry-run/apply backfill for durable dividend policy changes.

Dry-run is the default.  Production writes require ``--apply``.  The tool
downloads each stored dividend PDF when available, so body-only policy changes
are not missed, and writes a reversible before/after manifest.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from typing import Any

import requests
from dotenv import load_dotenv
from pypdf import PdfReader
from supabase import create_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.events.dividend_policy import detect_dividend_policy_change


def _payload(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _fetch_pdf_text(row: dict) -> tuple[str, str | None]:
    url = row.get("pdf_url") or row.get("source_url")
    if not url:
        return "", "missing_pdf_url"
    try:
        response = requests.get(
            url,
            timeout=(10, 45),
            headers={"User-Agent": "tdnet-dividend-policy-backfill/1.0"},
        )
        response.raise_for_status()
        reader = PdfReader(BytesIO(response.content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:12])
        return text, None if text.strip() else "empty_pdf_text"
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"


def _new_summary(extracted: dict) -> str:
    label = extracted.get("policy_change_label") or "配当方針変更"
    detail = extracted.get("policy_change_summary") or ""
    summary = f"配当修正／{label}"
    return f"{summary}\n{detail}" if detail and detail != label else summary


def _analyze(row: dict, text: str, fetch_error: str | None) -> dict:
    payload = _payload(row.get("raw_payload"))
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    policy = detect_dividend_policy_change(row.get("headline") or "", text)
    before_detected = extracted.get("policy_change_detected") is True
    proposed_extracted = dict(extracted)
    proposed_extracted.update({
        "policy_change_detected": policy.detected,
        "policy_change_scope": policy.scope,
        "policy_change_label": policy.label,
        "policy_change_action": policy.action,
        "policy_change_summary": policy.summary,
        "policy_change_before": policy.before,
        "policy_change_after": policy.after,
        "policy_change_metrics": policy.metrics,
        "policy_change_evidence": policy.evidence,
    })
    proposed_payload = dict(payload)
    proposed_payload["extracted"] = proposed_extracted
    if policy.detected:
        proposed_payload["text_extract_status"] = "ok"
        proposed_payload["text_empty"] = False

    proposed_summary = _new_summary(proposed_extracted) if policy.detected else row.get("summary")
    card_changed = bool(policy.detected and (
        not before_detected
        or policy.label not in str(row.get("summary") or "")
        or policy.label not in str(row.get("display_summary") or "")
    ))
    title_explicit = any(token in str(row.get("headline") or "") for token in (
        "配当方針の変更", "配当方針変更", "株主還元方針の変更", "利益還元方針の変更",
    ))
    suspected_false_positive = bool(
        policy.detected
        and not title_explicit
        and (not policy.evidence or policy.summary == policy.label)
    )
    return {
        "id": row.get("id"),
        "ticker": row.get("ticker"),
        "disclosed_at": row.get("disclosed_at"),
        "headline": row.get("headline"),
        "fetch_error": fetch_error,
        "before_detected": before_detected,
        "detected": policy.detected,
        "scope": policy.scope,
        "label": policy.label,
        "summary": policy.summary,
        "before": policy.before,
        "after": policy.after,
        "metrics": policy.metrics,
        "evidence": policy.evidence,
        "suspected_false_positive": suspected_false_positive,
        "card_changed": card_changed,
        "before_row": {
            "summary": row.get("summary"),
            "display_summary": row.get("display_summary"),
            "formatted_message": row.get("formatted_message"),
            "raw_payload": payload,
        },
        "update": {
            "summary": proposed_summary,
            "display_summary": proposed_summary,
            "formatted_message": proposed_summary,
            "raw_payload": proposed_payload,
        } if policy.detected else None,
    }


def _fetch_rows(client, since: str | None, limit: int | None) -> list[dict]:
    rows: list[dict] = []
    page_size = 500
    offset = 0
    while True:
        query = (
            client.table("tdnet_events")
            .select("id,ticker,disclosed_at,event_type,event_subtype,headline,summary,display_summary,formatted_message,raw_payload,pdf_url,source_url,status")
            .eq("event_type", "dividend")
            .order("disclosed_at", desc=True)
            .range(offset, offset + page_size - 1)
        )
        if since:
            query = query.gte("disclosed_at", since)
        batch = query.execute().data or []
        rows.extend(batch)
        if len(batch) < page_size or (limit and len(rows) >= limit):
            break
        offset += page_size
    return rows[:limit] if limit else rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply proposed updates (default is dry-run)")
    parser.add_argument("--since", help="Only disclosures at/after this ISO timestamp")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is required")
    client = create_client(url, key)
    rows = _fetch_rows(client, args.since, args.limit)

    analyses: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as pool:
        futures = {pool.submit(_fetch_pdf_text, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            text, error = future.result()
            analyses.append(_analyze(row, text, error))
    analyses.sort(key=lambda item: str(item.get("disclosed_at") or ""), reverse=True)

    detected = [item for item in analyses if item["detected"]]
    false_positive = [item for item in detected if item["suspected_false_positive"]]
    changed = [item for item in detected if item["card_changed"]]
    failed = [item for item in analyses if item["fetch_error"]]
    applied = 0
    apply_errors: list[dict] = []
    apply_candidates = [item for item in changed if not item["suspected_false_positive"]]
    if args.apply:
        for item in apply_candidates:
            try:
                client.table("tdnet_events").update(item["update"]).eq("id", item["id"]).execute()
                applied += 1
            except Exception as exc:
                apply_errors.append({"id": item["id"], "error": f"{type(exc).__name__}: {exc}"})

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or ROOT / "out" / f"dividend_policy_backfill_{'apply' if args.apply else 'dryrun'}_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "generated_at": datetime.now().astimezone().isoformat(),
        "filters": {"since": args.since, "limit": args.limit},
        "target_count": len(rows),
        "policy_change_detected_count": len(detected),
        "suspected_false_positive_count": len(false_positive),
        "existing_card_changed_count": len(changed),
        "pdf_fetch_or_extract_failure_count": len(failed),
        "applied_count": applied,
        "apply_candidate_count": len(apply_candidates),
        "apply_errors": apply_errors,
        "representative_examples": detected[:20],
        "suspected_false_positive_examples": false_positive[:20],
        "failures": failed[:50],
        "changes": changed,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        key: report[key] for key in (
            "mode", "target_count", "policy_change_detected_count",
            "suspected_false_positive_count", "existing_card_changed_count",
            "pdf_fetch_or_extract_failure_count", "applied_count", "apply_errors",
        )
    } | {"output": str(output)}, ensure_ascii=False, indent=2))
    return 1 if apply_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
