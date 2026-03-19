-- ============================================================
-- schema.sql — XBRL ETL 用 SQLite スキーマ定義
-- ============================================================
-- value は円の整数 (JPY) に統一
-- 修正開示は上書き禁止で追加（v_latest_facts / v_latest_guidance で最新参照）
-- ============================================================

PRAGMA foreign_keys = ON;

-- =========================
-- 1) Master: companies
-- =========================
CREATE TABLE IF NOT EXISTS companies (
  company_id     INTEGER PRIMARY KEY,
  ticker_code    TEXT NOT NULL,              -- "3538" 等（文字列推奨）
  name_ja        TEXT,
  name_en        TEXT,
  exchange       TEXT,                       -- "TSE" 等
  industry       TEXT,
  edinet_code    TEXT,
  tdnet_code     TEXT,
  is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_ticker_code
ON companies(ticker_code);

-- =========================
-- 2) Disclosures: 開示イベント
-- =========================
CREATE TABLE IF NOT EXISTS disclosures (
  disclosure_id  INTEGER PRIMARY KEY,
  company_id     INTEGER NOT NULL,
  source         TEXT NOT NULL CHECK (source IN ('TDNET','EDINET','MANUAL','OTHER')),
  disclosed_at   TEXT NOT NULL,              -- ISO8601 "2026-02-26 13:00:00" 等
  title          TEXT NOT NULL,
  doc_type       TEXT NOT NULL CHECK (
                  doc_type IN ('TANSHIN','REVISION','PRESENTATION','QA','REPOST','OTHER')
                ),
  is_target      INTEGER NOT NULL DEFAULT 1 CHECK (is_target IN (0,1)),
  url            TEXT,
  sha256         TEXT,                       -- 原本同一性チェック用（任意）
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (company_id) REFERENCES companies(company_id)
);

CREATE INDEX IF NOT EXISTS ix_disclosures_company_time
ON disclosures(company_id, disclosed_at);

CREATE INDEX IF NOT EXISTS ix_disclosures_doc_type
ON disclosures(doc_type);

CREATE INDEX IF NOT EXISTS ix_disclosures_is_target
ON disclosures(is_target);

-- =========================
-- 3) Filing artifacts: ZIP/PDF/XBRL等の管理
-- =========================
CREATE TABLE IF NOT EXISTS filing_artifacts (
  artifact_id    INTEGER PRIMARY KEY,
  disclosure_id  INTEGER NOT NULL,
  artifact_type  TEXT NOT NULL CHECK (artifact_type IN ('ZIP','XBRL','IXBRL','PDF','HTML','OTHER')),
  filename       TEXT,
  mime_type      TEXT,
  byte_size      INTEGER,
  local_path     TEXT,                       -- 保存している場合
  url            TEXT,
  sha256         TEXT,
  extracted      INTEGER NOT NULL DEFAULT 0 CHECK (extracted IN (0,1)),
  extract_error  TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (disclosure_id) REFERENCES disclosures(disclosure_id)
);

CREATE INDEX IF NOT EXISTS ix_artifacts_disclosure
ON filing_artifacts(disclosure_id);

CREATE INDEX IF NOT EXISTS ix_artifacts_type
ON filing_artifacts(artifact_type);

-- =========================
-- 4) Periods: 会計期間ディメンション（会社ごと）
-- =========================
CREATE TABLE IF NOT EXISTS periods (
  period_id       INTEGER PRIMARY KEY,
  company_id      INTEGER NOT NULL,
  fiscal_year_end TEXT NOT NULL,             -- "2025-12-31"
  fiscal_year     INTEGER NOT NULL,          -- 2025
  quarter         INTEGER NOT NULL CHECK (quarter IN (1,2,3,4)),
  period_start    TEXT,                      -- "2025-01-01" 等（任意）
  period_end      TEXT,                      -- "2025-12-31" 等（任意）
  is_full_year    INTEGER NOT NULL DEFAULT 0 CHECK (is_full_year IN (0,1)),
  currency        TEXT NOT NULL DEFAULT 'JPY',
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (company_id) REFERENCES companies(company_id)
);

-- 会社×期末×Qで一意
CREATE UNIQUE INDEX IF NOT EXISTS ux_periods_company_fye_q
ON periods(company_id, fiscal_year_end, quarter);

CREATE INDEX IF NOT EXISTS ix_periods_company_year
ON periods(company_id, fiscal_year);

-- =========================
-- 5) Facts: 実績数値（縦持ち・上書きしない）
-- =========================
CREATE TABLE IF NOT EXISTS facts (
  fact_id        INTEGER PRIMARY KEY,
  company_id     INTEGER NOT NULL,
  period_id      INTEGER NOT NULL,
  disclosure_id  INTEGER NOT NULL,

  scope          TEXT NOT NULL CHECK (scope IN ('CONSOLIDATED','NON_CONSOLIDATED')),
  metric         TEXT NOT NULL,              -- 例: NET_SALES, GROSS_PROFIT, OP_INCOME, ORDINARY_INCOME

  value          INTEGER NOT NULL,           -- 推奨: 円で整数に統一（例: 4915000000）
  unit           TEXT NOT NULL DEFAULT 'JPY' CHECK (unit IN ('JPY','JPY_MILLION')),
  scale          INTEGER,                    -- XBRL scale等（任意）
  quality        TEXT NOT NULL CHECK (quality IN ('XBRL','IXBRL','PDF','MANUAL')),
  confidence     INTEGER CHECK (confidence BETWEEN 0 AND 100),
  notes          TEXT,

  created_at     TEXT NOT NULL DEFAULT (datetime('now')),

  FOREIGN KEY (company_id) REFERENCES companies(company_id),
  FOREIGN KEY (period_id) REFERENCES periods(period_id),
  FOREIGN KEY (disclosure_id) REFERENCES disclosures(disclosure_id)
);

CREATE INDEX IF NOT EXISTS ix_facts_company_period_metric_scope
ON facts(company_id, period_id, metric, scope);

CREATE INDEX IF NOT EXISTS ix_facts_disclosure
ON facts(disclosure_id);

-- 同一開示内で同じ指標を複数行入れたくないなら（必要に応じて）
CREATE UNIQUE INDEX IF NOT EXISTS ux_facts_unique_per_disclosure
ON facts(disclosure_id, period_id, metric, scope);

-- =========================
-- 6) Guidance: 会社予想（上方修正含む）
-- =========================
CREATE TABLE IF NOT EXISTS guidance (
  guidance_id    INTEGER PRIMARY KEY,
  company_id     INTEGER NOT NULL,
  period_id      INTEGER NOT NULL,
  disclosure_id  INTEGER NOT NULL,

  metric         TEXT NOT NULL,
  value          INTEGER,                    -- 単一値
  range_low      INTEGER,                    -- レンジ下限
  range_high     INTEGER,                    -- レンジ上限
  unit           TEXT NOT NULL DEFAULT 'JPY' CHECK (unit IN ('JPY','JPY_MILLION')),
  quality        TEXT NOT NULL CHECK (quality IN ('XBRL','IXBRL','PDF','MANUAL')),
  confidence     INTEGER CHECK (confidence BETWEEN 0 AND 100),
  notes          TEXT,

  created_at     TEXT NOT NULL DEFAULT (datetime('now')),

  FOREIGN KEY (company_id) REFERENCES companies(company_id),
  FOREIGN KEY (period_id) REFERENCES periods(period_id),
  FOREIGN KEY (disclosure_id) REFERENCES disclosures(disclosure_id)
);

CREATE INDEX IF NOT EXISTS ix_guidance_company_period_metric
ON guidance(company_id, period_id, metric);

CREATE INDEX IF NOT EXISTS ix_guidance_disclosure
ON guidance(disclosure_id);

-- =========================
-- 7) Revisions: 予想修正の差分（任意だが分析が速い）
-- =========================
CREATE TABLE IF NOT EXISTS revisions (
  revision_id    INTEGER PRIMARY KEY,
  company_id     INTEGER NOT NULL,
  period_id      INTEGER NOT NULL,
  disclosure_id  INTEGER NOT NULL,

  metric         TEXT NOT NULL,
  prev_value     INTEGER,
  new_value      INTEGER,
  unit           TEXT NOT NULL DEFAULT 'JPY' CHECK (unit IN ('JPY','JPY_MILLION')),
  reason         TEXT,

  created_at     TEXT NOT NULL DEFAULT (datetime('now')),

  FOREIGN KEY (company_id) REFERENCES companies(company_id),
  FOREIGN KEY (period_id) REFERENCES periods(period_id),
  FOREIGN KEY (disclosure_id) REFERENCES disclosures(disclosure_id)
);

CREATE INDEX IF NOT EXISTS ix_revisions_company_period_metric
ON revisions(company_id, period_id, metric);

-- =========================
-- 8) quarterly_memos: Z列メモ（上書き型）
-- =========================
CREATE TABLE IF NOT EXISTS quarterly_memos (
  memo_id        INTEGER PRIMARY KEY,
  company_id     INTEGER NOT NULL,
  period_id      INTEGER NOT NULL,
  memo_text      TEXT,
  updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (company_id) REFERENCES companies(company_id),
  FOREIGN KEY (period_id) REFERENCES periods(period_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_quarterly_memos_company_period
ON quarterly_memos(company_id, period_id);

-- =========================
-- 9) segment_facts: セグメント別数値（AA列〜 売上/利益ペア）
-- =========================
CREATE TABLE IF NOT EXISTS segment_facts (
  segment_fact_id INTEGER PRIMARY KEY,
  company_id      INTEGER NOT NULL,
  period_id       INTEGER NOT NULL,
  disclosure_id   INTEGER,              -- トレース用（任意）
  segment_name    TEXT NOT NULL,
  segment_order   INTEGER NOT NULL DEFAULT 0,
  sales           INTEGER,              -- 円整数
  profit          INTEGER,              -- 円整数
  unit            TEXT NOT NULL DEFAULT 'JPY',
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT,
  FOREIGN KEY (company_id) REFERENCES companies(company_id),
  FOREIGN KEY (period_id) REFERENCES periods(period_id),
  FOREIGN KEY (disclosure_id) REFERENCES disclosures(disclosure_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_facts_company_period_seg
ON segment_facts(company_id, period_id, segment_name);

CREATE INDEX IF NOT EXISTS ix_segment_facts_disclosure
ON segment_facts(disclosure_id);

-- =========================
-- 10) Views: 最新値参照（上書きせず最新を引く）
-- =========================

-- facts: (company, period, metric, scope)ごとの最新disclosed_atを採用
CREATE VIEW IF NOT EXISTS v_latest_facts AS
WITH ranked AS (
  SELECT
    f.*,
    d.disclosed_at,
    ROW_NUMBER() OVER (
      PARTITION BY f.company_id, f.period_id, f.metric, f.scope
      ORDER BY d.disclosed_at DESC, f.fact_id DESC
    ) AS rn
  FROM facts f
  JOIN disclosures d ON d.disclosure_id = f.disclosure_id
)
SELECT
  fact_id, company_id, period_id, disclosure_id,
  scope, metric, value, unit, scale, quality, confidence, notes,
  disclosed_at, created_at
FROM ranked
WHERE rn = 1;

-- guidance: (company, period, metric)ごとの最新disclosed_atを採用
CREATE VIEW IF NOT EXISTS v_latest_guidance AS
WITH ranked AS (
  SELECT
    g.*,
    d.disclosed_at,
    ROW_NUMBER() OVER (
      PARTITION BY g.company_id, g.period_id, g.metric
      ORDER BY d.disclosed_at DESC, g.guidance_id DESC
    ) AS rn
  FROM guidance g
  JOIN disclosures d ON d.disclosure_id = g.disclosure_id
)
SELECT
  guidance_id, company_id, period_id, disclosure_id,
  metric, value, range_low, range_high, unit, quality, confidence, notes,
  disclosed_at, created_at
FROM ranked
WHERE rn = 1;
