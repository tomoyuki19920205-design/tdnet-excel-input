# GT更新後 metricsサマリー: highconf_plus1

生成日時: 2026-04-05T12:50:58+09:00  
適用GT: `screening_sheet.highconf_plus1.csv`

## 1. 更新件数

- 更新対象GT: `screening_sheet.highconf_fixed.csv`
- 更新件数: **1 件**

## 2. 更新対象PDF

| PDF | before | after | 根拠 |
|-----|--------|-------|------|
| 140120260313581329.pdf | `no` | `yes` | manual_check_581228_581329.md (change_to_yes 確定) |

## 3. FP 前後比較

| 指標 | before | after | delta |
|------|--------|-------|-------|
| FP | 20 | 19 | -1 |
| TP | 41 | 42 | +1 |

## 4. precision 前後比較

| 指標 | before | after | delta |
|------|--------|-------|-------|
| precision | 0.6721 | 0.6885 | +0.0164 |
| F1 | 0.7387 | 0.7500 | +0.0113 |

## 5. FN / recall 前後比較

| 指標 | before | after | delta |
|------|--------|-------|-------|
| FN | 9 | 9 | +0 |
| recall | 0.8200 | 0.8235 | +0.0035 |

## 6. 最終結論（3行）

1. FPが 20→19 (-1) に減少し、precision が 0.6721→0.6885 に改善した。
2. FN・recallは変化なし（9件, recall=0.8235）。列ヘッダーガードによる副作用（新規FN）は発生していない。
3. highconf_plus1（GT+1件修正後）での最終ベースライン: TP=42, FP=19, FN=9, recall=0.8235, precision=0.6885, F1=0.7500。
