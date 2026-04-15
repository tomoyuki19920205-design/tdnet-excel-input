# TDnet Pipeline — Pipeline Map

## パイプライン実行順

```
pipeline_run.py (統合ランナー)
├── Stage 1: Ingest (tdnet_ingest.py)
│   ├── TDnet RSS フィード取得
│   ├── PDF ダウンロード → data/tdnet_cache/{filing_id}/source.pdf
│   ├── XBRL ZIP ダウンロード → data/tdnet_cache/{filing_id}/xbrl.zip
│   └── metadata.json 生成
│
├── Stage 2: Process (filings_process.py)
│   ├── Phase 2-A: XBRL-first (worker.py → process_one_filing_xbrl_first)
│   │   ├── XBRL PL 抽出 (extractor.py)
│   │   ├── XBRL セグメント抽出 (xbrl_segment_extractor.py)
│   │   └── 失敗時 → needs_pdf → Phase 2-B
│   ├── Phase 2-B: PDF-only (worker.py → process_one_filing_pdf_only)
│   │   ├── PDF PL 抽出 (extractor.py)
│   │   └── PDF セグメント抽出 (segment_detection_v2.py)
│   └── Quarantine 判定 (retry.py → classify_review_hint)
│       ├── quarantine.json 保存
│       └── review_hint 分類
│
├── Stage 3: Canonical Sync
│   ├── sync_financials.py → canonical_financials
│   ├── sync_segments.py
│   │   ├── XBRL → segment_canonical + canonical_segments (dual-write)
│   │   └── SQLite (excel_legacy) → segment_canonical + canonical_segments
│   └── canonical_writer.py (EAV 変換 + upsert)
│
└── Stage 4: Notify (discord_alerts.py)
    └── Discord 通知
```

## Source Priority (勝者決定)

```
source_priority.py / recency.py

1. source_priority ASC (xbrl=1, tdnet=3, excel_legacy=5)
2. correction_flag DESC (訂正 > 通常)
3. disclosure_datetime DESC (新しい開示優先)
4. updated_at DESC
```

## Backfill 専用ツール

| ツール | 用途 |
|---|---|
| backfill_filings.py | 過去 filing の一括処理 |
| backfill_canonical_financials.py | SQLite → canonical_financials |
| backfill_canonical_segments.py | SQLite → canonical_segments |
| backfill_xbrl_to_canonical.py | segment_canonical(wide) → canonical_segments(EAV) |
| retry_quarantine_segments.py | quarantine 再試行 |
| reprocess_ticker.py | 特定 ticker 再処理 |

## Quarantine フロー

```
extraction 失敗
    ↓
quarantine.json に保存
    ↓
state.db に review_hint 記録
    ↓
retry_quarantine_segments.py で再試行
    ↓
成功 → canonical_segments へ投入
失敗 → still_quarantined
```
