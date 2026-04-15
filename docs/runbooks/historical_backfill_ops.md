# Historical Backfill — 運用メモ
# 基準版: 2026-03-19 Phase 5D 完了

## ステータス

| 項目 | 状態 |
|------|------|
| order historical backfill | ✅ 完了 (59件投入) |
| segment historical backfill | ✅ 完了 (既投入済み) |
| SQLite / Supabase sync | ✅ 完了 (319 rows) |
| COMPANYVIEW 反映 | ✅ 完了 |
| 残TODO | 1801 _extract_total_from_table 精度改善（別件） |

## コマンドリファレンス

### 手動 backfill (新規 filing 追加時)
```powershell
# dry-run
.\.venv\Scripts\python.exe backfill_historical_comparatives.py --order-only --dry-run

# 本番 (全件)
.\.venv\Scripts\python.exe backfill_historical_comparatives.py --order-only

# 件数制限付き
.\.venv\Scripts\python.exe backfill_historical_comparatives.py --order-only --limit 50

# セグメントのみ
.\.venv\Scripts\python.exe backfill_historical_comparatives.py --segment-only
```

### Supabase 同期
```powershell
# dry-run
.\.venv\Scripts\python.exe tools\sync_orders.py

# 本番
.\.venv\Scripts\python.exe tools\sync_orders.py --apply
```

## 安全設計

- **overwrite 禁止**: filter_skip_existing で既存値チェック → INSERT only
- **same-value guard**: orders_total == backlog_total の場合 historical skip
  - backlog_total == carryover_construction_total は建設業で正常 → 許可
- **source_system**: `tdnet_historical` で current と分離
- **audit log**: `data/backfill_audit_*.csv` に全操作記録

## Nightly / Realtime での取り扱い

### 現状
- historical backfill は **バッチ専用**（手動実行）
- nightly パイプライン (`TDNET_MainPipeline`) は current データのみ処理
- realtime パイプライン (`TDNET_Realtime`) は current データのみ処理

### 推奨運用
1. **nightly に組み込まない** — backfill は過去比較データなので、
   新規 filing が追加された時点で手動 or 月次バッチで充分
2. **月次バッチ** — 月1回 `backfill_historical_comparatives.py --order-only` を実行
   → 新規 filing の historical が自動的に追加される
   → `sync_orders.py --apply` で Supabase 同期
3. **filter_skip_existing が冪等性を保証** — 何度実行しても安全
4. **同期も冪等** — sync_orders.py は既存チェック後 INSERT only
