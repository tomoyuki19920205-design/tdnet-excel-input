# 抽出エンジン完成版アーキテクチャ

## 概要

tdnet-pipeline の PDF/HTML/XBRL 抽出エンジンを、精度改善を継続しやすい6レイヤー構成へ段階移行する設計。

### 設計思想

1. **ルールベース維持** — 決定的・説明可能・修正容易な構造を保つ
2. **多段処理** — 「1発判定」ではなく段階処理へ分解
3. **スコアベース** — KW一致だけでなく confidence score で候補を選択
4. **Provenance** — どのソース・ページ・テーブル・行・ルールで決まったか追跡可能
5. **Stage-aware Quarantine** — どの段階で失敗したか + 改善ヒントを記録

## 6レイヤー構成

```
┌─────────────────────────────────────────────────┐
│  Layer 1: Source Loader                         │
│  ZIP/PDF/HTML/XBRL読込 → SourceDocument         │
├─────────────────────────────────────────────────┤
│  Layer 2: Structural Parser                     │
│  PDF→tables, HTML→DOM, XBRL→facts               │
│  → ParsedDocument / ParsedTable / ParsedFact    │
├─────────────────────────────────────────────────┤
│  Layer 3: Candidate Detector                    │
│  セグメント表/受注表/PL候補を検出                 │
│  → CandidateTable + score                       │
├─────────────────────────────────────────────────┤
│  Layer 4: Semantic Interpreter (心臓部)          │
│  ヘッダー正規化・role判定・行解釈・スコアリング     │
│  → HeaderAnalysis / MetricCandidate             │
├─────────────────────────────────────────────────┤
│  Layer 5: Record Builder                        │
│  候補→DB保存用record変換・重複排除・統合           │
│  → FinancialRecord / SegmentRecord              │
├─────────────────────────────────────────────────┤
│  Layer 6: Persistence / Review                  │
│  SQLite保存・Supabase同期・Quarantine記録         │
└─────────────────────────────────────────────────┘
```

## 中間表現 (IR)

ソースごとの差異を吸収する共通IR:

| モデル | 定義場所 | 用途 |
|---|---|---|
| `ParsedTable` | `candidate_models.py` | PDF/HTML表の共通表現 |
| `ParsedRow` | `candidate_models.py` | 行 (セルのリスト) |
| `ParsedCell` | `candidate_models.py` | セル (text, rowspan, colspan) |
| `ParsedFact` | `candidate_models.py` | XBRL fact (concept, value, context) |
| `CandidateTable` | `candidate_models.py` | スコア付き表候補 |
| `MetricCandidate` | `candidate_models.py` | スコア付き数値候補 |
| `Provenance` | `candidate_models.py` | 出自追跡 |

## スコアリング設計

### セグメント表スコア

| 要素 | 加点 | 減点 |
|---|---|---|
| 「セグメント」「事業別」「報告セグメント」 | +0.3 | |
| 売上系ヘッダあり | +0.2 | |
| 利益系ヘッダあり | +0.2 | |
| 数値行3行以上 | +0.2 | |
| 目次行2行以上 | | -0.3 |

### ヘッダーロールスコア

`score_header_role(text)` → `{role: score}` dict:

| ロール | 代表キーワード |
|---|---|
| `sales` | 売上高, 売上収益, 営業収益, Revenue |
| `operating_profit` | 営業利益, 事業利益, Operating profit |
| `segment_profit` | セグメント利益, セグメント損益, 利益又は損失 |
| `ordinary_profit` | 経常利益, Ordinary income |
| `ratio` | 前年比, 構成比, 利益率, % |
| `assets` | 資産, 総資産, Segment assets |

### 行ロールスコア

| ロール | 判定基準 |
|---|---|
| `segment_name` | 2-30文字、スキップでないもの |
| `total` | 合計, 総計, 計, Total |
| `adjustment` | 調整額 |
| `skip` | 合計/調整/消去/全社/配賦不能 |

## XBRLプロファイル

5業態対応:

| プロファイル | 売上タグ例 | 利益タグ例 |
|---|---|---|
| `general` | NetSales, Revenue | OperatingIncome |
| `bank` | OrdinaryIncomeBNK | OperatingIncomeBNK |
| `securities` | OperatingRevenueSEC | OperatingIncomeSEC |
| `insurance` | OrdinaryIncomeINS | OperatingIncomeINS |
| `reit` | OperatingRevenuesREIT | OperatingIncomeREIT |

フロー: `detect_industry_profile(fact_names)` → プロファイル順探索 → `resolve_facts()`

## Stage-aware Quarantine

| ステージ | 説明 |
|---|---|
| `source_load` | ZIP/PDF読込失敗 |
| `structural_parse` | テキスト抽出/XML解析失敗 |
| `candidate_detect` | 表候補未検出 |
| `semantic_interpret` | ヘッダーrole判定失敗 |
| `record_build` | レコード構築失敗 |

追加フィールド: `failed_stage`, `review_hint` (人間向け改善ヒント)

## Sprint 2/3 ロードマップ

### Sprint 2 (構造移行)
- Layer 2: `extractor.py` のPDF解析を `ParsedTable` 出力に移行
- Layer 3: スコアベース候補検出でセグメント表判定を置き換え
- Layer 4: `header_analysis` を全面適用 (列位置ベースの結合)
- XBRL: `xbrl_profiles` を `_parse_xbrl_content` に統合

### Sprint 3 (高度化)
- 列位置ベースの複数行ヘッダー結合
- OCR混在ページの検出と処理分岐
- 受注メトリクスのスコアベース候補検出
- Quarantine review UI の導入
