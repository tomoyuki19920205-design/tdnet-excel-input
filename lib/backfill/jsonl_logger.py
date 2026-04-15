"""lib/backfill/jsonl_logger.py — JSONL 詳細ログ (run + filing)

2系統:
1. 全体 run ログ: logs/backfill_segments_tdnet_{timestamp}.jsonl
2. filing 単位: data/tdnet_cache/{filing_id}/logs.jsonl (cache.py 経由)
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("backfill.jsonl")
JST = timezone(timedelta(hours=9))


def generate_run_id() -> str:
    """run_id を生成する (短い UUID)。"""
    return uuid.uuid4().hex[:12]


class RunLogger:
    """全体 run の JSONL ログ。

    Usage::

        rl = RunLogger("logs/backfill_segments_tdnet_20260311.jsonl")
        rl.log_filing_result(result, filing)
        rl.log_summary(metrics)
        rl.close()
    """

    def __init__(self, path: str | None = None, run_id: str | None = None):
        self.run_id = run_id or generate_run_id()
        self._path = path
        self._file = None
        self._event_count = 0
        self._has_summary = False
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")
            self.log_run_start()

    def _write(self, event: dict) -> None:
        event["ts"] = datetime.now(JST).isoformat()
        event["run_id"] = self.run_id
        line = json.dumps(event, ensure_ascii=False, default=str)
        if self._file:
            self._file.write(line + "\n")
            self._file.flush()
        self._event_count += 1

    def log_run_start(self, **extra) -> None:
        """run 開始イベント。コンストラクタで自動呼出し。"""
        self._write({"event": "run_start", **extra})

    def log_fatal(self, error: str, **extra) -> None:
        """致命的エラーイベント。"""
        self._write({"event": "fatal", "error": error[:2000], **extra})

    def log_filing_result(self, result, filing=None) -> None:
        """filing 1件の結果をログ。"""
        m = result.metrics or {}
        seg_count = len(result.segment_records)
        # quality metrics
        sales_non_null = sum(1 for r in result.segment_records if r.get("segment_sales") is not None)
        profit_non_null = sum(1 for r in result.segment_records if r.get("segment_profit") is not None)
        complete = sum(1 for r in result.segment_records
                       if r.get("segment_sales") is not None and r.get("segment_profit") is not None)
        sales_only = sum(1 for r in result.segment_records
                         if r.get("segment_sales") is not None and r.get("segment_profit") is None)
        profit_only = sum(1 for r in result.segment_records
                          if r.get("segment_sales") is None and r.get("segment_profit") is not None)
        event = {
            "event": "filing_result",
            "filing_id": result.filing_id,
            "ticker": filing.ticker if filing else "",
            "status": result.status,
            "via": result.via,
            "rows": seg_count,
            "segment_count": seg_count,
            "sales_non_null_count": sales_non_null,
            "profit_non_null_count": profit_non_null,
            "complete_count": complete,
            "sales_only_count": sales_only,
            "profit_only_count": profit_only,
            "duration_ms": m.get("total_ms", 0),
            "cache_hit_pdf": m.get("pdf_cache_hit", False),
            "cache_hit_xbrl": m.get("xbrl_cache_hit", False),
            "listing_source": filing.listing_source if filing else "",
            "attempts": m.get("attempts", {}),
            "review_hint": (result.quarantine or {}).get("review_hint", None),
            "candidate_reject_reason": (result.quarantine or {}).get("candidate_reject_reason", None),
            "quarantine_reason": (result.quarantine or {}).get("error_message", None),
            "xbrl_fallback_attempted": m.get("xbrl_fallback_attempted", False),
            "xbrl_fallback_succeeded": m.get("xbrl_fallback_succeeded", False),
            "rescued_from_hint": m.get("rescued_from_hint", None),
            "xbrl_failure_reason": m.get("xbrl_failure_reason", None),
            "invalid_structure_rescue_attempted": m.get("invalid_structure_rescue_attempted", False),
            "invalid_structure_rescue_succeeded": m.get("invalid_structure_rescue_succeeded", False),
            # EDINET
            "edinet_api_key_present": m.get("edinet_api_key_present", False),
            "edinet_resolve_attempted": m.get("edinet_resolve_attempted", False),
            "edinet_resolve_succeeded": m.get("edinet_resolve_succeeded", False),
            "edinet_resolve_skipped_reason": m.get("edinet_resolve_skipped_reason", ""),
            "edinet_skipped_not_applicable": m.get("edinet_skipped_not_applicable", False),
            "edinet_skip_reason": m.get("edinet_skip_reason", ""),
            "edinet_doc_id": m.get("edinet_doc_id", ""),
            "edinet_match_score": m.get("edinet_match_score", 0.0),
            "edinet_match_basis": m.get("edinet_match_basis", ""),
            "edinet_candidate_count": m.get("edinet_candidate_count", 0),
            "edinet_top1_doc_id": m.get("edinet_top1_doc_id", ""),
            "edinet_top1_score": m.get("edinet_top1_score", 0.0),
            "edinet_top2_score": m.get("edinet_top2_score", 0.0),
            "edinet_selected_reason": m.get("edinet_selected_reason", ""),
            "edinet_fail_reason": m.get("edinet_fail_reason", ""),
            "edinet_ticker_match_count": m.get("edinet_ticker_match_count", 0),
            "edinet_window_stats": m.get("edinet_window_stats", {}),
            "edinet_cache_hit": m.get("edinet_cache_hit", False),
            "edinet_download_attempted": m.get("edinet_download_attempted", False),
            "edinet_download_succeeded": m.get("edinet_download_succeeded", False),
            "fallback_source": m.get("fallback_source", ""),
        }
        if result.result_fingerprint:
            event["fingerprint"] = result.result_fingerprint
        self._write(event)

    def log_filing_result_v2(self, result, filing=None) -> None:
        """V2 worker (FilingResultV2) の結果をログ。"""
        segment_records = getattr(result, "segment_records", None)
        segment_record_count = len(segment_records) if segment_records is not None else 0
        metrics = getattr(result, "metrics", None)
        duration_ms = metrics.get("total_ms", 0) if metrics is not None else 0
        account_like_ratio = getattr(result, "account_like_ratio", None)
        account_like_ratio_rounded = round(account_like_ratio, 3) if account_like_ratio is not None else 0.0

        event = {
            "event": "filing_result",
            "worker_version": "v2",
            "filing_id": getattr(result, "filing_id", None),
            "ticker": filing.ticker if filing else "",
            "status": getattr(result, "status", None),
            "worker_status": getattr(result, "status", None),
            "source": getattr(result, "source", None),
            "selected_path": getattr(result, "selected_path", None),
            "selected_source": getattr(result, "source", None),
            "selected_status": getattr(result, "status", None),
            "selected_confidence": getattr(result, "confidence", None),
            "confidence": getattr(result, "confidence", None),
            "via": getattr(result, "via", None),
            "fallback_used": getattr(result, "fallback_used", None),
            "fallback_reason": getattr(result, "fallback_reason", None),
            "hard_fail_reason": getattr(result, "hard_fail_reason", None),
            "quarantine_reason": getattr(result, "quarantine_reason", None),
            "raw_segment_count": getattr(result, "raw_segment_count", None),
            "valid_segment_count": getattr(result, "valid_segment_count", None),
            "invalid_segment_count": getattr(result, "invalid_segment_count", None),
            "sales_non_null_count": getattr(result, "sales_non_null_count", None),
            "profit_non_null_count": getattr(result, "profit_non_null_count", None),
            "account_like_ratio": account_like_ratio_rounded,
            "narrative_contamination": getattr(result, "narrative_contamination", None),
            "rows": segment_record_count,
            "segment_count": segment_record_count,
            "duration_ms": duration_ms,
            "candidate_summary": getattr(result, "candidate_summary", None),
            "listing_source": filing.listing_source if filing else "",
        }
        # candidate-level detail
        for c in getattr(result, "candidates", []):
            prefix = getattr(c, "source", "unknown")
            event[f"{prefix}_attempted"] = getattr(c, "attempted", None)
            event[f"{prefix}_available"] = getattr(c, "available", None)
            event[f"{prefix}_skip_reason"] = getattr(c, "skip_reason", None)
            validation = getattr(c, "validation", None)
            c_error = getattr(c, "error", None)
            if validation:
                event[f"{prefix}_validator_status"] = getattr(getattr(validation, "status", None), "value", None)
                event[f"{prefix}_validator_reason"] = getattr(getattr(validation, "hard_fail_reason", None), "value", None)
            elif c_error:
                event[f"{prefix}_validator_status"] = "error"
                event[f"{prefix}_validator_reason"] = c_error[:100]
            else:
                event[f"{prefix}_validator_status"] = "not_attempted"
                event[f"{prefix}_validator_reason"] = getattr(c, "skip_reason", None)

        result_fingerprint = getattr(result, "result_fingerprint", None)
        if result_fingerprint:
            event["fingerprint"] = result_fingerprint

        # Phase B-boost trace & scores
        if isinstance(result, dict):
            rule_trace = result.get("rule_trace")
            score_summary = result.get("score_summary")
        else:
            rule_trace = getattr(result, "rule_trace", None)
            score_summary = getattr(result, "score_summary", None)

        if rule_trace:
            event["rule_trace"] = rule_trace
        if score_summary:
            event["score_summary"] = score_summary

        self._write(event)

    def log_upsert(self, filing_id: str, batch_stats: dict) -> None:
        """upsert 結果をログ。"""
        self._write({
            "event": "upsert",
            "filing_id": filing_id,
            **batch_stats,
        })

    def log_summary(self, metrics_dict: dict) -> None:
        """run 終了サマリをログ。"""
        self._write({"event": "run_summary", **metrics_dict})
        self._has_summary = True

    def close(self) -> None:
        if self._file:
            if not self._has_summary:
                self._write({"event": "run_end_no_summary", "warning": "close() called without summary"})
            self._file.close()
            self._file = None


def make_filing_event(
    event: str,
    filing_id: str,
    *,
    stage: str = "",
    status: str = "",
    error: str = "",
    review_hint: str = "",
    via: str = "",
    segment_count: int = 0,
    attempt: int = 0,
    duration_ms: int = 0,
    **extra,
) -> dict:
    """filing cache/logs.jsonl 用イベント dict を組み立てる。"""
    d = {
        "event": event,
        "filing_id": filing_id,
        "ts": datetime.now(JST).isoformat(),
    }
    if stage:
        d["stage"] = stage
    if status:
        d["status"] = status
    if error:
        d["error"] = error[:500]
    if review_hint:
        d["review_hint"] = review_hint
    if via:
        d["via"] = via
    if segment_count:
        d["segment_count"] = segment_count
    if attempt:
        d["attempt"] = attempt
    if duration_ms:
        d["duration_ms"] = duration_ms
    d.update(extra)
    return d
