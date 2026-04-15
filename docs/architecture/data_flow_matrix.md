# データフローマトリクス

各 feature が「どの入力 → どの中間処理 → どのテーブル」に出力されるかの一覧。

## 凡例

- **SoT**: Source of Truth（その feature にとっての正本）
- **永続**: 長期保存
- **短期**: artifacts/ 等の一時出力
- **派生**: 正本から再生成可能

## Feature × Layer マトリクス

| Feature | Input | Raw Storage | Normalized | Serving | Search | SoT | Retention | Notes |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|
| **financials (PL)** | TDnet PDF/HTML, XBRL ZIP | `data/docs/`, `data/xbrl_archive/` | `decision_db.quarterly_results` | Supabase `financials` | — | `quarterly_results` | 永続 | field_sources でソース追跡 |
| **financials (J-Quants)** | J-Quants API | `jquants.db` | `jquants_financials_normalized` | Supabase `financials` (sync後) | — | `jquants_financials_normalized` | 永続 | field-level COALESCE merge |
| **segment financials** | TDnet PDF/HTML 表抽出 | `data/docs/` | `decision_db.segment_financials` | Supabase `segment_financials` | — | `segment_financials` | 永続 | header 正規化あり |
| **monthly data** | J-Quants or manual | — | `decision_db` (該当列) | Supabase (該当列) | — | `decision_db` | 永続 | 未本格実装 |
| **KPI data** | viewer 手入力 | — | — | Supabase `kpi_data` | — | Supabase | 永続 | viewer 直接管理 |
| **forecast revision** | TDnet diff 検出 | `data/docs/` | `decision_db.filing_diff_summaries` | 通知 / viewer 表示 | — | `filing_diff_summaries` | 永続 | AI 要約は派生 |
| **buyback events** | PDF 本文検索 + 抽出 | `data/docs/` | `data/decision_db.buyback_events` | (将来 Supabase) | — | `buyback_events` | 永続 | scanner→review→save |
| **company memos** | viewer 手入力 | — | `decision_db.company_memos` | Supabase `company_memos` | — | `company_memos` | 永続 | 双方向 sync |
| **quarterly notes** | TDnet 付帯情報 | `data/docs/` | `decision_db.quarterly_notes` | Excel (Z列) | — | `quarterly_notes` | 永続 | |
| **disclosure master** | TDnet 取得ログ | `state.db` | — | — | — | `processing_log` | 永続 | doc_id, 取得日時 |
| **discord alerts** | diff 検出結果 | — | — | Discord webhook | — | — | 派生 | 再生成可能 |
| **diff summary** | filing_diff_summaries | `data/docs/` | `filing_diff_summaries` | Discord / viewer | — | `filing_diff_summaries` | 永続 | |
| **document search** | PDF/HTML 本文 | `data/docs/` | — | — | (将来) Search DB | Raw files | — | 未実装 |
| **quarantine** | 抽出失敗行 | — | `quarantine.db` | — | — | `quarantine.db` | 中期 | review 後に解消/退避 |
| **buyback scan 中間** | scanner 候補 CSV | — | artifacts/ 一時 | — | — | — | 短期 | 再生成可能 |
| **Excel export** | financials + segments | — | — | `data/data.xlsx` 等 | — | — | 派生 | 手修正しても逆流させない |

## パイプライン経路

```
TDnet API → fetch → data/docs/*.pdf
  → extract (PDF/HTML/XBRL) → quarterly_results + segment_financials
  → sync → Supabase (financials, segment_financials)
  → Excel export (data.xlsx)
  → Discord alert (diff あれば)

J-Quants API → fetch → jquants.db
  → sync_financials.py → Supabase financials
  → Excel export (data_jquants.xlsx)

Buyback pipeline:
  data/docs/*.pdf → scanner → candidate_manifest.csv
  → review → review_results.csv
  → export → save_candidates.csv
  → save_to_db → buyback_events
```
