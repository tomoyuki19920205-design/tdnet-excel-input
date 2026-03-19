# Buyback Scanner Score Tuning — Summary

- **実行時刻**: 2026-03-11 14:27:08 JST
- **candidates**: `artifacts/buyback_candidates/buyback_candidates.csv`
- **review**: `artifacts/buyback_review_candidates/review_buyback_results.csv`
- **rules**: `default`

## 基本統計

| 項目 | 件数 |
|:---|---:|
| candidate 行数 | 29 |
| join 成功 | 29 |
| join 失敗 | 0 |

## before/after priority 分布

| priority | old | new | delta |
|:---|---:|---:|---:|
| high | 1 | 0 | -1 |
| medium | 18 | 3 | -15 |
| low | 10 | 26 | +16 |

## before/after precision 指標

| 指標 | old | new |
|:---|---:|---:|
| high → HCE 率 | 0.0% (0/1) | 0% (0/0) |
| medium → buyback 率 | 5.6% (1/18) | 100.0% (3/3) |
| promoted → HCE | — | 0 |
| demoted → FP | — | 17 |

## 閾値比較

| 閾値 | old | new |
|:---|---:|---:|
| high | 6 | 7 |
| medium | 3 | 4 |

## keyword 調整候補 (上位20)

| keyword | total | HCE | non_bb | weight | suggest | reason |
|:---|---:|---:|---:|---:|:---|:---|
| 自己株式 | 24 | 0 | 24 | 1 | move_to_penalty | false positive rate 100% |
| 上限 | 3 | 0 | 2 | 1 | move_to_penalty | false positive rate 67% |
| 自己株式の取得 | 2 | 1 | 0 | 3 | keep | balanced |
| 自己株式の消却 | 1 | 0 | 0 | 3 | decrease | mostly classifier_only (100%) |

## mismatch focus: 1 件


## 所見

- high precision 変化なし: 0.0%
- 新ルールで 17 件の false positive が降格
- 調整推奨キーワード: 自己株式, 上限, 自己株式の消却
