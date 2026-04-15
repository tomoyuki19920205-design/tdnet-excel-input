"""lib/backfill/retry.py — stage 別 retry + exponential backoff

retry は worker 内部で使われ、stage (download/xbrl/pdf) ごとに
回数・backoff を制御する。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

logger = logging.getLogger("backfill.retry")
T = TypeVar("T")


@dataclass
class RetryConfig:
    """stage 別 retry 設定。"""
    download: int = 3
    xbrl: int = 2
    pdf: int = 1
    base_delay: float = 0.5   # 初回 backoff (秒)
    max_delay: float = 5.0

    def max_attempts(self, stage: str) -> int:
        return getattr(self, stage, 1)


@dataclass
class TimeoutConfig:
    """stage 別 timeout 設定 (秒)。"""
    download: int = 30
    xbrl: int = 60
    pdf: int = 120

    def get(self, stage: str) -> int:
        return getattr(self, stage, 60)


@dataclass
class RetryResult:
    """retry_with_backoff の結果。"""
    success: bool
    value: T | None = None
    attempts: int = 0
    last_error: str | None = None
    timed_out: bool = False


def retry_with_backoff(
    fn: Callable,
    *,
    stage: str,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    timeout_sec: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RetryResult:
    """fn() を retry + exponential backoff で実行する。

    Args:
        fn: 引数なし callable。成功時に値を返す。失敗時は例外。
        stage: "download" / "xbrl" / "pdf"
        max_attempts: 最大試行回数
        base_delay: 初回 backoff 秒
        max_delay: 最大 backoff 秒
        timeout_sec: stage 全体の timeout 秒 (None=無制限)
        sleep_fn: テスト用 sleep mock

    Returns:
        RetryResult
    """
    t0 = time.monotonic()
    last_error = None

    for attempt in range(1, max_attempts + 1):
        # timeout チェック
        if timeout_sec and (time.monotonic() - t0) > timeout_sec:
            logger.warning(
                f"[retry] {stage} timeout after {attempt - 1} attempts "
                f"({timeout_sec}s)"
            )
            return RetryResult(
                success=False, attempts=attempt - 1,
                last_error=f"{stage}_timeout", timed_out=True,
            )

        try:
            value = fn()
            return RetryResult(success=True, value=value, attempts=attempt)
        except Exception as e:
            last_error = str(e)
            logger.info(
                f"[retry] {stage} attempt {attempt}/{max_attempts} "
                f"failed: {last_error[:100]}"
            )
            # HTTP 404 は確定失敗 — リトライしない
            if "xbrl_download_not_found" in last_error:
                logger.info(f"[retry] {stage} 404 not_found — skip retry")
                return RetryResult(
                    success=False, attempts=attempt,
                    last_error=last_error, timed_out=False,
                )
            if attempt < max_attempts:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                sleep_fn(delay)

    return RetryResult(
        success=False, attempts=max_attempts,
        last_error=last_error, timed_out=False,
    )


# V2 quarantine_reason → review_hint マッピング
_V2_REASON_MAP = {
    "no_segment_page_candidate": "pdf_no_segment_page_candidate",
    "no_segment_table_candidate": "pdf_no_segment_table_candidate",
    "segment_table_found_but_no_sales_profit_columns": "pdf_no_sales_profit_columns",
    "segment_table_found_but_no_rows_extracted": "pdf_no_rows_extracted",
    "pdf_text_cid_corrupted": "pdf_text_cid_corrupted",
    "pdf_partial_sales_only": "pdf_partial_sales_only",
    "non_segment_table_company_profile": "pdf_non_segment_table",
    "non_segment_table_correction_or_notice": "pdf_non_segment_table",
    "non_segment_table_narrative_text": "pdf_non_segment_table",
    "non_segment_table_explanation_slide": "pdf_non_segment_table",
}


def classify_review_hint(
    stage: str,
    error: str,
    timed_out: bool,
    *,
    v2_reason: str | None = None,
) -> str:
    """failure 内容から review_hint を生成する。

    優先順位:
    1. candidate_guard:xxx 形式 → row_classifier の一元マッピング
    2. V2 quarantine_reason → _V2_REASON_MAP
    3. picked_pl_table → pdf_pl_table_selected
    4. timeout → stage_timeout
    5. stage 別 fallback
    """
    error_str = error or ""

    # 1. candidate_guard reason (row_classifier の一元マッピングを使用)
    if "candidate_guard:" in error_str:
        try:
            from src.analysis.row_classifier import map_reject_reason_to_review_hint
            return map_reject_reason_to_review_hint(error_str)
        except ImportError:
            pass

    # picked_pl_table
    if "picked_pl_table" in error_str:
        return "pdf_pl_table_selected"

    # period_quarter_unresolved
    if "period_quarter_unresolved" in error_str:
        return "period_quarter_unresolved"

    # V2 quarantine_reason 優先
    if v2_reason:
        # candidate_guard reason が v2_reason に入っている場合
        if v2_reason.startswith("candidate_guard:"):
            try:
                from src.analysis.row_classifier import map_reject_reason_to_review_hint
                return map_reject_reason_to_review_hint(v2_reason)
            except ImportError:
                pass
        if v2_reason in _V2_REASON_MAP:
            return _V2_REASON_MAP[v2_reason]

    if timed_out:
        return f"{stage}_timeout"

    error_lower = (error or "").lower()

    if stage == "download":
        if any(kw in error_lower for kw in ["404", "not found"]):
            return "download_not_found"
        if any(kw in error_lower for kw in ["connect", "timeout", "network"]):
            return "download_network_error"
        return "download_failed"

    if stage == "xbrl":
        if "missing" in error_lower or "not found" in error_lower:
            return "xbrl_missing"
        if "parse" in error_lower or "xml" in error_lower:
            return "xbrl_parse_failed"
        return "xbrl_extraction_failed"

    if stage == "pdf":
        if "table" in error_lower:
            return "pdf_table_parse_failed"
        if "page" in error_lower:
            return "pdf_page_read_failed"
        return "pdf_extraction_failed"

    if "segment" in error_lower or "no_segment" in error_lower:
        return "no_segment_data"

    return f"{stage}_failed"


