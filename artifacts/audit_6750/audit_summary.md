# financials 整合性監査レポート

## 実行情報
| 項目 | 値 |
|:---|:---|
| 実行時刻 | 2026-03-11 09:46:10 JST |
| 対象テーブル | financials |
| SQLite DB | C:\Users\takuy\OneDrive\tdnet-excel-input\data\jquants.db |
| Supabase | public.financials |
| 対象 ticker | 6750 |
| 比較キー | `(ticker, period, quarter)` |
| 比較対象列 | sales, gross_profit, operating_profit |
| strict 判定対象 | missing / value_mismatch / source_mismatch / duplicate |

> **ticker 正規化ルール**: SQLite の local_code は5桁末尾0形式（例: 67500）で格納されていますが、
> Supabase の ticker は4桁形式（例: 6750）です。比較時は5桁末尾0を4桁に正規化して統一しています。

> **注意**: 今回の比較対象列は 3 列のみです（sales, gross_profit, operating_profit）。
> updated_at は参考指標として集計していますが、strict 判定や完全一致率には含めません。

## 件数サマリ
| 指標 | 件数 |
|:---|---:|
| SQLite 件数 (重複排除後) | 20 |
| Supabase 件数 | 22 |
| 全ユニーク key 数 | 22 |
| 両DB共通 key 数 | 20 |
| missing_in_supabase | 0 |
| missing_in_sqlite | 2 |
| value_mismatch 行 | 20 |
| source_mismatch | 20 |
| updated_at_mismatch | 20 |
| duplicate_in_sqlite | 3 |
| duplicate_in_supabase | 0 |

## 一致率
| 指標 | 値 | 計算式 |
|:---|---:|:---|
| key ベース一致率 | 90.91% | 両DBに存在する key / 全ユニーク key |
| 完全一致率 (value columns) | 0.00% | value列すべて一致する行 / 共通key数 |

### 列別一致率
| 列 | 一致セル数 | 比較対象セル数 | 一致率 |
|:---|---:|---:|---:|
| sales | 0 | 20 | 0.00% |
| gross_profit | 0 | 20 | 0.00% |
| operating_profit | 0 | 20 | 0.00% |

### NULL率比較
| 列 | SQLite NULL数 | SQLite NULL率 | Supabase NULL数 | Supabase NULL率 | 差 |
|:---|---:|---:|---:|---:|---:|
| sales | 0 | 0.0% | 0 | 0.0% | 0 |
| gross_profit | 0 | 0.0% | 0 | 0.0% | 0 |
| operating_profit | 0 | 0.0% | 0 | 0.0% | 0 |

### source 分布比較
| source | SQLite件数 | Supabase件数 |
|:---|---:|---:|
| (empty) | 20 | 0 |
| jquants | 0 | 20 |

### updated_at 比較サマリ (参考指標)
| 状態 | 件数 |
|:---|---:|
| equal | 0 |
| sqlite_newer | 0 |
| supabase_newer | 0 |
| one_side_null | 20 |

### 列存在サマリ
- SQLite only: (なし)
- Supabase only: source, updated_at
- 共通列: gross_profit, operating_profit, period, quarter, sales, ticker
- 今回比較列: sales, gross_profit, operating_profit

### 差分上位 ticker
| ticker | mismatch件数 |
|:---|---:|
| 6750 | 60 |

### 差分上位 period
| period | mismatch件数 |
|:---|---:|
| 2025-03-31 | 12 |
| 2022-03-31 | 12 |
| 2024-03-31 | 12 |
| 2023-03-31 | 12 |
| 2026-03-31 | 9 |
| 2021-03-31 | 3 |

## 所見
- source 不一致が 20 件あります
- updated_at 差分は 20 件（参考指標: 同期実行時刻差の可能性）
- Supabase にのみ存在する行が 2 件あります（SQLite 側で削除された可能性）
- SQLite 生データに重複が 3 グループ あります

## 制約
- 今回は sales, gross_profit, operating_profit の 3 列のみ対象
- updated_at は参考指標（strict 判定や完全一致率に含まない）
- Supabase 側ページネーション取得のため、大量データ時に時間がかかる場合があります
