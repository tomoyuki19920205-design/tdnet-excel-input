# Phase 2 Migration Plan

## Phase 2-A: Canonical テーブル + Dual-Write (完了)

### 目的
- financials / segment_canonical の暫定構造を整理し、canonical と viewer の責務を分離する
- source 優先順位、recency_key、訂正開示を吸収しやすい構造に移行する
- 既存テーブル (financials / segment_canonical) は壊さず、dual-write で canonical 層を導入する

### テーブル
- `canonical_financials`: PL 正規化 long テーブル (ticker, period, quarter, metric, value, source, ...)
- `canonical_segments`: セグメント正規化 long テーブル (ticker, period, quarter, segment_key, metric, ...)
- `source_row_key` UNIQUE 制約で重複防止 (deterministic 生成)

### Dual-Write 方針
- 既存テーブルへの write は**そのまま維持**
- 直後に canonical テーブルへも write (best-effort)
- canonical write 失敗時は warning ログのみ、既存処理に影響しない
- 3箇所で dual-write: sqlite_to_supabase.py (tdnet), sync_financials.py (jquants), sync_segments.py (excel_legacy)

### 意思決定事項
- jquants は一律 priority=6 (FY 特例なし)
- FY 特例の要否は Phase 2-B の差分監査結果で判断
- app 切替は別プロジェクトのため未実施

---

## Phase 2-B: Viewer テーブル + Rebuild (予定)
- viewer_financials_latest / history
- viewer_segments_latest / history
- canonical → viewer rebuild ロジック
- 差分監査スクリプト

## Phase 2-C: App 切替 (別プロジェクト)
- app の financials → viewer_financials_latest
- app の segment_canonical → viewer_segments_latest

## Phase 2-D: Snapshot + 縮退
- viewer_company_snapshot
- 旧テーブル縮退
