#!/usr/bin/env python3
r"""
retry_quarantine_segments.py -- quarantined PDF filing の再解析 CLI

Usage:
  .\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --dry-run
  .\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --apply --limit 50
  .\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --apply --only-hint pdf_table_parse_failed
  .\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --dry-run --only-hint pdf_no_segment_table_candidate --debug-table-candidates
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("retry_quarantine")


def _safe_print(label: str, value) -> None:
    """cp932 safe な debug 印字."""
    try:
        text = str(value)
        safe = text.encode("cp932", errors="replace").decode("cp932")
        print(f"{label} {safe}")
    except Exception:
        print(f"{label} (encoding error)")


def _get_quarantined_filings(
    state_db: str,
    *,
    only_hint: str | None = None,
    tickers: list[str] | None = None,
    limit: int = 0,
    include_rescued_partial: bool = False,
) -> list[dict]:
    """state DB から quarantined filing を取得する。
    
    include_rescued_partial=True の場合:
    - quarantined + hint=pdf_no_sales_profit_columns も取得
      (dry-run 後 DB 未更新の partial 群を含めるため)
    - upserted + hint=pdf_partial_sales_only も取得
      (apply 済みの partial 群を再 review するため)
    """
    from lib.backfill.state_store import BackfillStateStore
    store = BackfillStateStore(state_db)
    rows = store.get_pending(statuses=["quarantined"])

    # hint フィルタの構築
    hint_set: set[str] | None = None
    if only_hint:
        hint_set = {h.strip() for h in only_hint.split(",")}

    # include_rescued_partial の場合、hint set を拡張
    if include_rescued_partial and hint_set:
        # pdf_partial_sales_only が指定されたら、
        # DB 上はまだ pdf_no_sales_profit_columns のままなので追加
        if "pdf_partial_sales_only" in hint_set:
            hint_set.add("pdf_no_sales_profit_columns")

        # apply 済みの rescued partial も取得 (upserted + pdf_partial_sales_only)
        try:
            upserted_rows = store.get_pending(statuses=["upserted"])
            for r in upserted_rows:
                rh = r.get("review_hint", "")
                if rh == "pdf_partial_sales_only":
                    r["_source"] = "rescued_partial"
                    rows.append(r)
        except Exception:
            pass  # upserted が取れない場合は ignore

    # hint フィルタ適用
    if hint_set:
        rows = [r for r in rows if r.get("review_hint", "") in hint_set
                or (not r.get("review_hint") and "pdf_table_parse_failed" in hint_set)]

    if tickers:
        rows = [r for r in rows if r.get("ticker") in tickers]

    if limit and limit > 0:
        rows = rows[:limit]

    store.close()
    return rows


def _retry_one(filing_row: dict, cache_root: str, *, debug: bool = False) -> dict:
    """1件の quarantined filing を再解析する。
    
    Returns:
        {"status": "rescued"|"still_quarantined", "hint": str, "segments": int, ...}
    """
    from lib.backfill.cache import CachePaths
    
    fid = filing_row["filing_id"]
    ticker = filing_row.get("ticker", "")
    
    cache_dir = Path(cache_root) / fid[:2] / fid
    if not cache_dir.exists():
        cache_dir = Path(cache_root) / fid

    # PDF path を探す
    pdf_path = None
    for ext in [".pdf"]:
        for candidate in cache_dir.glob(f"*{ext}"):
            pdf_path = str(candidate)
            break
    
    if not pdf_path:
        # source_pdf パターンも試す
        for candidate in cache_dir.glob("source.*"):
            if candidate.suffix == ".pdf":
                pdf_path = str(candidate)
                break
    
    if not pdf_path:
        return {
            "status": "still_quarantined",
            "hint": "cache_pdf_not_found",
            "segments": 0,
            "filing_id": fid,
            "ticker": ticker,
        }
    
    # V2 セグメント抽出実行
    try:
        from src.analysis.segment_detection_v2 import run_segment_detection_v2
        v2_result = run_segment_detection_v2(pdf_path, doc_id=fid, ticker=ticker)
        
        # Phase 7: dry-run column_diagnosis debug
        if debug:
            col_diag = (v2_result.score_summary or {}).get("column_diagnosis", {})
            print(f"\n[DEBUG] column_diagnosis  filing={fid[:20]}... ticker={ticker}")
            if col_diag:
                print("  resolution_strategy:", col_diag.get("resolution_strategy"))
                _safe_print("  raw_headers:", col_diag.get("raw_headers"))
                _safe_print("  new_headers:", col_diag.get("new_headers"))
                _safe_print("  legacy_headers:", col_diag.get("legacy_headers"))
                _safe_print("  reconstruction_steps:", col_diag.get("reconstruction_steps"))
                print("  best_sales_col:", col_diag.get("best_sales_col"))
                print("  best_profit_col:", col_diag.get("best_profit_col"))
                print("  profit_role:", col_diag.get("profit_role"))
                roles = col_diag.get("column_roles", [])
                print("  column_roles:", roles[:8] if roles else [])
                # sales candidates
                sc_list = col_diag.get("sales_candidates")
                if sc_list:
                    print("  sales_candidates:")
                    for sc in sc_list:
                        mark = "*" if sc.get("selected") else " "
                        _safe_print(f"    [{mark}] col={sc.get('col')}", f"header={sc.get('header')} best={sc.get('best_role')}({sc.get('best_score')}) {sc.get('scores')}")
                else:
                    print("  sales_candidates: None")
                # profit candidates
                pc_list = col_diag.get("profit_candidates")
                if pc_list:
                    print("  profit_candidates:")
                    for pc in pc_list:
                        mark = "✓" if pc.get("selected") else " "
                        _safe_print(f"    [{mark}] col={pc.get('col')}", f"header={pc.get('header')} best={pc.get('best_role')}({pc.get('best_score')}) {pc.get('scores')}")
                else:
                    print("  profit_candidates: None")
                # rule_trace (最後5件)
                rt = v2_result.rule_trace[-5:] if v2_result.rule_trace else []
                if rt:
                    print("  rule_trace (last 5):")
                    for t_line in rt:
                        _safe_print("    ", t_line)
            else:
                print("  (column_diagnosis empty)")

        result_dict: dict = {}

        if v2_result.success and v2_result.segments:
            # v2_result.review_hint が設定されていれば伝播 (e.g. pdf_partial_sales_only)
            _rescued_hint = getattr(v2_result, 'review_hint', '') or ''
            result_dict = {
                "status": "rescued",
                "hint": _rescued_hint,
                "segments": len(v2_result.segments),
                "filing_id": fid,
                "ticker": ticker,
                "parse_qualities": [s.parse_quality for s in v2_result.segments],
            }
        else:
            # V2 失敗
            from lib.backfill.retry import classify_review_hint
            new_hint = classify_review_hint(
                "pdf", v2_result.quarantine_reason or "unknown", False,
                v2_reason=v2_result.quarantine_reason,
            )
            result_dict = {
                "status": "still_quarantined",
                "hint": new_hint,
                "segments": 0,
                "filing_id": fid,
                "ticker": ticker,
                "v2_reason": v2_result.quarantine_reason,
                "v2_stage": v2_result.failed_stage,
            }

        # Phase 5: debug 情報の付加
        if debug:
            result_dict["debug"] = {
                "candidate_tables_count": v2_result.candidate_tables_count,
                "scored_pages_count": v2_result.scored_pages_count,
                "all_table_scores": v2_result.score_summary.get("all_table_scores", []),
                "rule_trace": v2_result.rule_trace[-8:] if v2_result.rule_trace else [],
                "column_diagnosis": v2_result.score_summary.get("column_diagnosis", {}),
            }

        return result_dict
    except Exception as e:
        return {
            "status": "still_quarantined",
            "hint": f"exception:{str(e)[:100]}",
            "segments": 0,
            "filing_id": fid,
            "ticker": ticker,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="quarantined PDF filing の再解析 (PDF parse 改善後)",
    )
    parser.add_argument("--apply", action="store_true", help="state DB 更新 (upsert)")
    parser.add_argument("--dry-run", action="store_true", help="再解析のみ、DB更新なし (default)")
    parser.add_argument("--only-hint", type=str, default=None,
                        help="指定 review_hint のみ対象 (カンマ区切りで複数OK、例: pdf_no_sales_profit_columns,pdf_no_segment_page_candidate)")
    parser.add_argument("--limit", type=int, default=0, help="対象件数制限 (0=unlimited)")
    parser.add_argument("--tickers", type=str, default=None,
                        help="カンマ区切り ticker (例: 1234,5678)")
    parser.add_argument("--state-db", default="data/backfill_state.db", help="state DB path")
    parser.add_argument("--cache-root", default="data/tdnet_cache", help="cache root dir")
    parser.add_argument("--write-report", action="store_true", help="JSON レポート出力")
    # Phase 5 追加
    parser.add_argument("--debug-table-candidates", action="store_true",
                        help="各 filing の table candidate 情報を表示 (スコア内訳含む)")
    parser.add_argument("--debug-column-roles", action="store_true",
                        help="各 filing の列ロール診断情報を表示 (normalized headers, candidates, scores)")
    parser.add_argument("--include-rescued-partial", action="store_true",
                        help="rescued だが partial_sales_only の filing も再解析対象に含める")
    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    dry_run = not opts.apply or opts.dry_run
    mode = "DRY-RUN" if dry_run else "APPLY"
    state_db = os.path.join(_PROJECT_ROOT, opts.state_db)
    cache_root = os.path.join(_PROJECT_ROOT, opts.cache_root)
    debug = opts.debug_table_candidates or opts.debug_column_roles
    debug_columns = opts.debug_column_roles

    tickers = opts.tickers.split(",") if opts.tickers else None
    include_rescued_partial = getattr(opts, 'include_rescued_partial', False)

    logger.info(f"[start] mode={mode} state_db={state_db}")
    logger.info(f"[start] only_hint={opts.only_hint} limit={opts.limit or 'unlimited'} include_rescued_partial={include_rescued_partial}")

    # 1. quarantined 取得
    targets = _get_quarantined_filings(
        state_db,
        only_hint=opts.only_hint,
        tickers=tickers,
        limit=opts.limit,
        include_rescued_partial=include_rescued_partial,
    )

    if not targets:
        print("[retry] No quarantined filings found matching criteria")
        return 0

    # ターゲット内訳
    _quarantined_count = sum(1 for t in targets if t.get("_source") != "rescued_partial")
    _rescued_partial_count = sum(1 for t in targets if t.get("_source") == "rescued_partial")
    logger.info(f"[targets] {len(targets)} filings (quarantined={_quarantined_count}, rescued_partial={_rescued_partial_count})")

    # before hint 集計
    hint_before = Counter()
    for t in targets:
        hint_before[t.get("review_hint", "unknown")] += 1

    # 2. 再解析
    results = []
    rescued = 0
    still_q = 0
    t0 = time.monotonic()

    for i, row in enumerate(targets, 1):
        result = _retry_one(row, cache_root, debug=debug)
        results.append(result)

        if result["status"] == "rescued":
            rescued += 1
        else:
            still_q += 1

        if i % 10 == 0 or i == len(targets):
            logger.info(f"[progress] {i}/{len(targets)} rescued={rescued} still_q={still_q}")

    elapsed = time.monotonic() - t0

    # 3. after hint 集計 (still_quarantined + rescued_partial)
    hint_after = Counter()
    for r in results:
        if r["status"] == "still_quarantined":
            hint_after[r.get("hint", "unknown")] += 1
        elif r["status"] == "rescued" and r.get("hint"):
            # rescued だが品質限定 (e.g. pdf_partial_sales_only)
            hint_after[r.get("hint")] += 1

    # Phase 5: hint 遷移マトリクス
    hint_transition: Counter = Counter()
    rescued_full = 0
    rescued_partial = 0
    for r, t in zip(results, targets):
        before_hint = t.get("review_hint", "unknown")
        if r["status"] == "rescued":
            qualities = r.get("parse_qualities", ["full"])
            has_full = any(pq == "full" for pq in qualities)
            if has_full:
                after_hint = "rescued"
                rescued_full += 1
            else:
                after_hint = "rescued_(partial_sales_only)"
                rescued_partial += 1
        else:
            after_hint = r.get("hint", "unknown")
        hint_transition[(before_hint, after_hint)] += 1

    # 4. apply: state DB 更新
    if not dry_run:
        from lib.backfill.state_store import BackfillStateStore
        store = BackfillStateStore(state_db)
        applied = 0
        hint_updated = 0

        for r in results:
            if r["status"] == "rescued":
                try:
                    store.update_status(
                        r["filing_id"], "upserted",
                        stage="retry_rescued",
                    )
                    applied += 1
                except Exception as e:
                    logger.warning(f"[apply] {r['filing_id']} rescued update failed: {e}")
            elif r["status"] == "still_quarantined" and r.get("hint"):
                # still_quarantined の新 review_hint を永続反映
                try:
                    store.update_review_hint(r["filing_id"], r["hint"])
                    hint_updated += 1
                except Exception as e:
                    logger.warning(f"[apply] {r['filing_id']} hint update failed: {e}")

        store.close()
        logger.info(f"[apply] rescued={applied} hint_updated={hint_updated}")

    # 5. Summary
    rescue_rate = (rescued / len(targets) * 100) if targets else 0

    print()
    print("=" * 60)
    print(f"  Quarantine Retry - {mode}")
    print("=" * 60)
    print(f"  total_targets           : {len(targets)}")
    if _rescued_partial_count > 0:
        print(f"    quarantined_targets  : {_quarantined_count}")
        print(f"    rescued_partial_tgts : {_rescued_partial_count}")
    print(f"  rescued                 : {rescued}")
    if rescued > 0:
        print(f"    rescued_full          : {rescued_full}")
        print(f"    rescued_partial       : {rescued_partial}")
    print(f"  still_quarantined       : {still_q}")
    print(f"  rescue_rate             : {rescue_rate:.1f}%")
    # partial → full 昇格統計
    _promoted = sum(
        1 for r, t in zip(results, targets)
        if t.get("_source") == "rescued_partial"
        and r["status"] == "rescued"
        and any(pq == "full" for pq in r.get("parse_qualities", []))
    )
    _stayed = sum(
        1 for r, t in zip(results, targets)
        if t.get("_source") == "rescued_partial"
        and r["status"] == "rescued"
        and not any(pq == "full" for pq in r.get("parse_qualities", []))
    )
    _worsened = sum(
        1 for r, t in zip(results, targets)
        if t.get("_source") == "rescued_partial"
        and r["status"] == "still_quarantined"
    )
    if _rescued_partial_count > 0:
        print(f"  partial_review:")
        print(f"    promoted_full         : {_promoted}")
        print(f"    stayed_partial        : {_stayed}")
        print(f"    worsened              : {_worsened}")
    print(f"  elapsed_sec             : {elapsed:.1f}")
    if targets:
        print(f"  avg_sec_per_filing      : {elapsed / len(targets):.2f}")
    print()
    print(f"  hint_breakdown_before:")
    for hint, cnt in hint_before.most_common():
        print(f"    {hint:40s}: {cnt}")
    print()
    print(f"  hint_breakdown_after:")
    for hint, cnt in hint_after.most_common():
        print(f"    {hint:40s}: {cnt}")

    # Phase 5: hint 遷移表示
    print()
    print(f"  hint_transition:")
    for (bh, ah), cnt in hint_transition.most_common():
        print(f"    {bh:35s} -> {ah:35s}: {cnt}")

    # parse_quality 内訳
    pq_counter = Counter()
    for r in results:
        if r["status"] == "rescued":
            qualities = r.get("parse_qualities", ["full"])
            for pq in qualities:
                pq_counter[pq] += 1
    if pq_counter:
        print()
        print(f"  parse_quality_breakdown:")
        for pq, cnt in pq_counter.most_common():
            print(f"    {pq:40s}: {cnt}")

    print("=" * 60)

    # Phase 5: debug 表示
    if debug:
        print()
        print("=" * 60)
        print("  Table Candidate Debug")
        print("=" * 60)
        for r in results:
            dbg = r.get("debug")
            if not dbg:
                continue
            print(f"\n  filing: {r.get('filing_id', '?')[:20]}... ticker={r.get('ticker', '?')}")
            print(f"    status           : {r['status']}")
            print(f"    scored_pages     : {dbg.get('scored_pages_count', '?')}")
            print(f"    candidate_tables : {dbg.get('candidate_tables_count', '?')}")
            table_scores = dbg.get("all_table_scores", [])
            if table_scores:
                print(f"    top table candidates:")
                for j, ts in enumerate(table_scores[:3], 1):
                    print(f"      #{j}: page={ts.get('page','?')} score={ts.get('score','?')}"
                          f" weak={ts.get('weak_evidence',False)}")
                    cats = ts.get("categories", {})
                    if cats:
                        parts = [f"{k}={v}" for k, v in cats.items() if v != 0]
                        print(f"          categories: {', '.join(parts)}")
                    reason = ts.get("reason", "")
                    if reason:
                        print(f"          reason: {reason}")
            else:
                print(f"    no table candidates found")
            # rule trace
            trace = dbg.get("rule_trace", [])
            if trace:
                print(f"    rule_trace (last {len(trace)}):")
                for t_line in trace:
                    # cp932 safe
                    safe = t_line.encode("cp932", errors="replace").decode("cp932")
                    print(f"      {safe}")
        print("=" * 60)

    # Phase 5: column roles debug 表示
    if debug_columns:
        print()
        print("=" * 60)
        print("  Column Roles Debug")
        print("=" * 60)
        for r in results:
            dbg = r.get("debug")
            if not dbg:
                continue
            col_diag = dbg.get("column_diagnosis", {})
            if not col_diag:
                continue
            print(f"\n  filing: {r.get('filing_id', '?')[:20]}... ticker={r.get('ticker', '?')}")
            print(f"    status           : {r['status']}")
            raw = col_diag.get("raw_headers", [])
            if raw:
                for rh in raw[:3]:
                    safe = str(rh).encode("cp932", errors="replace").decode("cp932")[:80]
                    print(f"    raw_header       : {safe}")
            recon = col_diag.get("reconstructed_headers", [])
            if recon:
                for rch in recon[:5]:
                    safe = str(rch).encode("cp932", errors="replace").decode("cp932")[:60]
                    print(f"    reconstructed    : {safe}")
            roles = col_diag.get("column_roles", [])
            if roles:
                print(f"    column_roles     : {roles[:5]}")
            print(f"    sales_col={col_diag.get('best_sales_col', '?')} profit_col={col_diag.get('best_profit_col', '?')} profit_role={col_diag.get('profit_role', '')}")
            # sales candidates
            sc_list = col_diag.get("sales_candidates")
            if sc_list:
                print("    sales_candidates:")
                for sc in sc_list:
                    mark = "*" if sc.get("selected") else " "
                    _safe_print(f"      [{mark}] col={sc.get('col')}", f"header={sc.get('header')} best={sc.get('best_role')}({sc.get('best_score')}) {sc.get('scores')}")
            else:
                print("    sales_candidates: None")
            # profit candidates
            pc_list = col_diag.get("profit_candidates")
            if pc_list:
                print("    profit_candidates:")
                for pc in pc_list:
                    mark = "✓" if pc.get("selected") else " "
                    _safe_print(f"      [{mark}] col={pc.get('col')}", f"header={pc.get('header')} best={pc.get('best_role')}({pc.get('best_score')}) {pc.get('scores')}")
            else:
                print("    profit_candidates: None")
        print("=" * 60)

    # 6. report
    if opts.write_report:
        report_path = os.path.join(_PROJECT_ROOT, "logs", "quarantine_retry_report.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        report = {
            "mode": mode,
            "total_targets": len(targets),
            "rescued": rescued,
            "still_quarantined": still_q,
            "rescue_rate": round(rescue_rate, 1),
            "elapsed_sec": round(elapsed, 1),
            "hint_before": dict(hint_before),
            "hint_after": dict(hint_after),
            "hint_transition": {f"{bh}->{ah}": cnt for (bh, ah), cnt in hint_transition.items()},
            "results": results,
        }
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"  report: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
