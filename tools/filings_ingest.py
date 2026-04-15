#!/usr/bin/env python3
# ============================================================
# filings_ingest.py — 決算短信 ingest 薄ラッパー
# ============================================================
"""
tdnet_ingest.run_ingest() を呼ぶだけの薄ラッパー。
CLI ロジックとは分離し pipeline_run.py から import して使う。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ── path setup ──
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.config import Config
from tools.tdnet_ingest import run_ingest

logger = logging.getLogger("pipeline.ingest")


def run(
    *,
    dry_run: bool = False,
    company_code: str | None = None,
    db_path: str | None = None,
) -> dict:
    """
    filings ingest を実行し結果 dict を返す。

    Returns:
        {"total": int, "results": [...], "summary": {...}}
    """
    config = Config()
    logger.info(
        f"[filings_ingest] start dry_run={dry_run} "
        f"company_code={company_code or 'ALL'}"
    )

    # ── phase marker: FETCH_START ──
    logger.info("===FETCH_START===")

    result = run_ingest(
        config,
        company_code=company_code,
        dry_run=dry_run,
        db_path=db_path,
    )

    summary = result.get("summary", {})
    total_fetched = summary.get("total_fetched", 0)

    # ── phase marker: FETCH_END ──
    logger.info(
        f"===FETCH_END=== fetched={total_fetched} "
        f"target_statements={summary.get('target_statements', 0)}"
    )

    # ── phase marker: SQLITE_SAVE_END ──
    logger.info(
        f"===SQLITE_SAVE_END=== "
        f"total={result.get('total', 0)} "
        f"success={summary.get('succeeded', summary.get('success', 0))} "
        f"errors={summary.get('errors', 0)} "
        f"skipped={summary.get('skipped', 0)}"
    )

    logger.info(
        f"[filings_ingest] done "
        f"total={result.get('total', 0)} "
        f"success={summary.get('succeeded', 0)} "
        f"errors={summary.get('errors', 0)}"
    )

    # ── Step: 成功した disclosure_id を job_queue に enqueue ──
    if not dry_run:
        _enqueue_disclosure_ids(result)

    return result


def _enqueue_disclosure_ids(ingest_result: dict) -> None:
    """ingest 結果から成功した disclosure_id を job_queue に enqueue する。

    _process_single() の返り値に含まれる disclosure_id を直接使い、
    job_type=tdnet_realtime_process として queue に登録する。
    inserted / updated の結果のみ対象。
    """
    results = ingest_result.get("results", [])
    if not results:
        return

    # ingest 結果から disclosure_id を直接収集（dedupe 付き）
    seen: set[str] = set()
    disclosure_ids: list[str] = []
    for r in results:
        if r.get("status") not in ("inserted", "updated"):
            continue
        did = r.get("disclosure_id", "")
        if did and did not in seen:
            seen.add(did)
            disclosure_ids.append(did)

    if not disclosure_ids:
        logger.info("[filings_ingest] no new disclosure_ids to enqueue")
        return

    # job_queue に enqueue
    from lib.pipeline.queue import enqueue_job
    from lib.pipeline.db import load_env
    load_env(_PROJECT_ROOT)

    enqueued = 0
    for did in disclosure_ids:
        try:
            enqueue_job(
                job_type="tdnet_realtime_process",
                target_type="disclosure",
                target_id=did,
                priority=3,
            )
            enqueued += 1
        except Exception as e:
            logger.warning(f"[filings_ingest] enqueue failed for {did}: {e}")

    logger.info(
        f"[filings_ingest] enqueued {enqueued}/{len(disclosure_ids)} "
        f"disclosure_ids to job_queue"
    )



if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
