# TDnet Pipeline — Component Map

## ディレクトリ構造

```
tdnet-excel-input/
├── lib/
│   ├── pipeline/           ← Canonical 層のコアロジック
│   │   ├── canonical_sync.py    SQLite → Supabase 同期オーケストレータ
│   │   ├── canonical_writer.py  EAV 変換 + upsert (write_segments_canonical等)
│   │   ├── db.py                Supabase 接続/認証/REST操作
│   │   ├── source_priority.py   source → priority マッピング
│   │   ├── recency.py           recency_key 生成 + 勝者判定
│   │   ├── queue.py             処理キュー管理
│   │   └── logging_utils.py     パイプラインログ
│   └── backfill/           ← Filing 単位の処理エンジン
│       ├── worker.py            Filing 処理ワーカー (Phase 1/2-A/2-B)
│       ├── cache.py             tdnet_cache レイアウト管理
│       ├── state_store.py       state.db 読み書き
│       ├── phase2_runner.py     Phase 2 (XBRL-first) ランナー
│       ├── retry.py             リトライ + review_hint 分類
│       ├── batch_upsert.py      バッチ upsert
│       ├── estimator.py         処理時間推定
│       ├── metrics.py           Filing ベースメトリクス
│       ├── reporting.py         レポート生成
│       └── listing_provider.py  上場企業リスト
│
├── src/
│   ├── extractor.py         ← PL 抽出メイン (55KB, PDF+XBRL)
│   ├── segment/             ← セグメント抽出
│   │   ├── xbrl_segment_extractor.py  iXBRL からセグメント抽出
│   │   ├── models.py                  SegmentRawRow モデル
│   │   └── normalize.py               セグメント名正規化
│   ├── extraction/          ← 抽出サブモジュール
│   ├── normalization/       ← データ正規化
│   ├── fetcher.py           ← TDnet HTTP フェッチ
│   ├── downloader.py        ← PDF/ZIP ダウンロード
│   ├── models.py            ← 共通モデル
│   └── year_parser.py       ← 日本の会計年度パーサー
│
├── tools/                   ← CLIツール群 (60ファイル)
│   ├── tdnet_ingest.py          日次 ingest
│   ├── filings_process.py       Filing 一括処理
│   ├── pipeline_run.py          統合パイプライン
│   ├── sync_financials.py       Financials sync
│   ├── sync_segments.py         Segments sync + dual-write
│   ├── retry_quarantine_segments.py  Quarantine 再試行
│   ├── discord_alerts.py        Discord 通知
│   ├── excel_sync.py            Excel 同期
│   ├── backfill_*.py            各種 backfill
│   └── cleanup_*.py             データクリーンアップ
│
├── company-memo-app/        ← Viewer (Next.js 15)
│   ├── app/page.tsx             SPA メインページ
│   ├── lib/
│   │   ├── viewer-api.ts        Supabase → データ取得
│   │   ├── supabase.ts          Supabase クライアント
│   │   ├── memo-api.ts          メモ CRUD
│   │   └── kpi-api.ts           KPI CRUD
│   ├── components/
│   │   ├── FinancialsTable.tsx   PL テーブル (57KB)
│   │   ├── SegmentTable.tsx     セグメント表
│   │   ├── ForecastTable.tsx    業績予想
│   │   ├── MonthlyTable.tsx     月次売上
│   │   └── KpiTable.tsx         KPI
│   └── types/                   TypeScript 型定義
│
├── data/
│   ├── tdnet_cache/         ← Filing キャッシュ
│   ├── xbrl_archive/        ← XBRL ZIP アーカイブ
│   └── docs/                ← ダウンロード済み PDF
│
├── tests/                   ← 108 テストファイル
└── docs/                    ← 24 ドキュメント
```

## Supabase テーブル

| テーブル | 形式 | PK/UNIQUE | 用途 |
|---|---|---|---|
| canonical_financials | EAV | source_row_key | PL 正本 |
| canonical_segments | EAV | source_row_key | セグメント正本 |
| segment_canonical | wide | ticker+period+quarter+segment_name | セグメント (旧形式) |
| segment_raw | wide | — | セグメント生データ |
| api_latest_financials | VIEW | — | Viewer 用 PL |
| api_latest_segments | VIEW | — | Viewer 用セグメント (source priority 付き) |
| grid_memos | — | — | メモグリッド |
| kpi_definitions | — | — | KPI 定義 |
| kpi_values | — | — | KPI 値 |

## テスト (108ファイル)

主要カテゴリ:
- Backfill (cache, worker, phase2, resume, retry): 12件
- Canonical (sync, writer, priority, recency): 6件
- Segment detection: 12件
- Extraction (PL, XBRL, forecast): 8件
- Pipeline integration: 4件
- Buyback: 10件
- その他 (Excel, DB, migration, etc.): 56件
