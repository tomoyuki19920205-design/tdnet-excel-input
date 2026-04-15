# TDnet Pipeline — System Overview

## アーキテクチャ全体図

```mermaid
graph TB
    subgraph Input["データ取得層"]
        TDNET["TDnet API"]
        TDNET --> INGEST["tdnet_ingest.py"]
    end

    subgraph Cache["キャッシュ層"]
        INGEST --> CACHE["data/tdnet_cache/{filing_id}/"]
        CACHE --> PDF["source.pdf"]
        CACHE --> XBRL["xbrl.zip"]
        CACHE --> META["metadata.json"]
    end

    subgraph Extraction["抽出層"]
        PDF --> EXTRACT_FIN["extract_financials()"]
        XBRL --> EXTRACT_FIN
        PDF --> EXTRACT_SEG["extract_segments()"]
        XBRL --> XBRL_SEG["xbrl_segment_extractor"]
        EXTRACT_FIN --> FIN_JSON["extract_financials_result.json"]
        EXTRACT_SEG --> SEG_JSON["extract_segments_result.json"]
    end

    subgraph Storage["ローカル DB"]
        FIN_JSON --> SQLITE["decision_db.db (SQLite)"]
        SEG_JSON --> SQLITE
        SQLITE --> STATE["state.db"]
    end

    subgraph Canonical["Canonical 層 (Supabase)"]
        SQLITE --> SYNC_FIN["sync_financials.py"]
        SQLITE --> SYNC_SEG["sync_segments.py"]
        SYNC_FIN --> CF["canonical_financials"]
        SYNC_SEG --> CS["canonical_segments (EAV)"]
        SYNC_SEG --> SC["segment_canonical (wide)"]
    end

    subgraph Views["VIEW 層"]
        CF --> ALF["api_latest_financials"]
        CS --> ALS["api_latest_segments"]
    end

    subgraph Viewer["Company Viewer (Next.js)"]
        ALF --> VIEWER["page.tsx"]
        ALS --> VIEWER
        VIEWER --> FIN_TABLE["FinancialsTable"]
        VIEWER --> SEG_TABLE["SegmentTable"]
        VIEWER --> FORECAST["ForecastTable"]
        VIEWER --> MONTHLY["MonthlyTable"]
        VIEWER --> KPI["KpiTable"]
    end
```

## コア技術スタック

| 層 | 技術 |
|---|---|
| 取得 | Python + requests + BeautifulSoup |
| 抽出 | pdfplumber + iXBRL parser |
| ローカルDB | SQLite (decision_db.db / state.db) |
| クラウドDB | Supabase (PostgreSQL) |
| Viewer | Next.js 15 + TypeScript + Supabase JS |
| 通知 | Discord Webhooks |
| スケジューラ | Windows Task Scheduler |

## パイプライン日次フロー

```mermaid
graph LR
    A["1. Ingest<br/>tdnet_ingest.py"] --> B["2. Process<br/>filings_process.py"]
    B --> C["3. Canonical Sync<br/>canonical_sync.py"]
    C --> D["4. Notify<br/>discord_alerts.py"]

    B --> B1["XBRL抽出"]
    B --> B2["PDF抽出"]
    B --> B3["セグメント抽出"]
    B1 --> B4["quarantine判定"]
    B2 --> B4
    B3 --> B4
```
