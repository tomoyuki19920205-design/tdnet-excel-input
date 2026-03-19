# 自社株買い抽出エンジン — 実データ検証サマリ

## 実行情報
| 項目 | 値 |
|:---|:---|
| 実行時刻 | 2026-03-11 10:49:21 JST |
| input_dir | data/docs |
| recursive | True |
| min_confidence | 0.6 |
| confidence_final | max(classification, extraction) |

## ファイル集計
| 指標 | 件数 |
|:---|---:|
| 対象ファイル数 | 50 |
| └ pdf | 50 |
| テキスト取得成功 | 49 |
| テキスト取得失敗 | 1 |

## 分類集計
| 指標 | 件数 |
|:---|---:|
| buyback_related | 2 |
| non_buyback | 48 |
| excluded | 0 |

## event_type 別件数
| event_type | 件数 |
|:---|---:|
| treasury_cancel | 1 |

## confidence 分布
| 区分 | 件数 |
|:---|---:|
| high (>= 0.6) | 1 |
| low (< 0.6) | 1 |

## review_bucket 別件数
| bucket | 件数 |
|:---|---:|
| non_buyback | 47 |
| classifier_only | 2 |
| text_extract_failed | 1 |

## 主要抽出項目の抽出率
| フィールド | 抽出件数 | 抽出率 |
|:---|---:|---:|
| shares_limit | 0 | 0.0% |
| shares_acquired | 0 | 0.0% |
| shares_cancelled | 0 | 0.0% |
| amount_limit_million_yen | 0 | 0.0% |
| amount_acquired_million_yen | 0 | 0.0% |
| ratio_to_outstanding | 0 | 0.0% |
| start_date | 0 | 0.0% |
| end_date | 0 | 0.0% |
| cancel_date | 0 | 0.0% |
| acquisition_method | 0 | 0.0% |
| board_resolution_date | 0 | 0.0% |
| status_period_label | 0 | 0.0% |

## 頻出 missing_key_fields
| フィールド | 欠損件数 |
|:---|---:|
| shares_cancelled | 1 |
| cancel_date | 1 |

## extraction_failures
合計 1 件のエラーが発生しました。
| stage | 件数 |
|:---|---:|
| load_text | 1 |

## 所見
- テキスト取得失敗が 1 件あります（PDF画像系の可能性）
- 低 confidence の buyback 文書が 1 件あります（手レビュー推奨）
- 最頻出の欠損フィールドは `shares_cancelled` (1 件)
