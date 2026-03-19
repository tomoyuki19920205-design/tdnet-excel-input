# 自社株買い抽出エンジン — 実データ検証サマリ

## 実行情報
| 項目 | 値 |
|:---|:---|
| 実行時刻 | 2026-03-11 14:23:52 JST |
| input_dir | (manifest のみ) |
| recursive | False |
| min_confidence | 0.6 |
| manifest | `artifacts/buyback_candidates/candidate_manifest.csv` |
| manifest 行数 | 29 |
| manifest 解決成功 | 29 |
| manifest 解決失敗 | 0 |

## ファイル集計
| 指標 | 件数 |
|:---|---:|
| 対象ファイル数 | 29 |
| └ pdf | 29 |
| テキスト取得成功 | 29 |
| テキスト取得失敗 | 0 |

## 分類集計
| 指標 | 件数 |
|:---|---:|
| buyback_related | 3 |
| non_buyback | 26 |
| excluded | 0 |

## event_type 別件数
| event_type | 件数 |
|:---|---:|
| buyback_decision | 2 |

## confidence 分布
| 区分 | 件数 |
|:---|---:|
| high (>= 0.6) | 2 |
| low (< 0.6) | 1 |

## review_bucket 別件数
| bucket | 件数 |
|:---|---:|
| non_buyback | 26 |
| classifier_only | 2 |
| high_confidence_extracted | 1 |

## manifest 連携集計
## 主要抽出項目の抽出率
| フィールド | 抽出件数 | 抽出率 |
|:---|---:|---:|
| shares_limit | 1 | 50.0% |
| shares_acquired | 0 | 0.0% |
| shares_cancelled | 0 | 0.0% |
| amount_limit_million_yen | 1 | 50.0% |
| amount_acquired_million_yen | 0 | 0.0% |
| ratio_to_outstanding | 1 | 50.0% |
| start_date | 1 | 50.0% |
| end_date | 1 | 50.0% |
| cancel_date | 0 | 0.0% |
| acquisition_method | 1 | 50.0% |
| board_resolution_date | 1 | 50.0% |
| status_period_label | 0 | 0.0% |

## 頻出 missing_key_fields
| フィールド | 欠損件数 |
|:---|---:|
| shares_limit | 1 |
| amount_limit_million_yen | 1 |
| start_date | 1 |
| end_date | 1 |

## 所見
- manifest 由来の候補 29 件を review
- 低 confidence の buyback 文書が 1 件あります（手レビュー推奨）
- 最頻出の欠損フィールドは `shares_limit` (1 件)
