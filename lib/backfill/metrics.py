"""lib/backfill/metrics.py — バックフィル集計 (filing ベース + stage 分離)"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("backfill.metrics")


@dataclass
class BackfillMetrics:
    """バックフィル全体の集計。

    filing ベース指標: filing_id 単位でカウント。同一 filing で
    Stage B (XBRL) と Stage C (PDF) を通過しても 1 件。

    stage イベント指標: stage 処理の呼び出し回数をカウント。
    """
    total_filings: int = 0

    # ── filing ベース指標 ──
    # 各 filing の最終状態を追跡
    _filing_final_status: dict = field(default_factory=dict, repr=False)

    # ── stage イベント指標 ──
    xbrl_stage_events: int = 0
    pdf_stage_events: int = 0
    xbrl_stage_ok: int = 0
    xbrl_stage_needs_pdf: int = 0
    xbrl_stage_failed: int = 0
    xbrl_stage_quarantined: int = 0
    pdf_stage_ok: int = 0
    pdf_stage_failed: int = 0
    pdf_stage_quarantined: int = 0

    # ── 集計 ──
    total_segment_rows: int = 0
    upserted_count: int = 0
    cache_hit_pdf_count: int = 0
    cache_hit_xbrl_count: int = 0
    retried_count: int = 0
    timeout_count: int = 0
    elapsed_seconds: float = 0.0
    upsert_inserted: int = 0
    upsert_updated: int = 0
    upsert_failed_batches: int = 0
    batch_count: int = 0
    xbrl_stage_elapsed: float = 0.0
    pdf_stage_elapsed: float = 0.0

    # Step 5: per-filing duration tracking
    filing_durations_ms: list[int] = field(default_factory=list, repr=False)
    xbrl_durations_ms: list[int] = field(default_factory=list, repr=False)
    pdf_durations_ms: list[int] = field(default_factory=list, repr=False)

    _t0: float = field(default_factory=time.monotonic, repr=False)

    def record_xbrl_result(self, result) -> None:
        """Stage B (XBRL-first) の結果を集計。filing ベース + stage イベント。"""
        self.xbrl_stage_events += 1
        fid = result.filing_id

        if result.status == "ok":
            self.xbrl_stage_ok += 1
            self.total_segment_rows += len(result.segment_records)
            self._filing_final_status[fid] = {
                "status": "ok", "via": result.via or "xbrl",
            }
        elif result.status == "needs_pdf":
            self.xbrl_stage_needs_pdf += 1
            # needs_pdf は中間状態 → filing 最終状態は未定
            self._filing_final_status[fid] = {
                "status": "needs_pdf", "via": None,
            }
        elif result.status == "quarantined":
            self.xbrl_stage_quarantined += 1
            self._filing_final_status[fid] = {
                "status": "quarantined", "via": None,
            }
        elif result.status == "failed":
            self.xbrl_stage_failed += 1
            self._filing_final_status[fid] = {
                "status": "failed", "via": None,
            }

        self._record_common(result, "xbrl")

    def record_pdf_result(self, result) -> None:
        """Stage C (PDF-only) の結果を集計。filing ベース + stage イベント。"""
        self.pdf_stage_events += 1
        fid = result.filing_id

        if result.status == "ok":
            self.pdf_stage_ok += 1
            self.total_segment_rows += len(result.segment_records)
            self._filing_final_status[fid] = {
                "status": "ok", "via": result.via or "pdf",
            }
        elif result.status == "quarantined":
            self.pdf_stage_quarantined += 1
            self._filing_final_status[fid] = {
                "status": "quarantined", "via": None,
            }
        elif result.status == "failed":
            self.pdf_stage_failed += 1
            self._filing_final_status[fid] = {
                "status": "failed", "via": None,
            }

        self._record_common(result, "pdf")

    def record_result(self, result, stage: str = "unknown") -> None:
        """後方互換: record_xbrl_result / record_pdf_result を使うこと推奨。"""
        if stage == "xbrl":
            self.record_xbrl_result(result)
        elif stage == "pdf":
            self.record_pdf_result(result)
        else:
            # fallback: legacy call
            self.record_xbrl_result(result)

    def _record_common(self, result, stage: str) -> None:
        """共通メトリクス (cache/retry/timeout/duration)。"""
        m = result.metrics or {}
        if m.get("pdf_cache_hit"):
            self.cache_hit_pdf_count += 1
        if m.get("xbrl_cache_hit"):
            self.cache_hit_xbrl_count += 1

        attempts = m.get("attempts", {})
        if any(v > 1 for v in attempts.values() if isinstance(v, int)):
            self.retried_count += 1

        q = result.quarantine or {}
        if q.get("review_hint", "").endswith("_timeout"):
            self.timeout_count += 1

        # per-filing duration
        total_ms = m.get("total_ms", 0)
        if total_ms > 0:
            self.filing_durations_ms.append(total_ms)
        xbrl_ms = m.get("extract_ms", 0) or m.get("xbrl_segment_ms", 0)
        if xbrl_ms > 0 and stage == "xbrl":
            self.xbrl_durations_ms.append(xbrl_ms)
        pdf_ms = m.get("segment_ms", 0)
        if pdf_ms > 0 and stage == "pdf":
            self.pdf_durations_ms.append(pdf_ms)

    def record_upsert(self, stats) -> None:
        """BatchUpsertStats を反映。"""
        self.upsert_inserted += stats.inserted
        self.upsert_updated += stats.updated
        self.upsert_failed_batches += stats.failed_batches
        self.batch_count += stats.total_batches

    def finalize(self) -> None:
        self.elapsed_seconds = time.monotonic() - self._t0

    # ================================================================
    # filing ベース集計プロパティ
    # ================================================================

    @property
    def completed_filings(self) -> int:
        """最終状態に到達した filing 数 (needs_pdf は未完)。"""
        return sum(
            1 for s in self._filing_final_status.values()
            if s["status"] != "needs_pdf"
        )

    @property
    def ok_count(self) -> int:
        return sum(1 for s in self._filing_final_status.values() if s["status"] == "ok")

    @property
    def ok_xbrl_count(self) -> int:
        return sum(
            1 for s in self._filing_final_status.values()
            if s["status"] == "ok" and s.get("via") == "xbrl"
        )

    @property
    def ok_pdf_count(self) -> int:
        return sum(
            1 for s in self._filing_final_status.values()
            if s["status"] == "ok" and s.get("via") == "pdf"
        )

    @property
    def quarantined_count(self) -> int:
        return sum(1 for s in self._filing_final_status.values() if s["status"] == "quarantined")

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self._filing_final_status.values() if s["status"] == "failed")

    @property
    def needs_pdf_count(self) -> int:
        """XBRL で取れず PDF に回った件数 (最終的に ok/quarantined になるものを含む)。"""
        return self.xbrl_stage_needs_pdf

    @property
    def via_xbrl_count(self) -> int:
        return self.ok_xbrl_count

    @property
    def via_pdf_count(self) -> int:
        return self.ok_pdf_count

    @property
    def avg_seconds_per_filing(self) -> float:
        c = self.completed_filings
        if c == 0:
            return 0.0
        return self.elapsed_seconds / c

    @property
    def avg_segment_rows_per_filing(self) -> float:
        if self.ok_count == 0:
            return 0.0
        return self.total_segment_rows / self.ok_count

    @property
    def xbrl_success_rate(self) -> float:
        """XBRL で segment まで取れた filing の割合。"""
        if self.total_filings == 0:
            return 0.0
        return self.ok_xbrl_count / self.total_filings

    @property
    def pdf_fallback_rate(self) -> float:
        """PDF fallback が必要だった filing の割合。"""
        if self.total_filings == 0:
            return 0.0
        return self.needs_pdf_count / self.total_filings

    @property
    def quarantine_rate(self) -> float:
        if self.total_filings == 0:
            return 0.0
        return self.quarantined_count / self.total_filings

    @property
    def failed_rate(self) -> float:
        if self.total_filings == 0:
            return 0.0
        return self.failed_count / self.total_filings

    @property
    def avg_xbrl_sec(self) -> float:
        if not self.xbrl_durations_ms:
            if self.xbrl_stage_elapsed > 0 and self.ok_xbrl_count > 0:
                return self.xbrl_stage_elapsed / self.ok_xbrl_count
            return 0.0
        return sum(self.xbrl_durations_ms) / len(self.xbrl_durations_ms) / 1000

    @property
    def avg_pdf_sec(self) -> float:
        if not self.pdf_durations_ms:
            if self.pdf_stage_elapsed > 0 and self.ok_pdf_count > 0:
                return self.pdf_stage_elapsed / self.ok_pdf_count
            return 0.0
        return sum(self.pdf_durations_ms) / len(self.pdf_durations_ms) / 1000

    @property
    def avg_batch_size(self) -> float:
        if self.batch_count == 0:
            return 0.0
        return (self.upsert_inserted + self.upsert_updated) / self.batch_count

    def summary_dict(self) -> dict:
        """サマリ dict を返す。filing ベースと stage ベースを分離。"""
        self.finalize()
        return {
            # ── filing ベース ──
            "total_filings": self.total_filings,
            "filing_completed": self.completed_filings,
            "filing_ok": self.ok_count,
            "filing_ok_xbrl": self.ok_xbrl_count,
            "filing_ok_pdf": self.ok_pdf_count,
            "filing_quarantined": self.quarantined_count,
            "filing_failed": self.failed_count,
            "filing_needs_pdf": self.needs_pdf_count,
            "xbrl_success_rate": f"{self.xbrl_success_rate:.1%}",
            "pdf_fallback_rate": f"{self.pdf_fallback_rate:.1%}",
            "quarantine_rate": f"{self.quarantine_rate:.1%}",
            "failed_rate": f"{self.failed_rate:.1%}",
            "via_xbrl": self.via_xbrl_count,
            "via_pdf": self.via_pdf_count,
            # ── stage イベント ──
            "xbrl_stage_events": self.xbrl_stage_events,
            "xbrl_stage_ok": self.xbrl_stage_ok,
            "xbrl_stage_needs_pdf": self.xbrl_stage_needs_pdf,
            "xbrl_stage_failed": self.xbrl_stage_failed,
            "xbrl_stage_quarantined": self.xbrl_stage_quarantined,
            "pdf_stage_events": self.pdf_stage_events,
            "pdf_stage_ok": self.pdf_stage_ok,
            "pdf_stage_failed": self.pdf_stage_failed,
            "pdf_stage_quarantined": self.pdf_stage_quarantined,
            # ── shared ──
            "upserted": self.upserted_count,
            "retried": self.retried_count,
            "timeouts": self.timeout_count,
            "cache_hit_pdf": self.cache_hit_pdf_count,
            "cache_hit_xbrl": self.cache_hit_xbrl_count,
            "total_segment_rows": self.total_segment_rows,
            "avg_segments_per_filing": round(self.avg_segment_rows_per_filing, 1),
            "elapsed_sec": round(self.elapsed_seconds, 1),
            "avg_sec_per_filing": round(self.avg_seconds_per_filing, 2),
            "avg_xbrl_sec": round(self.avg_xbrl_sec, 3),
            "avg_pdf_sec": round(self.avg_pdf_sec, 3),
            "xbrl_stage_sec": round(self.xbrl_stage_elapsed, 1),
            "pdf_stage_sec": round(self.pdf_stage_elapsed, 1),
            "avg_batch_size": round(self.avg_batch_size, 1),
            "upsert_inserted": self.upsert_inserted,
            "upsert_updated": self.upsert_updated,
            "upsert_failed_batches": self.upsert_failed_batches,
            "batch_count": self.batch_count,
            # extraction mode indicator
            "current_extraction_mode": (
                "pdf_only_effective" if self.pdf_fallback_rate >= 0.99
                else "xbrl_primary" if self.xbrl_success_rate >= 0.5
                else "mixed"
            ),
        }

    def print_summary(self) -> None:
        """サマリを print する。"""
        self.finalize()
        d = self.summary_dict()
        print("\n" + "=" * 60)
        print("  Backfill Summary (filing-based)")
        print("=" * 60)

        # filing ベース
        filing_keys = [
            "total_filings", "filing_completed", "filing_ok",
            "filing_ok_xbrl", "filing_ok_pdf", "filing_quarantined",
            "filing_failed", "filing_needs_pdf",
            "xbrl_success_rate", "pdf_fallback_rate",
            "quarantine_rate", "failed_rate", "via_xbrl", "via_pdf",
        ]
        print("  ── filing metrics ──")
        for k in filing_keys:
            print(f"  {k:30s} {d[k]}")

        # stage ベース
        stage_keys = [
            "xbrl_stage_events", "xbrl_stage_ok", "xbrl_stage_needs_pdf",
            "xbrl_stage_failed", "xbrl_stage_quarantined",
            "pdf_stage_events", "pdf_stage_ok",
            "pdf_stage_failed", "pdf_stage_quarantined",
        ]
        print("  ── stage events ──")
        for k in stage_keys:
            print(f"  {k:30s} {d[k]}")

        # shared
        print("  ── shared ──")
        for k, v in d.items():
            if k not in filing_keys and k not in stage_keys:
                print(f"  {k:30s} {v}")
        print("=" * 60)


# ================================================================
# V2 Metrics
# ================================================================

def _safe_median(vals: list) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) / 2
    return s[n // 2]


@dataclass
class BackfillMetricsV2:
    """V2 worker 用メトリクス。process_one_filing_v2 の FilingResultV2 を集計。"""
    total_filings: int = 0
    _filing_results: list = field(default_factory=list, repr=False)
    elapsed_seconds: float = 0.0
    _t0: float = field(default_factory=time.monotonic, repr=False)
    total_segment_rows: int = 0
    upserted_count: int = 0
    upsert_inserted: int = 0
    upsert_updated: int = 0
    upsert_failed_batches: int = 0
    batch_count: int = 0

    def record_v2_result(self, result) -> None:
        """FilingResultV2 を1件集計。"""
        segment_records = getattr(result, "segment_records", None)
        segment_record_count = len(segment_records) if segment_records is not None else 0
        metrics = getattr(result, "metrics", None)
        duration_ms = metrics.get("total_ms", 0) if metrics is not None else 0

        self._filing_results.append({
            "filing_id": getattr(result, "filing_id", None),
            "status": getattr(result, "status", None),
            "source": getattr(result, "source", None),
            "selected_path": getattr(result, "selected_path", None),
            "confidence": getattr(result, "confidence", None),
            "reason": getattr(result, "reason", None),
            "fallback_used": getattr(result, "fallback_used", None),
            "fallback_reason": getattr(result, "fallback_reason", None),
            "hard_fail_reason": getattr(result, "hard_fail_reason", None),
            "quarantine_reason": getattr(result, "quarantine_reason", None),
            "raw_segment_count": getattr(result, "raw_segment_count", None),
            "valid_segment_count": getattr(result, "valid_segment_count", None),
            "invalid_segment_count": getattr(result, "invalid_segment_count", None),
            "sales_non_null_count": getattr(result, "sales_non_null_count", None),
            "profit_non_null_count": getattr(result, "profit_non_null_count", None),
            "account_like_ratio": getattr(result, "account_like_ratio", None),
            "narrative_contamination": getattr(result, "narrative_contamination", None),
            "segment_record_count": segment_record_count,
            "duration_ms": duration_ms,
            "metrics": metrics or {},
        })
        if getattr(result, "status", None) in ("ok", "partial"):
            self.total_segment_rows += segment_record_count

    def record_upsert(self, stats) -> None:
        self.upsert_inserted += stats.inserted
        self.upsert_updated += stats.updated
        self.upsert_failed_batches += stats.failed_batches
        self.batch_count += stats.total_batches

    def finalize(self) -> None:
        self.elapsed_seconds = time.monotonic() - self._t0

    def _count_by(self, key: str) -> dict:
        counts: dict[str, int] = {}
        for r in self._filing_results:
            val = r.get(key, "") or ""
            counts[val] = counts.get(val, 0) + 1
        return dict(sorted(counts.items()))

    def _avg_of(self, key: str) -> float:
        vals = [r[key] for r in self._filing_results if isinstance(r.get(key), (int, float))]
        return sum(vals) / len(vals) if vals else 0.0

    def _median_of(self, key: str) -> float:
        vals = [r[key] for r in self._filing_results if isinstance(r.get(key), (int, float))]
        return _safe_median(vals)

    def summary_dict(self) -> dict:
        self.finalize()
        n = len(self._filing_results)
        status_bd = self._count_by("status")
        path_bd = self._count_by("selected_path")
        fallback_count = sum(1 for r in self._filing_results if r["fallback_used"])
        fallback_reason_bd = self._count_by("fallback_reason")
        fallback_reason_bd.pop("", None)
        quarantine_reason_bd: dict[str, int] = {}
        hard_fail_reason_bd: dict[str, int] = {}
        skip_reason_bd: dict[str, int] = {}
        for r in self._filing_results:
            if r["status"] == "quarantined":
                qr = r["quarantine_reason"] or "unknown"
                quarantine_reason_bd[qr] = quarantine_reason_bd.get(qr, 0) + 1
                hfr = r["hard_fail_reason"] or "unknown"
                hard_fail_reason_bd[hfr] = hard_fail_reason_bd.get(hfr, 0) + 1
            elif r["status"] == "skipped_normal":
                # reason (etf_like / reit_like 等) を優先、なければ quarantine_reason / hard_fail_reason
                sr = r.get("reason") or r.get("quarantine_reason") or r.get("hard_fail_reason") or "unknown"
                skip_reason_bd[sr] = skip_reason_bd.get(sr, 0) + 1
        narrative_count = sum(1 for r in self._filing_results if r["narrative_contamination"])

        # AI フォールバック内訳（metrics フィールドから集計）
        ai_used_count = 0
        ai_success_count = 0
        ai_no_segments_count = 0
        ai_parse_error_count = 0
        ai_api_error_count = 0
        # AI period/quarter 補完メトリクス
        ai_period_type_resolved_count = 0
        ai_quarter_resolved_count = 0
        ai_period_unresolved_count = 0
        ai_period_reason_breakdown: dict[str, int] = {}
        for r in self._filing_results:
            _m = r.get("metrics", {}) or {}
            if _m.get("ai_used"):
                ai_used_count += 1
                _reason = _m.get("ai_reason", "")
                if _reason == "ai_ok":
                    ai_success_count += 1
                elif _reason == "ai_no_segments":
                    ai_no_segments_count += 1
                elif _reason == "ai_parse_error":
                    ai_parse_error_count += 1
                elif _reason == "ai_api_error":
                    ai_api_error_count += 1
                # period/quarter 補完の集計（ai_ok 時のみ意味があるが全 ai_used で記録）
                if _m.get("ai_period_type_resolved"):
                    ai_period_type_resolved_count += 1
                if _m.get("ai_quarter_resolved"):
                    ai_quarter_resolved_count += 1
                if _reason == "ai_ok" and not _m.get("ai_period_type_resolved") and not _m.get("ai_quarter_resolved"):
                    ai_period_unresolved_count += 1
                _p_reason = _m.get("ai_period_reason", "") or ""
                if _p_reason:
                    ai_period_reason_breakdown[_p_reason] = (
                        ai_period_reason_breakdown.get(_p_reason, 0) + 1
                    )

        return {
            "worker_version": "v4",
            "total_filings": n,
            "filing_ok": status_bd.get("ok", 0),
            "filing_partial": status_bd.get("partial", 0),
            "filing_skipped_normal": status_bd.get("skipped_normal", 0),
            "filing_quarantined": status_bd.get("quarantined", 0),
            "filing_failed": status_bd.get("failed", 0),
            "selected_path_xbrl": path_bd.get("xbrl", 0),
            "selected_path_html": path_bd.get("html", 0),
            "selected_path_pdf": path_bd.get("pdf", 0),
            "selected_path_ai": path_bd.get("ai", 0),
            "selected_path_none": path_bd.get("none", 0),
            "fallback_used_count": fallback_count,
            "fallback_reason_breakdown": fallback_reason_bd,
            "quarantine_reason_breakdown": dict(sorted(quarantine_reason_bd.items())),
            "hard_fail_reason_breakdown": dict(sorted(hard_fail_reason_bd.items())),
            "skip_reason_breakdown": dict(sorted(skip_reason_bd.items())),
            # AI フォールバック内訳
            "ai_used_count": ai_used_count,
            "ai_success_count": ai_success_count,
            "ai_no_segments_count": ai_no_segments_count,
            "ai_parse_error_count": ai_parse_error_count,
            "ai_api_error_count": ai_api_error_count,
            "ai_reason_breakdown": {
                "ai_ok": ai_success_count,
                "ai_no_segments": ai_no_segments_count,
                "ai_parse_error": ai_parse_error_count,
                "ai_api_error": ai_api_error_count,
            },
            # AI period/quarter 補完
            "ai_period_type_resolved_count": ai_period_type_resolved_count,
            "ai_quarter_resolved_count": ai_quarter_resolved_count,
            "ai_period_unresolved_count": ai_period_unresolved_count,
            "ai_period_reason_breakdown": dict(sorted(ai_period_reason_breakdown.items())),
            "avg_valid_segment_count": round(self._avg_of("valid_segment_count"), 2),
            "median_valid_segment_count": round(self._median_of("valid_segment_count"), 1),
            "avg_sales_non_null_count": round(self._avg_of("sales_non_null_count"), 2),
            "median_sales_non_null_count": round(self._median_of("sales_non_null_count"), 1),
            "avg_profit_non_null_count": round(self._avg_of("profit_non_null_count"), 2),
            "median_profit_non_null_count": round(self._median_of("profit_non_null_count"), 1),
            "narrative_contamination_count": narrative_count,
            "total_segment_rows": self.total_segment_rows,
            "avg_segments_per_filing": round(
                self.total_segment_rows / max(status_bd.get("ok", 0) + status_bd.get("partial", 0), 1), 1
            ),
            "elapsed_sec": round(self.elapsed_seconds, 1),
            "avg_duration_ms": round(self._avg_of("duration_ms"), 0),
            "median_duration_ms": round(self._median_of("duration_ms"), 0),
            "upserted": self.upserted_count,
            "upsert_inserted": self.upsert_inserted,
            "upsert_updated": self.upsert_updated,
            "upsert_failed_batches": self.upsert_failed_batches,
            "batch_count": self.batch_count,
        }

    def print_summary(self) -> None:
        d = self.summary_dict()
        print("\n" + "=" * 60)
        print("  Backfill Summary (V2 worker)")
        print("=" * 60)
        for k, v in d.items():
            if isinstance(v, dict):
                print(f"  {k}:")
                for kk, vv in v.items():
                    print(f"    {kk:30s} {vv}")
            else:
                print(f"  {k:35s} {v}")
        print("=" * 60)
