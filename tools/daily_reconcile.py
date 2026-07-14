#!/usr/bin/env python3
# ============================================================
# daily_reconcile.py — 深夜整合性チェック (Phase 1 最小実装)
# ============================================================
"""
軽量チェックを実行し、異常を data_quality_issues に記録。

Usage:
    python tools/daily_reconcile.py
    python tools/daily_reconcile.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env, get_supabase_config, supabase_select, supabase_upsert, supabase_update
from lib.pipeline.logging_utils import PipelineRun

logger = logging.getLogger("pipeline.reconcile")
JST = timezone(timedelta(hours=9))
_PAGE_SIZE = 1000


class ReconcileReadError(RuntimeError):
    """A required Supabase read could not be completed."""


class IssueWriteError(RuntimeError):
    """A data-quality issue could not be persisted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _required_select(table: str, *, params: dict, config: dict) -> list[dict]:
    """SELECT that distinguishes an empty result from an HTTP/read failure."""
    import requests

    try:
        response = requests.get(
            f"{config['rest_url']}/{table}",
            params=params,
            headers=config["headers"],
            timeout=30,
        )
    except Exception as exc:
        raise ReconcileReadError(f"SELECT {table} request failed") from exc
    if response.status_code != 200:
        raise ReconcileReadError(f"SELECT {table} failed: status={response.status_code}")
    try:
        rows = response.json()
    except Exception as exc:
        raise ReconcileReadError(f"SELECT {table} returned invalid JSON") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ReconcileReadError(f"SELECT {table} returned an invalid row set")
    return rows


def _get_previous_reconcile_cutoff(config: dict, current_cutoff: datetime) -> datetime | None:
    rows = _required_select(
        "pipeline_runs",
        params={
            "job_type": "eq.reconcile",
            "status": "eq.done",
            "finished_at": f"lt.{current_cutoff.isoformat()}",
            "select": "finished_at",
            "order": "finished_at.desc",
            "limit": "1",
        },
        config=config,
    )
    if not rows:
        return None
    return _parse_timestamp(rows[0].get("finished_at"))


def _fetch_all_failed_jobs(config: dict) -> list[dict]:
    rows: list[dict] = []
    seen_ids: set[object] = set()
    offset = 0
    while True:
        page = _required_select(
            "job_queue",
            params={
                "status": "eq.failed",
                "select": "id,job_type,target_id,error_message,attempts,finished_at",
                "order": "id.asc",
                "limit": str(_PAGE_SIZE),
                "offset": str(offset),
            },
            config=config,
        )
        for row in page:
            job_id = row.get("id")
            if job_id is None or job_id in seen_ids:
                raise ReconcileReadError("job_queue paging returned a missing or duplicate id")
            seen_ids.add(job_id)
            rows.append(row)
        if len(page) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def _fetch_open_issues(config: dict, check_name: str, detail: str) -> list[dict]:
    rows: list[dict] = []
    seen_ids: set[object] = set()
    offset = 0
    while True:
        page = _required_select(
            "data_quality_issues",
            params={
                "check_name": f"eq.{check_name}",
                "detail": f"eq.{detail[:2000]}",
                "status": "eq.open",
                "select": "id",
                "order": "id.asc",
                "limit": str(_PAGE_SIZE),
                "offset": str(offset),
            },
            config=config,
        )
        for row in page:
            issue_id = row.get("id")
            if issue_id is None or issue_id in seen_ids:
                raise ReconcileReadError(
                    "data_quality_issues paging returned a missing or duplicate id"
                )
            seen_ids.add(issue_id)
            rows.append(row)
        if len(page) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def _record_issue(
    check_name: str,
    detail: str,
    *,
    ticker: str | None = None,
    severity: str = "warn",
    config: dict,
    dry_run: bool = False,
    max_retries: int = 3,
) -> None:
    """data_quality_issues に記録。"""
    logger.warning(f"[reconcile] {check_name}: {detail}")
    if dry_run:
        return
    result = supabase_upsert(
        "data_quality_issues",
        {
            "check_name": check_name,
            "ticker": ticker,
            "detail": detail[:2000],
            "severity": severity,
            "status": "open",
        },
        config=config,
        max_retries=max_retries,
    )
    if not result.get("ok"):
        raise IssueWriteError(
            f"failed to save {check_name}: {result.get('error') or result.get('status')}"
        )


def _record_or_reuse_issue(
    check_name: str,
    detail: str,
    *,
    severity: str,
    config: dict,
    dry_run: bool,
) -> tuple[int, int, int]:
    if dry_run:
        logger.warning(f"[reconcile] {check_name}: {detail}")
        return 0, 0, 0
    existing = _fetch_open_issues(config, check_name, detail)
    if existing:
        return 0, 1, max(0, len(existing) - 1)
    _record_issue(
        check_name,
        detail,
        severity=severity,
        config=config,
        dry_run=False,
        max_retries=1,
    )
    return 1, 0, 0


def check_stuck_jobs(config: dict, dry_run: bool) -> int:
    """running が1時間以上続いている job_queue を検出し、pending に戻す。"""
    cutoff = (datetime.now(JST) - timedelta(hours=1)).isoformat()
    rows = supabase_select(
        "job_queue",
        params={
            "status": "eq.running",
            "started_at": f"lt.{cutoff}",
            "select": "id,job_type,target_id,started_at,attempts",
        },
        config=config,
    )
    requeued = 0
    for row in rows:
        attempts = row.get("attempts", 0)
        _record_issue(
            "stuck_job",
            f"job_queue id={row['id']} type={row.get('job_type')} "
            f"target={row.get('target_id')} started={row.get('started_at')}",
            severity="error",
            config=config,
            dry_run=dry_run,
        )
        # retry_count <= 3 なら pending に戻す
        if attempts <= 3 and not dry_run:
            try:
                supabase_update(
                    "job_queue",
                    {"status": "pending", "started_at": None, "finished_at": None},
                    params={"id": f"eq.{row['id']}"},
                    config=config,
                )
                requeued += 1
                logger.info(
                    f"[reconcile] stuck job requeued: id={row['id']} "
                    f"attempts={attempts}"
                )
            except Exception as e:
                logger.error(f"[reconcile] stuck job requeue failed: {e}")
        elif attempts > 3:
            logger.warning(
                f"[reconcile] stuck job id={row['id']} exceeded max retries "
                f"(attempts={attempts}), needs manual review"
            )
    if requeued:
        logger.info(f"[reconcile] requeued {requeued} stuck jobs")
    return len(rows)


def check_failed_jobs(
    config: dict,
    dry_run: bool,
    *,
    previous_cutoff: datetime,
    current_cutoff: datetime,
) -> dict:
    """Fetch all failed jobs and classify permanent failures by transition time."""
    result = {
        "status": "success",
        "failed_jobs_total": 0,
        "reconcile_scanned": 0,
        "existing_backlog": 0,
        "new_critical": 0,
        "unclassified_failed": 0,
        "issue_rows_created": 0,
        "issue_rows_reused": 0,
        "duplicate_open_issues": 0,
        "classification_status": "complete",
        "issue_write_status": "success",
        "has_more": False,
        "oldest_failed_at": None,
        "latest_failed_at": None,
        "reason_counts": {},
    }
    try:
        rows = _fetch_all_failed_jobs(config)
    except Exception as exc:
        logger.error(f"[reconcile] failed job paging failed: {exc}")
        result.update(status="failed", classification_status="unknown", has_more=True)
        return result

    result["failed_jobs_total"] = len(rows)
    result["reconcile_scanned"] = len(rows)
    requeued = 0
    permanent_failures = 0
    valid_finished: list[datetime] = []
    reasons: Counter[str] = Counter()
    new_rows: list[tuple[dict, str]] = []
    for row in rows:
        attempts = row.get("attempts", 0)
        if attempts <= 3:
            if not dry_run:
                try:
                    supabase_update(
                        "job_queue",
                        {
                            "status": "pending",
                            "started_at": None,
                            "finished_at": None,
                            "error_message": None,
                        },
                        params={"id": f"eq.{row['id']}"},
                        config=config,
                    )
                    requeued += 1
                    logger.info(
                        f"[reconcile] failed job requeued: id={row['id']} "
                        f"type={row.get('job_type')} target={row.get('target_id')} "
                        f"attempts={attempts}"
                    )
                except Exception as e:
                    logger.error(f"[reconcile] failed job requeue failed: {e}")
            else:
                requeued += 1  # dry-run でもカウント
        else:
            permanent_failures += 1
            reasons[str(row.get("error_message") or "(none)")] += 1
            try:
                finished_at = _parse_timestamp(row.get("finished_at"))
            except (TypeError, ValueError):
                result["unclassified_failed"] += 1
                continue
            valid_finished.append(finished_at)
            if finished_at > current_cutoff:
                result["unclassified_failed"] += 1
            elif finished_at <= previous_cutoff:
                result["existing_backlog"] += 1
            elif finished_at <= current_cutoff:
                result["new_critical"] += 1
                detail = (
                    f"job_queue id={row['id']} type={row.get('job_type')} "
                    f"target={row.get('target_id')} attempts={attempts} "
                    f"error={str(row.get('error_message') or '')[:200]}"
                )
                new_rows.append((row, detail))
            else:
                result["unclassified_failed"] += 1

    result["reason_counts"] = dict(sorted(reasons.items()))
    if valid_finished:
        result["oldest_failed_at"] = min(valid_finished).isoformat()
        result["latest_failed_at"] = max(valid_finished).isoformat()
    if result["unclassified_failed"]:
        result.update(status="failed", classification_status="unknown")
        return result

    for _row, detail in new_rows:
        try:
            created, reused, duplicates = _record_or_reuse_issue(
                "permanent_failure",
                detail,
                severity="error",
                config=config,
                dry_run=dry_run,
            )
        except Exception as exc:
            logger.error(f"[reconcile] permanent failure issue handling failed: {exc}")
            result.update(status="failed", issue_write_status="failed")
            return result
        result["issue_rows_created"] += created
        result["issue_rows_reused"] += reused
        result["duplicate_open_issues"] += duplicates
    if requeued:
        logger.info(
            f"[reconcile] requeued {requeued} failed jobs "
            f"(permanent_failures={permanent_failures})"
        )
    return result


def check_rebuild_backlog(config: dict, dry_run: bool) -> int:
    """6時間以上前の pending rebuild を検出。"""
    cutoff = (datetime.now(JST) - timedelta(hours=6)).isoformat()
    rows = supabase_select(
        "rebuild_queue",
        params={
            "status": "eq.pending",
            "created_at": f"lt.{cutoff}",
            "select": "id,ticker,created_at",
        },
        config=config,
    )
    if rows:
        tickers = [r.get("ticker", "?") for r in rows]
        _record_issue(
            "rebuild_backlog",
            f"{len(rows)} pending rebuilds older than 6h: {tickers[:10]}",
            severity="warn",
            config=config,
            dry_run=dry_run,
        )
    return len(rows)


def check_financials_duplicates(config: dict, dry_run: bool) -> int:
    """financials で (ticker, period, quarter) が重複する行を検出。"""
    # Supabase REST では GROUP BY が使えないため、
    # 最大 2000 行取得してアプリ側で checker
    rows = supabase_select(
        "financials",
        params={
            "select": "ticker,period,quarter",
            "limit": "2000",
        },
        config=config,
    )
    seen: dict[tuple, int] = {}
    for row in rows:
        key = (row.get("ticker"), row.get("period"), row.get("quarter"))
        seen[key] = seen.get(key, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    for key, count in list(dups.items())[:10]:
        _record_issue(
            "financials_duplicate",
            f"ticker={key[0]} period={key[1]} quarter={key[2]} count={count}",
            ticker=key[0],
            severity="error",
            config=config,
            dry_run=dry_run,
        )
    return len(dups)


def check_quarantine_spike(config: dict, dry_run: bool) -> int:
    """当日の quarantine 件数が異常に多くないかチェック。"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    rows = supabase_select(
        "quarantine_items",
        params={
            "created_at": f"gte.{today}T00:00:00",
            "select": "id",
            "limit": "200",
        },
        config=config,
    )
    count = len(rows)
    if count >= 20:
        _record_issue(
            "quarantine_spike",
            f"Today's quarantine count: {count} (threshold: 20)",
            severity="warn" if count < 50 else "error",
            config=config,
            dry_run=dry_run,
        )
    return count


def run(*, dry_run: bool = False) -> dict:
    """daily_reconcile メイン。"""
    current_cutoff = _utc_now()
    results = {}
    issues_total = 0
    non_failed_issues_total = 0
    summary = {
        "status": "failed",
        "issues_total": 0,
        "non_failed_issues_total": 0,
        "failed_issue_contribution": 0,
        "checks": results,
        "failed_jobs_total": None,
        "reconcile_scanned": None,
        "existing_backlog": None,
        "new_critical": None,
        "unclassified_failed": None,
        "issue_rows_created": 0,
        "issue_rows_reused": 0,
        "duplicate_open_issues": 0,
        "previous_reconcile_cutoff": None,
        "classification_cutoff": current_cutoff.isoformat(),
        "classification_status": "unknown",
        "issue_write_status": "not_started",
        "has_more": None,
        "oldest_failed_at": None,
        "latest_failed_at": None,
        "reason_counts": {},
    }

    logger.info("[reconcile] starting daily reconcile checks")
    try:
        load_env(_PROJECT_ROOT)
        config = get_supabase_config()
        previous_cutoff = _get_previous_reconcile_cutoff(config, current_cutoff)
        if previous_cutoff is None:
            logger.error("[reconcile] previous successful reconcile cutoff is unavailable")
            return summary
        summary["previous_reconcile_cutoff"] = previous_cutoff.isoformat()

        n = check_stuck_jobs(config, dry_run)
        results["stuck_jobs"] = n
        non_failed_issues_total += n
        summary["non_failed_issues_total"] = non_failed_issues_total

        failed = check_failed_jobs(
            config,
            dry_run,
            previous_cutoff=previous_cutoff,
            current_cutoff=current_cutoff,
        )
        results["failed_jobs"] = failed["failed_jobs_total"]
        for key in (
            "failed_jobs_total", "reconcile_scanned", "existing_backlog",
            "new_critical", "unclassified_failed", "issue_rows_created",
            "issue_rows_reused", "duplicate_open_issues", "classification_status",
            "issue_write_status", "has_more", "oldest_failed_at",
            "latest_failed_at", "reason_counts",
        ):
            summary[key] = failed[key]
        summary["failed_issue_contribution"] = failed["new_critical"]
        issues_total = non_failed_issues_total + failed["new_critical"]
        if failed["status"] == "failed":
            summary["issues_total"] = issues_total
            return summary

        n = check_rebuild_backlog(config, dry_run)
        results["rebuild_backlog"] = n
        non_failed_issues_total += n
        summary["non_failed_issues_total"] = non_failed_issues_total
        issues_total = non_failed_issues_total + summary["failed_issue_contribution"]

        n = check_financials_duplicates(config, dry_run)
        results["financials_duplicates"] = n
        non_failed_issues_total += n
        summary["non_failed_issues_total"] = non_failed_issues_total
        issues_total = non_failed_issues_total + summary["failed_issue_contribution"]

        n = check_quarantine_spike(config, dry_run)
        results["quarantine_today"] = n
    except IssueWriteError as exc:
        logger.error(f"[reconcile] issue write failed: {exc}")
        summary["issue_write_status"] = "failed"
        summary["issues_total"] = issues_total
        return summary
    except Exception as exc:
        logger.error(f"[reconcile] failed: {exc}")
        summary["issues_total"] = issues_total
        return summary

    summary.update(status="success", issues_total=issues_total)
    logger.info(f"[reconcile] done: issues_total={issues_total} checks={results}")
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Daily reconcile checks")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    print(f"\nReconcile: {result['status']} issues={result['issues_total']}")
