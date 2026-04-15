-- ============================================================
-- 003_rebuild_canonical_financials.sql
-- canonical_financials_rebuild テーブル作成
-- Supabase SQL Editor で実行
-- ============================================================

-- =========================
-- canonical_financials_rebuild — canonical_financials と同一スキーマ
-- =========================
CREATE TABLE IF NOT EXISTS canonical_financials_rebuild (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker              TEXT NOT NULL,
  period              TEXT NOT NULL,
  quarter             TEXT NOT NULL,
  metric              TEXT NOT NULL,
  value               NUMERIC,
  unit                TEXT DEFAULT 'JPY',
  source              TEXT NOT NULL,
  source_priority     INT NOT NULL,
  filing_id           TEXT,
  source_row_key      TEXT NOT NULL,
  disclosure_datetime TIMESTAMPTZ,
  correction_flag     BOOLEAN DEFAULT FALSE,
  recency_key         TEXT,
  extracted_at        TIMESTAMPTZ DEFAULT NOW(),
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- 重複防止: source_row_key は deterministic に生成される
ALTER TABLE canonical_financials_rebuild
  ADD CONSTRAINT uq_cf_rebuild_row_key UNIQUE (source_row_key);

-- 検索用インデックス (canonical_financials と同一)
CREATE INDEX IF NOT EXISTS ix_cfr_ticker_period
  ON canonical_financials_rebuild(ticker, period, quarter);
CREATE INDEX IF NOT EXISTS ix_cfr_recency
  ON canonical_financials_rebuild(ticker, period, quarter, metric, recency_key DESC);

-- =========================
-- RLS — canonical_financials と同一方針
-- =========================
ALTER TABLE canonical_financials_rebuild ENABLE ROW LEVEL SECURITY;
CREATE POLICY cfr_anon_read ON canonical_financials_rebuild
  FOR SELECT USING (true);
CREATE POLICY cfr_service_write ON canonical_financials_rebuild
  FOR ALL USING (auth.role() = 'service_role');
