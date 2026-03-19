-- XBRL ETL - PostgreSQL (Supabase) Schema
-- Converted from SQLite schema.sql
-- value: BIGINT (JPY integer)
-- v_latest_facts / v_latest_guidance for latest values

-- =========================
-- 1) Master: companies
-- =========================
CREATE TABLE IF NOT EXISTS companies (
  company_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker_code    TEXT NOT NULL,
  name_ja        TEXT,
  name_en        TEXT,
  exchange       TEXT,
  industry       TEXT,
  edinet_code    TEXT,
  tdnet_code     TEXT,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_ticker_code
ON companies(ticker_code);

-- =========================
-- 2) Disclosures
-- =========================
CREATE TABLE IF NOT EXISTS disclosures (
  disclosure_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id     BIGINT NOT NULL REFERENCES companies(company_id),
  source         TEXT NOT NULL CHECK (source IN ('TDNET','EDINET','MANUAL','OTHER')),
  disclosed_at   TIMESTAMPTZ NOT NULL,
  title          TEXT NOT NULL,
  doc_type       TEXT NOT NULL CHECK (
                  doc_type IN ('TANSHIN','REVISION','PRESENTATION','QA','REPOST','OTHER')
                ),
  is_target      BOOLEAN NOT NULL DEFAULT TRUE,
  url            TEXT,
  sha256         TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_disclosures_company_time
ON disclosures(company_id, disclosed_at);

CREATE INDEX IF NOT EXISTS ix_disclosures_doc_type
ON disclosures(doc_type);

CREATE INDEX IF NOT EXISTS ix_disclosures_is_target
ON disclosures(is_target);

-- =========================
-- 3) Filing artifacts
-- =========================
CREATE TABLE IF NOT EXISTS filing_artifacts (
  artifact_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  disclosure_id  BIGINT NOT NULL REFERENCES disclosures(disclosure_id),
  artifact_type  TEXT NOT NULL CHECK (artifact_type IN ('ZIP','XBRL','IXBRL','PDF','HTML','OTHER')),
  filename       TEXT,
  mime_type      TEXT,
  byte_size      BIGINT,
  local_path     TEXT,
  url            TEXT,
  sha256         TEXT,
  extracted      BOOLEAN NOT NULL DEFAULT FALSE,
  extract_error  TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_artifacts_disclosure
ON filing_artifacts(disclosure_id);

CREATE INDEX IF NOT EXISTS ix_artifacts_type
ON filing_artifacts(artifact_type);

-- =========================
-- 4) Periods
-- =========================
CREATE TABLE IF NOT EXISTS periods (
  period_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id      BIGINT NOT NULL REFERENCES companies(company_id),
  fiscal_year_end DATE NOT NULL,
  fiscal_year     INT NOT NULL,
  quarter         INT NOT NULL CHECK (quarter IN (1,2,3,4)),
  period_start    DATE,
  period_end      DATE,
  is_full_year    BOOLEAN NOT NULL DEFAULT FALSE,
  currency        TEXT NOT NULL DEFAULT 'JPY',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_periods_company_fye_q
ON periods(company_id, fiscal_year_end, quarter);

CREATE INDEX IF NOT EXISTS ix_periods_company_year
ON periods(company_id, fiscal_year);

-- =========================
-- 5) Facts
-- =========================
CREATE TABLE IF NOT EXISTS facts (
  fact_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id     BIGINT NOT NULL REFERENCES companies(company_id),
  period_id      BIGINT NOT NULL REFERENCES periods(period_id),
  disclosure_id  BIGINT NOT NULL REFERENCES disclosures(disclosure_id),

  scope          TEXT NOT NULL CHECK (scope IN ('CONSOLIDATED','NON_CONSOLIDATED')),
  metric         TEXT NOT NULL,

  value          BIGINT NOT NULL,
  unit           TEXT NOT NULL DEFAULT 'JPY' CHECK (unit IN ('JPY','JPY_MILLION')),
  scale          INT,
  quality        TEXT NOT NULL CHECK (quality IN ('XBRL','IXBRL','PDF','MANUAL')),
  confidence     INT CHECK (confidence BETWEEN 0 AND 100),
  notes          TEXT,

  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_facts_company_period_metric_scope
ON facts(company_id, period_id, metric, scope);

CREATE INDEX IF NOT EXISTS ix_facts_disclosure
ON facts(disclosure_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_facts_unique_per_disclosure
ON facts(disclosure_id, period_id, metric, scope);

-- =========================
-- 6) Guidance
-- =========================
CREATE TABLE IF NOT EXISTS guidance (
  guidance_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id     BIGINT NOT NULL REFERENCES companies(company_id),
  period_id      BIGINT NOT NULL REFERENCES periods(period_id),
  disclosure_id  BIGINT NOT NULL REFERENCES disclosures(disclosure_id),

  metric         TEXT NOT NULL,
  value          BIGINT,
  range_low      BIGINT,
  range_high     BIGINT,
  unit           TEXT NOT NULL DEFAULT 'JPY' CHECK (unit IN ('JPY','JPY_MILLION')),
  quality        TEXT NOT NULL CHECK (quality IN ('XBRL','IXBRL','PDF','MANUAL')),
  confidence     INT CHECK (confidence BETWEEN 0 AND 100),
  notes          TEXT,

  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_guidance_company_period_metric
ON guidance(company_id, period_id, metric);

CREATE INDEX IF NOT EXISTS ix_guidance_disclosure
ON guidance(disclosure_id);

-- =========================
-- 7) Revisions
-- =========================
CREATE TABLE IF NOT EXISTS revisions (
  revision_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id     BIGINT NOT NULL REFERENCES companies(company_id),
  period_id      BIGINT NOT NULL REFERENCES periods(period_id),
  disclosure_id  BIGINT NOT NULL REFERENCES disclosures(disclosure_id),

  metric         TEXT NOT NULL,
  prev_value     BIGINT,
  new_value      BIGINT,
  unit           TEXT NOT NULL DEFAULT 'JPY' CHECK (unit IN ('JPY','JPY_MILLION')),
  reason         TEXT,

  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_revisions_company_period_metric
ON revisions(company_id, period_id, metric);

-- =========================
-- 8) quarterly_memos
-- =========================
CREATE TABLE IF NOT EXISTS quarterly_memos (
  memo_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id     BIGINT NOT NULL REFERENCES companies(company_id),
  period_id      BIGINT NOT NULL REFERENCES periods(period_id),
  memo_text      TEXT,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_quarterly_memos_company_period
ON quarterly_memos(company_id, period_id);

-- =========================
-- 9) segment_facts
-- =========================
CREATE TABLE IF NOT EXISTS segment_facts (
  segment_fact_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id      BIGINT NOT NULL REFERENCES companies(company_id),
  period_id       BIGINT NOT NULL REFERENCES periods(period_id),
  disclosure_id   BIGINT REFERENCES disclosures(disclosure_id),
  segment_name    TEXT NOT NULL,
  segment_order   INT NOT NULL DEFAULT 0,
  sales           BIGINT,
  profit          BIGINT,
  unit            TEXT NOT NULL DEFAULT 'JPY',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_facts_company_period_seg
ON segment_facts(company_id, period_id, segment_name);

CREATE INDEX IF NOT EXISTS ix_segment_facts_disclosure
ON segment_facts(disclosure_id);

-- =========================
-- 10) Views
-- =========================

CREATE OR REPLACE VIEW v_latest_facts AS
WITH ranked AS (
  SELECT
    f.*,
    d.disclosed_at AS disc_disclosed_at,
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
  disc_disclosed_at AS disclosed_at, created_at
FROM ranked
WHERE rn = 1;

CREATE OR REPLACE VIEW v_latest_guidance AS
WITH ranked AS (
  SELECT
    g.*,
    d.disclosed_at AS disc_disclosed_at,
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
  disc_disclosed_at AS disclosed_at, created_at
FROM ranked
WHERE rn = 1;