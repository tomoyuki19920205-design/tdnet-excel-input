"""tools/diagnose_quarantined_segments.py — 8077 / 2408 quarantined 案件の詳細診断

quarantined になった際の extract_segments_result.json を読み、
品質ゲートを再実行してセグメント行ごとの除外理由を出力する。

実行:
    python -X utf8 .\\tools\\diagnose_quarantined_segments.py

出力:
    out/diagnose_quarantined_8077_2408.csv
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(str(_PROJECT_ROOT))

TARGET_TICKERS = {"8077", "2408"}
DB_PATH        = "data/backfill_state.db"
CACHE_ROOT     = "data/tdnet_cache"
OUT_CSV        = "out/diagnose_quarantined_8077_2408.csv"

CSV_COLUMNS = [
    "ticker",
    "disclosure_date",
    "title",
    "reason",
    "selected_path",
    "valid_segment_count",
    "sales_non_null_count",
    "profit_non_null_count",
    "segment_name",
    "sales",
    "profit",
    "excluded_reason",
]

# 除外対象セグメント名パターン（合計行・調整額など）
_EXCLUDE_NAMES = {
    "合計", "計", "調整額", "調整", "全社", "消去",
    "報告セグメント計", "合計/消去", "合計・消去",
}


def _load_json(path: str) -> dict | list | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_excluded_reason(seg: dict, validation_map: dict[str, str]) -> str:
    """セグメント行の除外理由を返す。

    validation_map: {segment_name: excluded_reason} — バリデータ結果から構築
    """
    name = (seg.get("segment_name") or "").strip()

    # 1. 合計行・調整額パターン
    if any(pat in name for pat in _EXCLUDE_NAMES):
        return "excluded_pattern:total_or_adjustment_row"

    # 2. sales / profit 両方 null
    sales  = seg.get("segment_sales")
    profit = seg.get("segment_profit")
    if sales is None and profit is None:
        return "excluded_pattern:no_numeric_values"

    # 3. バリデータの per-segment 判定結果
    if name in validation_map:
        return validation_map[name]

    return ""


def _run_validator(seg_records: list[dict], source: str = "pdf") -> dict[str, str]:
    """バリデータを実行し {segment_name: excluded_reason} を返す。"""
    try:
        from src.segment.extraction_result_validator import validate_extraction_result
    except ImportError:
        return {}

    try:
        result = validate_extraction_result(seg_records, source=source)
    except Exception as e:
        print(f"  [warn] validator error: {e}")
        return {}

    vmap: dict[str, str] = {}
    for sv in (result.validations or []):
        name = sv.name or ""
        if not sv.is_valid:
            reason_str = getattr(sv.invalid_reason, "value", str(sv.invalid_reason))
            rule_str   = sv.matched_rule or ""
            vmap[name] = f"invalid:{reason_str}  rule={rule_str}"
    return vmap


def _find_cache_dirs_for_ticker(ticker: str, conn: sqlite3.Connection) -> list[dict]:
    """指定ティッカーの全 filing を返す（status問わず）。"""
    rows = conn.execute(
        "SELECT filing_id, ticker, disclosure_date, title, last_error "
        "FROM filing_state WHERE ticker = ? ORDER BY disclosure_date DESC",
        (ticker,),
    ).fetchall()
    return [dict(r) for r in rows]


def _has_quarantine_json(fid: str) -> bool:
    return os.path.exists(os.path.join(CACHE_ROOT, fid, "quarantine.json"))


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    all_rows: list[dict] = []

    for ticker in sorted(TARGET_TICKERS):
        filings = _find_cache_dirs_for_ticker(ticker, conn)
        print(f"\n[diagnose] ticker={ticker}: {len(filings)} filings in DB")

        for f in filings:
            fid = f["filing_id"]
            cache_dir = os.path.join(CACHE_ROOT, fid)

            # quarantine.json が存在するものに限定
            q_path  = os.path.join(cache_dir, "quarantine.json")
            seg_path = os.path.join(cache_dir, "extract_segments_result.json")
            meta_path = os.path.join(cache_dir, "metadata.json")

            q_data   = _load_json(q_path)
            seg_data = _load_json(seg_path)
            meta_data = _load_json(meta_path)

            if not isinstance(q_data, dict):
                continue  # quarantine.json なし → スキップ

            print(f"  fid={fid} date={f['disclosure_date']} reason={q_data.get('review_hint')}")

            # メタ情報
            title        = f.get("title") or (meta_data or {}).get("title", "")
            reason       = q_data.get("review_hint") or f.get("last_error") or ""
            selected_path = q_data.get("selected_source") or ""

            # セグメントレコード
            seg_records: list[dict] = seg_data if isinstance(seg_data, list) else []

            # ── バリデータを再実行して per-segment 除外理由を取得
            source = "pdf" if "pdf" in selected_path.lower() else "xbrl" if "xbrl" in selected_path.lower() else "pdf"
            vmap = _run_validator(seg_records, source=source)

            # ── バリデーション集計
            try:
                from src.segment.extraction_result_validator import validate_extraction_result
                full_val = validate_extraction_result(seg_records, source=source)
                valid_count  = full_val.valid_segment_count
                sales_nn     = full_val.sales_non_null_count
                profit_nn    = full_val.profit_non_null_count
            except Exception:
                valid_count = sales_nn = profit_nn = 0

            if not seg_records:
                # セグメント行なしの場合も1行出力
                all_rows.append({
                    "ticker":               ticker,
                    "disclosure_date":      f["disclosure_date"] or "",
                    "title":                title,
                    "reason":               reason,
                    "selected_path":        selected_path,
                    "valid_segment_count":  valid_count,
                    "sales_non_null_count": sales_nn,
                    "profit_non_null_count": profit_nn,
                    "segment_name":         "(no records)",
                    "sales":                "",
                    "profit":               "",
                    "excluded_reason":      "no_segment_records_in_cache",
                })
                continue

            # セグメント行ごとに展開
            for seg in seg_records:
                name   = (seg.get("segment_name") or "").strip()
                sales  = seg.get("segment_sales")
                profit = seg.get("segment_profit")
                excl   = _get_excluded_reason(seg, vmap)

                all_rows.append({
                    "ticker":               ticker,
                    "disclosure_date":      f["disclosure_date"] or "",
                    "title":                title,
                    "reason":               reason,
                    "selected_path":        selected_path,
                    "valid_segment_count":  valid_count,
                    "sales_non_null_count": sales_nn,
                    "profit_non_null_count": profit_nn,
                    "segment_name":         name,
                    "sales":                "" if sales is None else sales,
                    "profit":               "" if profit is None else profit,
                    "excluded_reason":      excl,
                })

    conn.close()

    if not all_rows:
        print("\n[diagnose] 対象データが見つかりませんでした。")
        return

    Path(OUT_CSV).parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n[diagnose] CSV 出力完了: {OUT_CSV}  ({len(all_rows)} 行)")

    # ── サマリ表示
    print("\n--- 診断結果サマリ ---")
    for r in all_rows:
        print(
            f"  [{r['ticker']}] {r['disclosure_date']}  seg={r['segment_name'][:40]!r}"
            f"  sales={r['sales']}  profit={r['profit']}"
            f"  excluded={r['excluded_reason']}"
        )


if __name__ == "__main__":
    main()
