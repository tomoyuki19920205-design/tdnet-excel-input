# financials 整合性監査レポート

## 実行情報
| 項目 | 値 |
|:---|:---|
| 実行時刻 | 2026-03-11 09:57:30 JST |
| 対象テーブル | financials |
| SQLite DB | C:\Users\takuy\OneDrive\tdnet-excel-input\data\jquants.db |
| Supabase | public.financials |
| 対象 ticker | 全件 |
| 比較キー | `(ticker, period, quarter)` |
| 比較対象列 | sales, gross_profit, operating_profit |
| strict 判定対象 | missing / value_mismatch / source_mismatch / duplicate |

> **ticker 正規化ルール**: SQLite の local_code は5桁末尾0形式（例: 67500）で格納されていますが、
> Supabase の ticker は4桁形式（例: 6750）です。比較時は5桁末尾0を4桁に正規化して統一しています。

> **数値単位正規化**: SQLite は円単位、Supabase は百万円単位で保持されています。
> 比較前に SQLite 側の数値を ÷ 1,000,000 して百万円に変換しています。
> CSV の `sqlite_value_raw` は元の円単位、`sqlite_value_normalized` は百万円に変換後の値です。

> **source 比較スキップ**: SQLite 側に source 列がないため source 比較をスキップしています。
> source_mismatch は 0 として扱い、strict 判定にも含めません。

> **注意**: 今回の比較対象列は 3 列のみです（sales, gross_profit, operating_profit）。
> updated_at は参考指標として集計していますが、strict 判定や完全一致率には含めません。

## 件数サマリ
| 指標 | 件数 |
|:---|---:|
| SQLite 件数 (重複排除後) | 72,490 |
| Supabase 件数 | 99,085 |
| 全ユニーク key 数 | 99,098 |
| 両DB共通 key 数 | 72,477 |
| missing_in_supabase | 13 |
| missing_in_sqlite | 26,608 |
| value_mismatch 行 | 36,228 |
| source_mismatch | 0 |
| updated_at_mismatch | 72,477 |
| duplicate_in_sqlite | 11,678 |
| duplicate_in_supabase | 0 |

## 一致率
| 指標 | 値 | 計算式 |
|:---|---:|:---|
| key ベース一致率 | 73.14% | 両DBに存在する key / 全ユニーク key |
| 完全一致率 (value columns) | 50.01% | value列すべて一致する行 / 共通key数 |

### 列別一致率
| 列 | 一致セル数 | 比較対象セル数 | 一致率 |
|:---|---:|---:|---:|
| sales | 72,230 | 72,477 | 99.66% |
| gross_profit | 36,310 | 72,477 | 50.10% |
| operating_profit | 72,261 | 72,477 | 99.70% |

### NULL率比較
| 列 | SQLite NULL数 | SQLite NULL率 | Supabase NULL数 | Supabase NULL率 | 差 |
|:---|---:|---:|---:|---:|---:|
| sales | 929 | 1.3% | 769 | 1.1% | 160 |
| gross_profit | 7,558 | 10.4% | 7,413 | 10.2% | 145 |
| operating_profit | 2,878 | 4.0% | 2,723 | 3.8% | 155 |

### source 分布比較
| source | SQLite件数 | Supabase件数 |
|:---|---:|---:|

### updated_at 比較サマリ (参考指標)
| 状態 | 件数 |
|:---|---:|
| equal | 0 |
| sqlite_newer | 0 |
| supabase_newer | 0 |
| one_side_null | 72,477 |

### 列存在サマリ
- SQLite only: (なし)
- Supabase only: source, updated_at
- 共通列: gross_profit, operating_profit, period, quarter, sales, ticker
- 今回比較列: sales, gross_profit, operating_profit

### 差分上位 ticker
| ticker | mismatch件数 |
|:---|---:|
| 4494 | 60 |
| 2130 | 30 |
| 3727 | 28 |
| 3993 | 27 |
| 6736 | 25 |
| 9537 | 24 |
| 7044 | 24 |
| 6894 | 24 |
| 5284 | 24 |
| 7997 | 24 |

### 差分上位 period
| period | mismatch件数 |
|:---|---:|
| 2025-03-31 | 3506 |
| 2023-03-31 | 3472 |
| 2024-03-31 | 3468 |
| 2022-03-31 | 3413 |
| 2026-03-31 | 2596 |
| 2025-12-31 | 1197 |
| 2024-12-31 | 1189 |
| 2023-12-31 | 1129 |
| 2022-12-31 | 1046 |
| 2021-12-31 | 1007 |

## 所見
- sales の NULL 差が 160 件あります
- gross_profit の NULL 差が 145 件あります
- operating_profit の NULL 差が 155 件あります
- SQLite 側に source 列がないため source 比較はスキップされました
- updated_at 差分は 72,477 件（参考指標: 同期実行時刻差の可能性）
- Supabase に未反映の行が 13 件あります
- Supabase にのみ存在する行が 26,608 件あります（SQLite 側で削除された可能性）
- SQLite 生データに重複が 11,678 グループ あります

## 制約
- 今回は sales, gross_profit, operating_profit の 3 列のみ対象
- updated_at は参考指標（strict 判定や完全一致率に含まない）
- Supabase 側ページネーション取得のため、大量データ時に時間がかかる場合があります
