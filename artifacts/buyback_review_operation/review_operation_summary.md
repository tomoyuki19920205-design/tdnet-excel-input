# Buyback Review Operation — Summary

- **実行時刻**: 2026-03-11 15:35:23 JST
- **review results**: `c:\Users\takuy\OneDrive\tdnet-excel-input\artifacts\buyback_review_candidates_tuned\review_buyback_results.csv`

## パラメータ

| パラメータ | 値 |
|:---|:---|
| min_confidence | 0.6 |
| min_extracted_fields | 1 |
| include_priorities | all |

## 集計

| 項目 | 件数 |
|:---|---:|
| 入力 review 行数 | 3 |
| medium/high 対象 | 3 |
| **save candidates** | **1** |
| **manual review queue** | **2** |
| skipped (low/non-target) | 0 |
| non_buyback | 0 |
| excluded | 0 |
| save candidate rate | 33.3% |
| manual review rate | 66.7% |

## save candidate event_type 分布

| event_type | 件数 |
|:---|---:|
| buyback_decision | 1 |

## manual review 主因

| review_reason | 件数 |
|:---|---:|
| classifier_only | 2 |

## 所見

- medium/high 帯 3 件のうち **1 件** が保存候補 (33.3%)
- manual review queue: 2 件 (主因: classifier_only)
- 現段階では auto-save ではなく human-in-the-loop が妥当
