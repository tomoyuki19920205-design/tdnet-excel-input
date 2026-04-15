# Buyback Review 運用仕様

## 概要

buyback 候補文書の「scanner → review → 保存候補切り出し」パイプラインの運用仕様。
medium 帯を人手 review 対象とし、high_confidence_extracted のみを保存候補にする運用。

## 1. Scanner 運用

- tuned rules: `configs/buyback_scanner_rules.json`
- 閾値: **high=7, medium=4**
- low は通常 review 対象外
- scanner は候補母集団を作る用途であり、最終判定は review が担う

```powershell
cd "C:\Users\takuy\OneDrive\tdnet-excel-input"
.\.venv\Scripts\python.exe tools/find_buyback_candidate_docs.py `
  --input-dir data/docs --recursive `
  --output-dir artifacts/buyback_candidates_tuned `
  --rules configs/buyback_scanner_rules.json
```

## 2. Review 対象

- **medium 帯のみ人手 review**
- high が存在する場合も medium 相当として一緒に review
- low は通常スキップ（定期 recall チェック用は別途）

```powershell
cd "C:\Users\takuy\OneDrive\tdnet-excel-input"
.\.venv\Scripts\python.exe tools/review_buyback_extraction.py `
  --manifest artifacts/buyback_candidates_tuned/candidate_manifest.csv `
  --only-manifest-files `
  --output-dir artifacts/buyback_review_candidates_tuned `
  --min-confidence 0.60
```

## 3. 保存候補 (save candidate) 条件

以下を**すべて**満たすもの:

| 条件 | 値 |
|:---|:---|
| review_bucket | `high_confidence_extracted` |
| is_buyback_related | `true` |
| confidence_final | ≥ 0.60 |
| extracted_fields_count | ≥ 1 |

将来的な厳格化候補:
- confidence_final ≥ 0.80
- event_type in (buyback_decision, buyback_result, buyback_status, treasury_cancel)
- missing_key_fields が空

## 4. 人手 Review キュー

以下のいずれか:

- `review_bucket = classifier_only` — 分類のみ、抽出失敗
- `review_bucket = low_confidence` — 抽出はできたが信頼度不足
- `review_bucket = extraction_failed` — テキスト抽出やパース失敗
- cancel 系で key fields 不足
- medium 帯だが保存条件に届かない

## 5. 保存しないもの

- `non_buyback` — buyback と判定されない
- `excluded` — 除外条件に該当
- `text_extract_failed` — テキスト抽出自体が失敗
- low priority のみで review 未実施

## 6. 保存候補切り出し

```powershell
cd "C:\Users\takuy\OneDrive\tdnet-excel-input"
# 標準条件
.\.venv\Scripts\python.exe tools/export_buyback_save_candidates.py `
  --review artifacts/buyback_review_candidates_tuned/review_buyback_results.csv `
  --output-dir artifacts/buyback_review_operation `
  --min-confidence 0.60 --min-core-fields 1 `
  --include-priority medium,high

# 厳格条件
.\.venv\Scripts\python.exe tools/export_buyback_save_candidates.py `
  --review artifacts/buyback_review_candidates_tuned/review_buyback_results.csv `
  --output-dir artifacts/buyback_review_operation_strict `
  --min-confidence 0.80 --min-core-fields 1 `
  --include-priority medium,high
```

### 出力ファイル

| ファイル | 内容 |
|:---|:---|
| `review_save_candidates.csv` | DB 保存候補一覧 |
| `review_manual_review_queue.csv` | 人手レビュー対象一覧 |
| `review_operation_summary.md` | 運用サマリ |

## 7. 運用フロー図

```
data/docs/*.pdf
    ↓
[1] find_buyback_candidate_docs.py (tuned rules)
    → candidate_manifest.csv (medium/high のみ review 対象)
    ↓
[2] review_buyback_extraction.py
    → review_buyback_results.csv
    ↓
[3] export_buyback_save_candidates.py
    → review_save_candidates.csv    (DB保存候補)
    → review_manual_review_queue.csv (人手review待ち)
    → review_operation_summary.md    (運用判断用)
    ↓
[4] 人手確認 → buyback_events DB 保存 (未実装)
```

## 8. 既知の制約

- review_bucket は proxy 指標（正確な precision は人手確認が必要）
- サンプル件数がまだ少ない
- cancel 系の recall は未検証
- OCR 非対応
- auto-save は未実装（保存候補リスト出力まで）
