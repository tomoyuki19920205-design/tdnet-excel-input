# アーキテクチャギャップ分析

現在の tdnet-pipeline と完成版6レイヤーアーキテクチャの差分。

## ギャップ一覧

| # | 完成版の要素 | 現在の状態 | ギャップ | 優先度 |
|---|---|---|---|---|
| 1 | 中間表現 (ParsedTable/Row/Cell) | `pdfplumber` の生テーブルを直接処理 | IR層が未挿入 | Medium |
| 2 | スコアベース候補検出 | KW完全一致 (`any(kw in text)`) | **一部導入済** (`scoring.py`) | High |
| 3 | ヘッダー正規化 | 正規化なし → 空白揺れに弱い | **導入済** (`header_analysis.py`) | ★Done |
| 4 | ヘッダーroleスコア | KWリスト一致のみ | **導入済** (`score_header_role()`) | ★Done |
| 5 | 複数行ヘッダー結合 | 先頭5行(→10行)の文字列結合のみ | 列位置ベース結合は未実装 | High |
| 6 | XBRLプロファイル | `_XBRL_TAG_MAP` 1dict | **導入済** (`xbrl_profiles.py`) | ★Done |
| 7 | Stage-aware Quarantine | reason のみ記録 | **導入済** (DB拡張+モデル) | ★Done |
| 8 | Provenance追跡 | なし | **モデル定義済** (統合は未) | Medium |
| 9 | Layer 1 (Source Loader) | `extractor.py` 内で直接処理 | 未分離 | Low |
| 10 | Layer 5 (Record Builder) | 各抽出関数が直接dict返却 | 未分離 | Low |
| 11 | 受注メトリクス強化 | KW一致ベース | スコアベース未導入 | Medium |
| 12 | OCR混在検出 | なし | 未実装 | Low |

## 今回導入済みの要素

### ★ 新規モジュール (5ファイル)

| ファイル | Layer | 責務 |
|---|---|---|
| `src/analysis/scoring.py` | L3/L4 | スコアリング基盤 |
| `src/analysis/header_analysis.py` | L4 | ヘッダー正規化・role判定 |
| `src/analysis/candidate_models.py` | L2-L5 | 中間表現・候補モデル・Provenance |
| `src/extractors/xbrl_profiles.py` | L2/L4 | 業態別タグプロファイル |
| `src/pipeline/quarantine_models.py` | L6 | Stage-aware Quarantine |

### ★ 既存コード変更 (2ファイル)

| ファイル | 変更内容 |
|---|---|
| `extractor.py` | `normalize_header` import, KW拡張, ヘッダー探索10行化+正規化マッチ |
| `migration_db.py` | quarantine テーブルに `failed_stage`/`review_hint` カラム追加 |

## 推奨改善順

1. **Sprint 2-1**: `_detect_column_positions` をスコアベースに全面移行
2. **Sprint 2-2**: `_parse_xbrl_content` に `xbrl_profiles.resolve_facts()` を統合
3. **Sprint 2-3**: quarantine 呼び出し箇所に `failed_stage` + `review_hint` を付与
4. **Sprint 2-4**: `ParsedTable` IRを介した表処理の標準化
5. **Sprint 3-1**: 列位置ベースの複数行ヘッダー結合
6. **Sprint 3-2**: Provenance を SegmentRecord に付加
