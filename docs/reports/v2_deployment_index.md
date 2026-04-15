# V2 XBRL Only — ドキュメントインデックス

> **方針変更 (2026-03-15)**: TDNET セグメント抽出は **XBRL only**。PDF path は **disabled**。

## 設計方針

| レイヤー | 方針 | 精度目標 |
|---------|------|---------|
| **TDNET (速報)** | XBRL only / precision 最優先 | 93%+ (segment あり subset) |
| EDINET (後続) | 別系統で coverage 拡張 | TBD |

- XBRL source がない開示は `no_xbrl_segment_source` (速報時点で未取得)
- 「セグメントなし」とは断定しない

## Status / Quarantine Reason 体系

| 状況 | status | quarantine_reason | route_mode |
|------|--------|-------------------|------------|
| XBRL あり + 成功 | ok/partial | — | xbrl_v2 |
| XBRL あり + validator 失敗 | quarantined | too_few_valid_segments 等 | xbrl_v2 |
| XBRL あり + facts なし | quarantined | no_records | xbrl_v2 |
| XBRL あり + 抽出エラー | quarantined | xbrl_extraction_error | xbrl_v2 |
| **XBRL なし** | **quarantined** | **no_xbrl_segment_source** | **xbrl_only_no_source** |

## A/B テストレポート

| ファイル | 規模 | 日付 | 概要 |
|---------|------|------|------|
| [v2_final_ab_102_20260315.txt](v2_final_ab_102_20260315.txt) | 102 | 2026-03-15 | XBRL-first + strict validator |
| [v2_source_aware_ab_504_20260315.txt](v2_source_aware_ab_504_20260315.txt) | 504 | 2026-03-15 | source-aware routing |

## 運用方法

```bash
# v2 (デフォルト — XBRL only)
python tools/backfill_segments_tdnet.py

# v1 ロールバック
python tools/backfill_segments_tdnet.py --worker-version v1
```

## JSONL / Summary 監視項目

- `route_mode` breakdown: `xbrl_v2` / `xbrl_only_no_source`
- `selected_path` breakdown: `xbrl` / `none`
- `quarantine_reason_breakdown` (no_xbrl_segment_source / no_records / xbrl_extraction_error)
- `xbrl_resolved_count` / `xbrl_unresolved_count`
- XBRL resolved subset の ok 率

## Rescue / Reject

| ファイル | 内容 |
|---------|------|
| [v2_rescue_reject_tickers.json](v2_rescue_reject_tickers.json) | rescued 19, rejected 3 |

## Unknown Member Suffix 監視

1. `python /tmp/scan_suffixes.py` で定期スキャン
2. 新しい suffix が出現したら `xbrl_segment_extractor.py` の `_SEGMENT_MEMBER_RE` に追加検討
