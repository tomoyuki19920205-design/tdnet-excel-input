"""tools/build_pdf_segment_gt.py — PDF セグメント GT（20件）自動生成

キャッシュにある segment_results.json を走査し、
条件に合う Filing を 20 件選出して data/pdf_segment_gt.json を作成する。

実行:
    python tools/build_pdf_segment_gt.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ============================================================
# パス設定
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT   = PROJECT_ROOT / "data" / "tdnet_cache"
OUT_PATH     = PROJECT_ROOT / "data" / "pdf_segment_gt.json"

# ============================================================
# フィルタ条件
# ============================================================
MIN_SEGMENTS    = 2      # 2セグメント以上
MIN_SALES_COUNT = 2      # sales が 2件以上
TARGET_COUNT    = 20     # GT 件数

EXCLUDE_TICKERS = set()  # 個別除外ティッカー (あれば)

# ============================================================
# ヘルパー
# ============================================================

def load_json_safe(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def detect_layout(records: list[dict]) -> str:
    """縦持ち (vertical) / 横持ち (horizontal) を簡易判定。
    segment_name が複数ある → vertical
    segment_count が多い → vertical (col_as_seg の横持ち判定は rule_trace 参照)
    """
    names = [r.get("segment_name", "") for r in records if r.get("segment_name")]
    if len(set(names)) >= len(names) * 0.8:
        return "vertical"
    return "horizontal"


def detect_period_type(records: list[dict]) -> str:
    """quarter から YTD / QTD を判定。"""
    quarters = [r.get("quarter", "") for r in records if r.get("quarter")]
    if not quarters:
        return "unknown"
    q = quarters[0]
    # Q1通期累計("1Q") / 2Q累計("2Q") / etc. — "Q4" = 通期 (YTD)
    if q in ("Q4", "4Q", "annual", "full"):
        return "YTD"
    return "YTD" if q.startswith("1") else "QTD"


def classify_layout_from_trace(filing_dir: Path) -> str:
    """logs.jsonl の rule_trace から col_as_seg を検出して horizontal/vertical を判定。"""
    logs_path = filing_dir / "logs.jsonl"
    if not logs_path.exists():
        return "vertical"
    try:
        for line in logs_path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            traces = obj.get("rule_trace", [])
            if any("col_as_seg" in t or "column_first" in t or "COLUMN_FIRST" in t for t in traces):
                return "horizontal"
    except Exception:
        pass
    return "vertical"


# ============================================================
# 候補収集
# ============================================================

def collect_candidates() -> list[dict]:
    """キャッシュを走査してGT候補を収集する。"""
    candidates = []

    for filing_dir in sorted(CACHE_ROOT.iterdir()):
        if not filing_dir.is_dir():
            continue

        pdf_path = filing_dir / "source.pdf"
        seg_path = filing_dir / "extract_segments_result.json"
        meta_path = filing_dir / "metadata.json"

        if not pdf_path.exists() or not seg_path.exists():
            continue

        records = load_json_safe(seg_path)
        meta    = load_json_safe(meta_path) if meta_path.exists() else {}

        if not records or not isinstance(records, list):
            continue

        ticker    = meta.get("ticker", "")
        company   = meta.get("title", "")[:40] if meta else ""
        filing_id = filing_dir.name

        # ---- フィルタ ----
        if ticker in EXCLUDE_TICKERS:
            continue

        # セグメント行数
        seg_names = [r for r in records if r.get("segment_name")]
        if len(seg_names) < MIN_SEGMENTS:
            continue

        # sales がある件数
        sales_count = sum(
            1 for r in records
            if r.get("segment_sales") is not None and r.get("segment_name")
        )
        if sales_count < MIN_SALES_COUNT:
            continue

        # ETF / REIT 除外 (会社名キーワード)
        company_lower = company.lower()
        if any(kw in company_lower for kw in ["etf", "reit", "投資法人", "上場投信"]):
            continue

        # ---- セグメントリスト構築 ----
        segs = []
        for r in records:
            name = r.get("segment_name", "").strip()
            if not name:
                continue
            # 合計行・調整行はスキップ
            if any(kw in name for kw in ["合計", "調整額", "全社", "消去", "計"]):
                continue
            segs.append({
                "name":   name,
                "sales":  r.get("segment_sales"),
                "profit": r.get("segment_profit"),
            })

        if len(segs) < MIN_SEGMENTS:
            continue

        has_profit = any(s["profit"] is not None for s in segs)

        # ---- ユニット ----
        unit_raw = next(
            (r.get("unit_raw", r.get("source", "")) for r in records if r.get("unit_raw")),
            "百万円",
        )

        # ---- レイアウト / 期間タイプ ----
        layout      = classify_layout_from_trace(filing_dir)
        period_type = detect_period_type(records)
        quarter     = records[0].get("quarter", "") if records else ""
        period      = records[0].get("period", "")  if records else ""

        candidates.append({
            "filing_id":   filing_id,
            "ticker":      ticker,
            "company_name": company,
            "pdf_path":    str(pdf_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "has_segment": True,
            "segments":    segs,
            "meta": {
                "layout":      layout,
                "period_type": period_type,
                "has_profit":  has_profit,
                "unit":        unit_raw,
                "quarter":     quarter,
                "period":      period,
                "seg_count":   len(segs),
            },
        })

    return candidates


# ============================================================
# 選出ロジック（分布を満たす 20 件）
# ============================================================

def select_gt(candidates: list[dict], target: int = TARGET_COUNT) -> list[dict]:
    """水平/垂直・YTD/QTD・利益あり/なしの分布を満たすよう選出。"""
    horiz   = [c for c in candidates if c["meta"]["layout"] == "horizontal"]
    vert    = [c for c in candidates if c["meta"]["layout"] == "vertical"]
    ytd     = [c for c in candidates if c["meta"]["period_type"] == "YTD"]
    qtd     = [c for c in candidates if c["meta"]["period_type"] == "QTD"]
    w_profit  = [c for c in candidates if c["meta"]["has_profit"]]
    wo_profit = [c for c in candidates if not c["meta"]["has_profit"]]

    selected: list[dict] = []
    seen_ids: set[str] = set()

    def add(pool, n):
        added = 0
        for c in pool:
            if added >= n:
                break
            if c["filing_id"] not in seen_ids:
                selected.append(c)
                seen_ids.add(c["filing_id"])
                added += 1

    # 優先割り当て
    add(horiz,    5)     # 横持ち 5件
    add(vert,     5)     # 縦持ち 5件
    add(ytd,      5)     # YTD   5件
    add(qtd,      5)     # QTD   5件

    # 残り埋め: 利益ありを優先
    add(w_profit,  15)   # 利益あり 15件 (既選出分は skip)
    add(candidates, target)  # 残り全候補で補完

    return selected[:target]


# ============================================================
# メイン
# ============================================================

def main():
    print(f"キャッシュ走査: {CACHE_ROOT}")
    candidates = collect_candidates()
    print(f"候補件数: {len(candidates)}")

    if not candidates:
        print("候補が見つかりませんでした。キャッシュに PDF + segment_results.json が必要です。")
        sys.exit(1)

    gt = select_gt(candidates)
    print(f"選出件数: {len(gt)}")

    # 分布レポート
    layouts = [c["meta"]["layout"] for c in gt]
    periods = [c["meta"]["period_type"] for c in gt]
    profits = [c["meta"]["has_profit"] for c in gt]
    print(f"  layout : horizontal={layouts.count('horizontal')}  vertical={layouts.count('vertical')}")
    print(f"  period : YTD={periods.count('YTD')}  QTD={periods.count('QTD')}  unknown={periods.count('unknown')}")
    print(f"  profit : あり={profits.count(True)}  なし={profits.count(False)}")

    # 保存 (filing_id フィールドは GT には不要なので除去)
    out = []
    for c in gt:
        entry = {k: v for k, v in c.items() if k != "filing_id"}
        out.append(entry)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nGT 保存: {OUT_PATH}")
    print("\n--- GT プレビュー ---")
    for i, e in enumerate(gt, 1):
        segs = e["segments"][:3]
        seg_preview = ", ".join(s["name"] for s in segs)
        print(f"  {i:02d}. [{e['ticker']}] {e['company_name'][:20]} | {e['meta']['layout']}/{e['meta']['period_type']} | segs={e['meta']['seg_count']} | {seg_preview}")


if __name__ == "__main__":
    main()
