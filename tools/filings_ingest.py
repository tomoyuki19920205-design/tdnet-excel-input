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

    result = run_ingest(
        config,
        company_code=company_code,
        dry_run=dry_run,
        db_path=db_path,
    )

    summary = result.get("summary", {})
    logger.info(
        f"[filings_ingest] done "
        f"total={result.get('total', 0)} "
        f"success={summary.get('succeeded', 0)} "
        f"errors={summary.get('errors', 0)}"
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
