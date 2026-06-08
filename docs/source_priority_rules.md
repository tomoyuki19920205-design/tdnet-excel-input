# Source Priority Rules

## 定義

| source | priority | 説明 |
|:---|:---|:---|
| `summary_xbrl` | 1 | iXBRL サマリーファイルからの抽出 (最高優先) |
| `attachment_xbrl` | 2 | iXBRL 添付ファイル (Attachment/PL) からの抽出 |
| `html_table` | 3 | HTML テーブルからの抽出 |
| `pdf_table` | 4 | PDF テーブルからの抽出 |
| `legacy_excel` | 5 | 手動入力 Excel からの移行データ |
| `jquants` | 6 | J-Quants API からのデータ (一律, FY 特例なし) |

## エイリアス (実データ互換)

| source 値 | 解決先 priority | 用途 |
|:---|:---|:---|
| `xbrl` | 1 | segment source (summary_xbrl 相当) |
| `backfill_xbrl` | 2 | XBRL ZIP 後処理抽出 (attachment_xbrl 相当) |
| `html` | 3 | segment source (html_table 相当) |
| `pdf` | 4 | segment source (pdf_table 相当) |
| `tdnet` | 3 | financials source (html_table 相当) |
| `excel_legacy` | 5 | legacy_excel の別名 |

## 勝者判定順 (recency_key)

1. **source_priority** ASC (小さい = 高優先)
2. **correction_flag** DESC (True > False)
3. **disclosure_datetime** DESC (新しい > 古い)
4. **updated_at** DESC

recency_key は文字列ソートで最大値 = 勝者。

## 未知 source

定義されていない source は default priority = 99。

## jquants FY 特例について

Phase 2-A では jquants は一律 priority=6。
FY のみ jquants を優先する特例は、Phase 2-B の差分監査 (canonical vs legacy) の結果を見てから判断する。
