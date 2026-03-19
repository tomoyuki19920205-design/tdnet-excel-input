-- ============================================================
-- 002_phase2_canonical_tables.sql
-- Phase 2: Canonical / Viewer 層 (Phase 2-A: canonical テーブル)
-- Supabase SQL Editor で実行
-- ============================================================

-- =========================
-- 1) canonical_financials — PL 正規化 long テーブル
-- =========================
CREATE TABLE IF NOT EXISTS canonical_financials (
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
ALTER TABLE canonical_financials
  ADD CONSTRAINT uq_canonical_financials_row_key UNIQUE (source_row_key);

-- 検索用インデックス
CREATE INDEX IF NOT EXISTS ix_cf_ticker_period
  ON canonical_financials(ticker, period, quarter);
CREATE INDEX IF NOT EXISTS ix_cf_recency
  ON canonical_financials(ticker, period, quarter, metric, recency_key DESC);

-- =========================
-- 2) canonical_segments — セグメント正規化 long テーブル
-- =========================
CREATE TABLE IF NOT EXISTS canonical_segments (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker              TEXT NOT NULL,
  period              TEXT NOT NULL,
  quarter             TEXT NOT NULL,
  segment_name        TEXT NOT NULL,
  segment_key         TEXT NOT NULL,
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

-- 重複防止
ALTER TABLE canonical_segments
  ADD CONSTRAINT uq_canonical_segments_row_key UNIQUE (source_row_key);

-- 検索用インデックス
CREATE INDEX IF NOT EXISTS ix_cs_ticker_period
  ON canonical_segments(ticker, period, quarter);
CREATE INDEX IF NOT EXISTS ix_cs_segment
  ON canonical_segments(ticker, period, quarter, segment_key);
CREATE INDEX IF NOT EXISTS ix_cs_recency
  ON canonical_segments(ticker, period, quarter, segment_key, metric, recency_key DESC);

-- =========================
-- 3) RLS — Phase 1 と同じ方針
-- =========================
-- canonical_financials
ALTER TABLE canonical_financials ENABLE ROW LEVEL SECURITY;
CREATE POLICY cf_anon_read ON canonical_financials
  FOR SELECT USING (true);
CREATE POLICY cf_service_write ON canonical_financials
  FOR ALL USING (auth.role() = 'service_role');

-- canonical_segments
ALTER TABLE canonical_segments ENABLE ROW LEVEL SECURITY;
CREATE POLICY cs_anon_read ON canonical_segments
  FOR SELECT USING (true);
CREATE POLICY cs_service_write ON canonical_segments
  FOR ALL USING (auth.role() = 'service_role');
