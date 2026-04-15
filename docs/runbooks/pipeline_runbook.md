# TDnet Segment Pipeline — 運用 Runbook

## 1. パイプライン概要

```
TDnet 開示 → filing ingestion → PDF/XBRL extraction → segment detection
→ state DB → quarantine retry → SQLite / Supabase → Company Viewer
```

### 成功率目標

| 指標 | 現在 |
|---|---|
| upserted | 2779 |
| quarantined | 146 |
| **success rate** | **95.0%** |

---

## 2. 日次運用

### 日次更新 (推奨)

```powershell
# 全自動 (ingest + process + quarantine retry + summary report)
.\.venv\Scripts\python.exe tools\pipeline_daily_run.py

# retry スキップ
.\.venv\Scripts\python.exe tools\pipeline_daily_run.py --skip-retry

# dry-run (DB更新なし)
.\.venv\Scripts\python.exe tools\pipeline_daily_run.py --dry-run

# 指定日数分取得
.\.venv\Scripts\python.exe tools\pipeline_daily_run.py --days 3
```

### 個別ステップ

```powershell
# ingest のみ
.\.venv\Scripts\python.exe tools\pipeline_run.py ingest

# process のみ
.\.venv\Scripts\python.exe tools\pipeline_run.py process

# rebuild
.\.venv\Scripts\python.exe tools\pipeline_run.py rebuild
```

---

## 3. バックフィル

```powershell
# 期間指定
.\.venv\Scripts\python.exe tools\backfill_filings.py --from 2023-01-01 --to 2025-12-31

# 3年一括
.\.venv\Scripts\python.exe tools\pipeline_run.py backfill --from 2023-04-01 --to 2026-03-31
```

---

## 4. Quarantine Retry

```powershell
# dry-run (結果確認のみ)
.\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --dry-run --limit 50

# apply (DB更新)
.\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --apply --limit 50

# 特定 hint のみ
.\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --dry-run --only-hint pdf_no_sales_profit_columns

# debug: table candidate 詳細
.\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --dry-run --debug-table-candidates --limit 10

# debug: column role 詳細
.\.venv\Scripts\python.exe tools\retry_quarantine_segments.py --dry-run --debug-column-roles --limit 10
```

---

## 5. 成果確認

### Summary Report

```powershell
.\.venv\Scripts\python.exe tools\pipeline_summary_report.py
```

### State DB 直接確認

```powershell
.\.venv\Scripts\python.exe -c "
from lib.backfill.state_store import BackfillStateStore
s = BackfillStateStore('data/backfill_state.db')
rows = s.conn.execute('SELECT status, COUNT(*) FROM filing_states GROUP BY status').fetchall()
for r in rows: print(f'  {r[0]:20s}: {r[1]}')
"
```

---

## 6. トラブルシューティング

### Failure Taxonomy

| review_hint | 原因 | 対処 |
|---|---|---|
| `pdf_no_segment_page_candidate` | セグメント表のあるページが検出できない | page scoring KW 拡張 |
| `pdf_no_segment_table_candidate` | ページ内で表が認識できない | table scoring 調整 |
| `pdf_no_sales_profit_columns` | 売上/利益列が認識できない | synonym 拡張、header 正規化改善 |
| `pdf_no_rows_extracted` | セグメント名行が検出できない | row label パターン追加 |
| `pdf_extraction_failed` | PDF テキスト抽出自体が失敗 | OCR 検討 |

### よくある問題

1. **cid:XXXX 文字化け**: PDF embedded font 問題 → OCR pipeline で対応
2. **ヘッダーband 誤認**: 表タイトルをヘッダーと認識 → header_band 検出改善
3. **単ページ PDF**: 決算短信1面のみ → セグメント情報なし (救済不可)

---

## 7. parse_quality

| 値 | 意味 | DB フィールド |
|---|---|---|
| `full` | sales + profit 両方抽出成功 | `segment_parse_quality = 'full'` |
| `partial_sales_only` | 売上のみ抽出成功 | `segment_parse_quality = 'partial_sales_only'` |

### Viewer での扱い

- `full`: 通常表示
- `partial_sales_only`: 利益列は空欄、「売上のみ」インジケータ表示
