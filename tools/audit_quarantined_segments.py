"""tools/audit_quarantined_segments.py — quarantined 案件の監査 CSV 出力ツール

status='quarantined' の filing を state_store DB から全件取得し、
各キャッシュディレクトリの quarantine.json / extract_segments_result.json /
metadata.json を参照して監査用 CSV を生成する。

実行:
    python -X utf8 .\\tools\\audit_quarantined_segments.py

出力:
    out/quarantined_segments_audit.csv
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from pathlib import Path
from collections import Counter

# ── プロジェクトルートを sys.path に追加（tools/ 直下からの実行を想定）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(str(_PROJECT_ROOT))

# ── 設定
DB_PATH      = "data/backfill_state.db"
CACHE_ROOT   = "data/tdnet_cache"
OUT_DIR      = "out"
OUT_CSV      = os.path.join(OUT_DIR, "quarantined_segments_audit.csv")

CSV_COLUMNS = [
    "ticker",
    "company_name",
    "disclosure_date",
    "title",
    "reason",
    "selected_path",
    "fallback_reason",
    "pdf_path",
    "xbrl_path",
    "valid_segment_count",
    "sales_non_null_count",
    "profit_non_null_count",
]


def _load_json(path: str) -> dict | list | None:
    """JSON を安全に読み込む。失敗時は None。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _count_segment_stats(records: list[dict]) -> tuple[int, int, int]:
    """extract_segments_result.json の行リストから統計を計算する。

    Returns:
        (valid_count, sales_non_null, profit_non_null)
    """
    if not isinstance(records, list):
        return 0, 0, 0

    # 除外パターン（合計行・調整額・全社など）
    _EXCLUDE = {"合計", "計", "調整額", "調整", "全社", "消去", "報告セグメント計", "合計/消去", "合計・消去"}

    valid = []
    for r in records:
        name = (r.get("segment_name") or "").strip()
        if not name:
            continue
        if any(pat in name for pat in _EXCLUDE):
            continue
        valid.append(r)

    sales_non_null  = sum(1 for r in valid if r.get("segment_sales")  is not None)
    profit_non_null = sum(1 for r in valid if r.get("segment_profit") is not None)
    return len(valid), sales_non_null, profit_non_null


def _build_row(db_row: dict) -> dict:
    """DB 行 + キャッシュ JSON から CSV 1 行分の dict を構築する。"""
    fid            = db_row["filing_id"]
    ticker         = db_row.get("ticker") or ""
    disclosure_date = db_row.get("disclosure_date") or ""
    title          = db_row.get("title") or ""
    reason         = db_row.get("last_error") or db_row.get("review_hint") or ""

    cache_dir      = os.path.join(CACHE_ROOT, fid)
    pdf_path       = ""
    xbrl_path      = ""
    if os.path.exists(cache_dir):
        _pdf  = os.path.join(cache_dir, "source.pdf")
        _xbrl = os.path.join(cache_dir, "xbrl.zip")
        if os.path.exists(_pdf):
            pdf_path  = _pdf
        if os.path.exists(_xbrl):
            xbrl_path = _xbrl

    # ── quarantine.json から selected_path / fallback_reason を取得
    selected_path  = ""
    fallback_reason = ""
    company_name   = ""

    q_data = _load_json(os.path.join(cache_dir, "quarantine.json"))
    if isinstance(q_data, dict):
        selected_path   = q_data.get("selected_source") or ""
        fallback_reason = q_data.get("candidate_summary") or ""

    # ── metadata.json から company_name 補完（title で代替することが多い）
    m_data = _load_json(os.path.join(cache_dir, "metadata.json"))
    if isinstance(m_data, dict):
        company_name = m_data.get("company_name") or ""
        # metadata に source_url があれば pdf_path フォールバック
        if not pdf_path:
            src_url = m_data.get("source_url") or ""
            if src_url.endswith(".pdf"):
                pdf_path = src_url  # URL のまま記録（キャッシュなし）

    # ── extract_segments_result.json からセグメント統計
    seg_data = _load_json(os.path.join(cache_dir, "extract_segments_result.json"))
    valid_count, sales_non_null, profit_non_null = 0, 0, 0
    if isinstance(seg_data, list):
        valid_count, sales_non_null, profit_non_null = _count_segment_stats(seg_data)

    return {
        "ticker":               ticker,
        "company_name":         company_name,
        "disclosure_date":      disclosure_date,
        "title":                title,
        "reason":               reason,
        "selected_path":        selected_path,
        "fallback_reason":      fallback_reason,
        "pdf_path":             pdf_path,
        "xbrl_path":            xbrl_path,
        "valid_segment_count":  valid_count,
        "sales_non_null_count": sales_non_null,
        "profit_non_null_count": profit_non_null,
    }


def main() -> None:
    # ── DB 接続
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM filing_state WHERE status = 'quarantined' ORDER BY disclosure_date ASC, ticker ASC"
    ).fetchall()
    conn.close()

    if not rows:
        print("[audit] quarantined 件数: 0 件。CSVは出力しません。")
        return

    print(f"[audit] quarantined 件数: {len(rows)} 件")

    # ── CSV 出力
    Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    csv_rows = []
    for db_row in rows:
        csv_rows.append(_build_row(dict(db_row)))

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"[audit] CSV 出力完了: {OUT_CSV}")

    # ── reason 別件数を標準出力
    reason_counts = Counter(r["reason"] for r in csv_rows)
    print("\n--- reason 別件数 ---")
    for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {reason:<40} {cnt:>4} 件")

    # ── selected_path 別件数
    path_counts = Counter(r["selected_path"] for r in csv_rows)
    print("\n--- selected_path 別件数 ---")
    for sp, cnt in sorted(path_counts.items(), key=lambda x: -x[1]):
        label = sp if sp else "(未記録)"
        print(f"  {label:<20} {cnt:>4} 件")

    # ── valid_segment_count 分布
    print("\n--- valid_segment_count 分布 ---")
    vc_counts = Counter(r["valid_segment_count"] for r in csv_rows)
    for vc, cnt in sorted(vc_counts.items()):
        print(f"  {vc} 件セグメント: {cnt} filings")


if __name__ == "__main__":
    main()
