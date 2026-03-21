-- ============================================================
-- earnings_summaries テーブル DDL
-- ============================================================
-- SQLite 用。Supabase では型を適宜変更（TEXT→varchar, REAL→numeric等）
--
-- 一意制約: fingerprint（同一開示の重複保存を防止）
-- ============================================================

CREATE TABLE IF NOT EXISTS earnings_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    company_name TEXT,
    fiscal_year TEXT,           -- '2026-01-31' 形式 (fiscal_year_end)
    quarter TEXT,               -- '1Q','2Q','3Q','4Q' 形式
    title TEXT,
    disclosure_date TEXT,       -- '2026-03-13' 形式
    sales_value REAL,           -- 売上高（円）
    sales_yoy REAL,             -- 売上YOY（小数: 0.25 = +25%）
    op_value REAL,              -- 営業利益（円）
    op_yoy REAL,                -- 営業利益YOY（小数）
    segment_summary_json TEXT,  -- セグメント情報 JSON
    overall_reason_summary TEXT,-- 全社増減理由
    segment_reason_summary TEXT,-- セグメント別増減理由
    summary_short TEXT,         -- 短縮要約（1行: "売上 +31%, 営利 +139%"）
    summary_full TEXT,          -- 完全要約（Discord通知と同等）
    fingerprint TEXT NOT NULL UNIQUE,
    source_url TEXT,
    archive_path TEXT,
    notified_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_es_ticker ON earnings_summaries(ticker);
CREATE INDEX IF NOT EXISTS idx_es_fiscal ON earnings_summaries(fiscal_year, quarter);
CREATE INDEX IF NOT EXISTS idx_es_disclosure ON earnings_summaries(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_es_fingerprint ON earnings_summaries(fingerprint);
CREATE INDEX IF NOT EXISTS idx_es_notified ON earnings_summaries(notified_at);
