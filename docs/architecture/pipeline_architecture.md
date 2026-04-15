# TDnet Segment Pipeline — Architecture

## データフロー

```mermaid
graph TD
    A[TDnet 開示] --> B[filing ingestion]
    B --> C{ソース判定}
    C -->|PDF| D[PDF extraction]
    C -->|HTML| E[HTML extraction]
    C -->|XBRL/iXBRL| F[XBRL extraction]
    D --> G[segment detection v2]
    E --> G
    F --> G
    G --> H{success?}
    H -->|Yes| I[state DB: upserted]
    H -->|No| J[state DB: quarantined]
    J --> K[quarantine retry]
    K --> H
    I --> L[SQLite]
    I --> M[Supabase]
    L --> N[Company Viewer]
    M --> N
```

## コンポーネント一覧

| コンポーネント | ファイル | 役割 |
|---|---|---|
| **Ingestion** | `tools/filings_ingest.py` | TDnet 開示取得 |
| **Processing** | `tools/filings_process.py` | 抽出 + DB 保存 |
| **Segment Detection** | `src/analysis/segment_detection_v2.py` | PDF セグメント表検出 |
| **Header Analysis** | `src/analysis/header_analysis.py` | ヘッダー正規化 + role 判定 |
| **Column Analysis** | `src/analysis/column_analysis.py` | 列 role 分類 |
| **Row Analysis** | `src/analysis/row_analysis.py` | 行 role 分類 |
| **Table Scoring** | `src/analysis/table_scoring.py` | 表候補スコアリング |
| **State Store** | `lib/backfill/state_store.py` | SQLite state DB 管理 |
| **Quarantine Retry** | `tools/retry_quarantine_segments.py` | quarantined 再処理 |
| **Daily Runner** | `tools/pipeline_daily_run.py` | 日次自動実行 |
| **Summary Report** | `tools/pipeline_summary_report.py` | 集計レポート |
| **Pipeline Run** | `tools/pipeline_run.py` | メインエントリポイント |

## Segment Detection v2 処理フロー

```mermaid
graph TD
    A[PDF text extraction] --> B["Phase A: Page Scoring"]
    B --> C["Phase B: Table Scoring"]
    C --> D["Phase C: Header Reconstruction"]
    D --> E["Phase D: Column Role Classification"]
    E --> F["Phase E: Row Role Classification"]
    F --> G["Phase F: Record Assembly"]
    B -.->|fail| Q1["quarantine: no_segment_page_candidate"]
    C -.->|fail| Q2["quarantine: no_segment_table_candidate"]
    E -.->|fail| Q3["quarantine: no_sales_profit_columns"]
    F -.->|fail| Q4["quarantine: no_rows_extracted"]
```

## State DB スキーマ

```
filing_states
├── filing_id (PK)
├── ticker
├── status          -- upserted | quarantined | pending
├── review_hint     -- pdf_no_sales_profit_columns etc.
├── source_file
├── created_at
└── updated_at
```

## 改善フェーズ履歴

| Phase | 内容 | 効果 |
|---|---|---|
| 1 | 基本 PDF extraction | 初版 |
| 2 | Header normalization + synonym | 認識率向上 |
| 3 | Column role inference + 隣接昇格 | 列推定精度向上 |
| 4 | Page candidate scoring + KW拡張 | ページ検出改善 |
| 4b | Table candidate scoring + weak evidence | 4件 rescue |
| 5 | Multi-line header + synonym 4th + pair estimation | **88件 rescue** |
| 6 | 運用化 / パイプライン整備 | 本フェーズ |
