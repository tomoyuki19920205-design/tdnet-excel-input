# 自社株買い抽出エンジン — 実データ検証サマリ

## 実行情報
| 項目 | 値 |
|:---|:---|
| 実行時刻 | 2026-03-11 10:52:21 JST |
| input_dir | data/docs |
| recursive | True |
| min_confidence | 0.6 |
| confidence_final | max(classification, extraction) |

## ファイル集計
| 指標 | 件数 |
|:---|---:|
| 対象ファイル数 | 10 |
| └ pdf | 10 |
| テキスト取得成功 | 10 |
| テキスト取得失敗 | 0 |

## 分類集計
| 指標 | 件数 |
|:---|---:|
| buyback_related | 0 |
| non_buyback | 10 |
| excluded | 0 |

## confidence 分布
| 区分 | 件数 |
|:---|---:|
| high (>= 0.6) | 0 |
| low (< 0.6) | 0 |

## review_bucket 別件数
| bucket | 件数 |
|:---|---:|
| non_buyback | 10 |

## 所見
- buyback 関連文書が検出されませんでした
