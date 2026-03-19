# Buyback Scanner Score Tuning — Summary

- **実行時刻**: 2026-03-11 14:59:35 JST
- **candidates**: `c:\Users\takuy\OneDrive\tdnet-excel-input\artifacts\buyback_candidates_tuned\buyback_candidates.csv`
- **review**: `c:\Users\takuy\OneDrive\tdnet-excel-input\artifacts\buyback_review_candidates_tuned\review_buyback_results.csv`
- **rules**: `c:\Users\takuy\OneDrive\tdnet-excel-input\configs\buyback_scanner_rules.json`

## 基本統計

| 項目 | 件数 |
|:---|---:|
| candidate 行数 | 3 |
| join 成功 | 3 |
| join 失敗 | 0 |

## before/after priority 分布

| priority | old | new | delta |
|:---|---:|---:|---:|
| high | 0 | 0 | +0 |
| medium | 0 | 3 | +3 |
| low | 3 | 0 | -3 |

## before/after precision 指標

| 指標 | old | new |
|:---|---:|---:|
| high → HCE 率 | 0% (0/0) | 0% (0/0) |
| medium → buyback 率 | 0% (0/0) | 100.0% (3/3) |
| promoted → HCE | — | 1 |
| demoted → FP | — | 0 |

## 閾値比較

| 閾値 | old | new |
|:---|---:|---:|
| high | 6 | 7 |
| medium | 3 | 4 |

## keyword 調整候補 (上位20)

| keyword | total | HCE | non_bb | weight | suggest | reason |
|:---|---:|---:|---:|---:|:---|:---|
| 自己株式の取得 | 2 | 1 | 0 | 3 | keep | balanced |
| 自己株式の消却 | 1 | 0 | 0 | 2 | decrease | mostly classifier_only (100%) |

## mismatch focus: 1 件


## 所見

- high precision 変化なし: 0%
- 新ルールで 1 件の真候補が上位に昇格
- 調整推奨キーワード: 自己株式の消却
