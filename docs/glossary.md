# 用語集 (Glossary)

本プロジェクトで使用する主要用語の定義。

## データ階層

| 用語 | 定義 |
|:---|:---|
| **Raw** | 外部から取得した元データ。PDF, XBRL ZIP, API 生レスポンス。再解析の証拠 |
| **Normalized** | Raw から抽出し、意味づけを行ったデータ。根拠つき。最も重要な層 |
| **Canonical** | Normalized の中で「確定採用」された値。最終的な正規化済みデータ |
| **Serving** | viewer / API / Excel が直接読む表示用データ。使いやすさ優先 |
| **Search** | 全文検索・embedding 等の検索専用データ。本体 DB とは分離 |
| **Source of Truth (SoT / 正本)** | 同じ意味のデータが複数ある場合に「正」とみなすもの |
| **派生物** | 正本から再生成可能なデータ。Excel export, Discord 通知, レポート等 |

## 財務用語

| 用語 | 定義 |
|:---|:---|
| **ticker** | 銘柄コード。4桁文字列 (`"2301"`) を標準とする |
| **local_code** | J-Quants の5桁コード (`"23010"`)。末尾の0を含む |
| **fiscal_year_end** | 会計年度末日 (`"2024-10-31"`)。日付文字列 |
| **quarter** | 四半期表現。`"1Q"`, `"2Q"`, `"3Q"`, `"FY"` (文字列) |
| **FY** | 通期 (Full Year)。累計の最終期を指す |
| **cumulative (累計)** | 期首からの積み上げ値。決算短信の標準形 |
| **discrete (単Q)** | その四半期単独の値。累計の差分で算出 |
| **consolidated (連結)** | 連結決算ベースの値。標準 |
| **non-consolidated (個別)** | 単体決算ベースの値 |
| **gross_profit (粗利)** | 売上総利益。sales - cost_of_sales |
| **operating_profit (営業利益)** | 営業利益。gross_profit - SGA |
| **ordinary_income (経常利益)** | 経常利益 |
| **net_income (純利益)** | 当期純利益 |

## 抽出・パイプライン用語

| 用語 | 定義 |
|:---|:---|
| **field_sources** | 各 field がどのソースから抽出されたかの記録 (`summary_xbrl`, `attachment_xbrl`, `pdf`, `html`) |
| **quarantine** | 抽出に失敗した行。人手 review 待ち |
| **COALESCE merge** | 同一 (ticker, period, quarter) グループ内で、各 field の最新非 NULL 値を採用するマージ方式 |
| **overwrite_risk** | 同一グループに非NULL行とNULL行が共存するケース。旧 row-level latest では NULL 上書きの原因 |
| **scanner** | PDF 本文キーワード検索で候補を粗選別するツール |
| **review_bucket** | review の結果分類。`high_confidence_extracted`, `classifier_only`, `low_confidence`, `not_buyback` |
| **save_candidates** | review 後に「保存してよい」と判定された行 |

## ソース種別

| 値 | 意味 |
|:---|:---|
| `summary_xbrl` | 決算短信サマリ XBRL から抽出 |
| `attachment_xbrl` | 添付 PL XBRL から抽出 |
| `pdf` | PDF テキスト / OCR から抽出 |
| `html` | HTML 表から抽出 |
| `manual` | 手動入力 |
| `j_quants` | J-Quants API から取得 |

## タイムスタンプ用語

| 列名 | 意味 |
|:---|:---|
| `disclosed_date` | 開示日 (filing 日) |
| `fetched_at` | API / TDnet からの取得日時 |
| `created_at` | レコード作成日時 (DB 内部管理) |
| `updated_at` | レコード更新日時 (DB 内部管理) |
| `parsed_at` | 抽出実行日時 (あれば) |
