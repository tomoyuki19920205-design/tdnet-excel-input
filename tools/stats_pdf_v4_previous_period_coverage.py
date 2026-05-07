"""tools/stats_pdf_v4_previous_period_coverage.py

PDF V4 previous period 取得率の統計スクリプト。

既存コード変更禁止 / DB書き込み禁止 / Supabase同期禁止。

使い方:
    .venv\\Scripts\\python.exe -X utf8 tools\\stats_pdf_v4_previous_period_coverage.py --limit 50
    .venv\\Scripts\\python.exe -X utf8 tools\\stats_pdf_v4_previous_period_coverage.py --limit 100 --cache-root data/tdnet_cache
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# パス設定
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PDF V4 previous period coverage stats"
    )
    p.add_argument(
        "--limit", type=int, default=50,
        help="処理する PDF の最大件数 (default: 50)"
    )
    p.add_argument(
        "--cache-root", default="data/tdnet_cache",
        help="tdnet_cache のルートパス (default: data/tdnet_cache)"
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="各 PDF の処理結果を詳細表示"
    )
    return p.parse_args()


def _collect_pdfs(cache_root: Path, limit: int) -> list[Path]:
    """cache_root/**/source.pdf を limit 件収集する。"""
    pdfs = sorted(cache_root.glob("**/source.pdf"))[:limit]
    return pdfs


def _run_v4(pdf_path: Path) -> tuple[object | None, str]:
    """run_segment_detection_v4 を実行。(result, error_msg) を返す。"""
    try:
        from src.analysis.segment_detection_v4 import run_segment_detection_v4
        result = run_segment_detection_v4(str(pdf_path), ticker="")
        return result, ""
    except Exception as e:
        return None, str(e)[:200]


def _classify_no_previous_reason(result) -> str:
    """previous がない理由を分類する。"""
    if result is None:
        return "error"
    qr = getattr(result, "quarantine_reason", "")
    if qr:
        if "single_segment" in qr or "omit" in qr.lower():
            return "single_segment_omitted"
        if "quarantine" in qr.lower():
            return "quarantined"
        return f"quarantined:{qr}"
    eps = getattr(result, "extracted_periods", [])
    if not eps:
        segs = getattr(result, "segments", [])
        if not segs:
            return "no_segment"
        return "no_extracted_periods"
    types = [ep.period_type for ep in eps]
    if "current" in types:
        return "current_only"
    return "unknown_only"


def main() -> None:
    args = _parse_args()
    cache_root = ROOT / args.cache_root
    if not cache_root.exists():
        print(f"[ERROR] cache_root が見つかりません: {cache_root}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] cache_root = {cache_root}")
    print(f"[INFO] limit      = {args.limit}")
    print()

    pdfs = _collect_pdfs(cache_root, args.limit)
    if not pdfs:
        print("[WARN] source.pdf が見つかりませんでした。")
        sys.exit(0)

    # -----------------------------------------------------------------------
    # カウンター
    # -----------------------------------------------------------------------
    n_scanned      = len(pdfs)
    n_v4_success   = 0   # V4 result.success == True
    n_has_eps      = 0   # extracted_periods が 1件以上
    n_has_current  = 0
    n_has_previous = 0
    n_has_both     = 0
    n_errors       = 0

    no_prev_reasons: dict[str, int] = {}

    # サンプル収集
    samples_prev: list[dict] = []     # previous あり
    samples_no_prev: list[dict] = []  # previous なし

    elapsed_total = 0.0

    for i, pdf_path in enumerate(pdfs, 1):
        t0 = time.monotonic()
        result, err_msg = _run_v4(pdf_path)
        elapsed = time.monotonic() - t0
        elapsed_total += elapsed

        fid = pdf_path.parent.name  # ディレクトリ名 = filing_id
        v4_ok = (result is not None) and getattr(result, "success", False)
        if v4_ok:
            n_v4_success += 1

        if result is None:
            n_errors += 1
            reason = "error"
            no_prev_reasons[reason] = no_prev_reasons.get(reason, 0) + 1
            if args.verbose:
                print(f"  [{i:4d}/{n_scanned}] ERROR  {fid}: {err_msg}")
            if len(samples_no_prev) < 5:
                samples_no_prev.append({"fid": fid, "reason": reason, "detail": err_msg})
            continue

        eps = getattr(result, "extracted_periods", [])
        qr  = getattr(result, "quarantine_reason", "")
        segs = getattr(result, "segments", [])
        types = [ep.period_type for ep in eps]

        has_eps      = len(eps) > 0
        has_current  = "current" in types
        has_previous = "previous" in types

        if has_eps:
            n_has_eps += 1
        if has_current:
            n_has_current += 1
        if has_previous:
            n_has_previous += 1
        if has_current and has_previous:
            n_has_both += 1

        # previous なし → 理由分類
        if not has_previous:
            reason = _classify_no_previous_reason(result)
            no_prev_reasons[reason] = no_prev_reasons.get(reason, 0) + 1
            if len(samples_no_prev) < 5:
                samples_no_prev.append({
                    "fid": fid,
                    "reason": reason,
                    "quarantine": qr,
                    "types": types,
                    "n_segs": len(segs),
                })
        else:
            if len(samples_prev) < 5:
                prev_segs = [ep.segments for ep in eps if ep.period_type == "previous"]
                n_prev_segs = sum(len(s) for s in prev_segs)
                samples_prev.append({
                    "fid": fid,
                    "types": types,
                    "n_prev_segs": n_prev_segs,
                })

        if args.verbose:
            status = "✅" if has_previous else ("⚠️" if has_current else "❌")
            print(
                f"  [{i:4d}/{n_scanned}] {status} {fid}"
                f"  types={types}  qr={qr!r:.30s}"
                f"  ({elapsed:.1f}s)"
            )

    # -----------------------------------------------------------------------
    # 結果出力
    # -----------------------------------------------------------------------
    avg_sec = elapsed_total / n_scanned if n_scanned else 0.0
    cov_v4  = (n_has_previous / n_v4_success  * 100) if n_v4_success  else 0.0
    cov_eps = (n_has_previous / n_has_eps     * 100) if n_has_eps     else 0.0

    print("=" * 60)
    print("📊 PDF V4 previous period coverage stats")
    print("=" * 60)
    print(f"PDF scanned               : {n_scanned}")
    print(f"V4 success                : {n_v4_success}")
    print(f"extracted_periods present : {n_has_eps}")
    print(f"current present           : {n_has_current}")
    print(f"previous present          : {n_has_previous}")
    print(f"current + previous present: {n_has_both}")
    print(f"errors                    : {n_errors}")
    print()
    print(f"previous coverage among V4 success        : {cov_v4:.1f}%")
    print(f"previous coverage among extracted_periods : {cov_eps:.1f}%")
    print(f"avg time per PDF                          : {avg_sec:.2f}s")
    print()

    # previous なし理由内訳
    print("--- previous なし理由内訳 ---")
    for reason, cnt in sorted(no_prev_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason:<35s}: {cnt}")
    print()

    # サンプル: previous あり
    print("--- ✅ previous ありサンプル (最大5件) ---")
    if samples_prev:
        for s in samples_prev:
            print(f"  fid={s['fid']}  types={s['types']}  prev_segs={s['n_prev_segs']}")
    else:
        print("  (なし)")
    print()

    # サンプル: previous なし
    print("--- ❌ previous なしサンプル (最大5件) ---")
    if samples_no_prev:
        for s in samples_no_prev:
            detail = s.get("detail") or f"types={s.get('types',[])}"
            print(f"  fid={s['fid']}  reason={s['reason']}  {detail}")
    else:
        print("  (なし)")
    print()


if __name__ == "__main__":
    main()
