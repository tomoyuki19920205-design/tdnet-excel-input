-- Company Viewer qualitative news monitor v1 (Supabase/PostgreSQL)
CREATE TABLE IF NOT EXISTS canonical_news_events (
 event_id UUID PRIMARY KEY, schema_version TEXT NOT NULL CHECK (schema_version = 'company_news_v1'),
 ticker TEXT NOT NULL CHECK (ticker ~ '^(\d{4}|\d{3}[A-Z])$'), headline TEXT NOT NULL,
 source_type TEXT NOT NULL, source_name TEXT NOT NULL, source_url TEXT NOT NULL CHECK (source_url ~ '^https?://'),
 published_at TIMESTAMPTZ NOT NULL, first_seen_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, checked_at TIMESTAMPTZ NOT NULL,
 category TEXT NOT NULL CHECK (category = ANY (ARRAY['orders','backlog','pricing','demand','volume','product_mix','margin','raw_material','labor_cost','energy_cost','logistics','capacity','utilization','capex','new_factory','new_product','customer','supplier','competitor','industry','regulation','m_and_a','divestiture','reorganization','large_project','inventory','fx','shareholder_return','guidance','management_comment','other'])),
 subcategory TEXT, direction TEXT NOT NULL CHECK (direction IN ('positive','negative','mixed','neutral','unknown')),
 importance TEXT NOT NULL CHECK (importance IN ('high','medium','low')),
 earnings_relevance TEXT NOT NULL CHECK (earnings_relevance IN ('direct','likely','general','context','unknown')),
 summary TEXT NOT NULL, why_it_matters TEXT NOT NULL, evidence_excerpt TEXT,
 temporal_scope TEXT NOT NULL CHECK (temporal_scope IN ('current','quarter','fiscal_year','multi_year','ongoing','historical','unknown')),
 valid_from TIMESTAMPTZ, valid_until TIMESTAMPTZ, temporal_status TEXT NOT NULL CHECK (temporal_status IN ('current','ongoing','historical','expired','unknown')),
 tags JSONB NOT NULL DEFAULT '[]'::jsonb, task_run_id TEXT NOT NULL, collector_type TEXT NOT NULL,
 collector_version TEXT, analysis_version TEXT, dedupe_key TEXT NOT NULL UNIQUE, source_hash TEXT NOT NULL,
 raw_payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from)
);
COMMENT ON COLUMN canonical_news_events.direction IS 'Qualitative direction for company earnings; not a share-price prediction.';
CREATE TABLE IF NOT EXISTS canonical_news_scan_runs (
 scan_run_id TEXT PRIMARY KEY, ticker TEXT NOT NULL CHECK (ticker ~ '^(\d{4}|\d{3}[A-Z])$'), checked_at TIMESTAMPTZ NOT NULL,
 collector_type TEXT NOT NULL, task_id TEXT, status TEXT NOT NULL CHECK (status IN ('completed','failed')),
 items_found INTEGER NOT NULL DEFAULT 0 CHECK (items_found >= 0), sources_checked_count INTEGER CHECK (sources_checked_count >= 0),
 error_code TEXT, error_message TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_news_events_ticker_published ON canonical_news_events(ticker, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_published ON canonical_news_events(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_created ON canonical_news_events(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_importance ON canonical_news_events(importance);
CREATE INDEX IF NOT EXISTS ix_news_events_direction ON canonical_news_events(direction);
CREATE INDEX IF NOT EXISTS ix_news_events_category ON canonical_news_events(category);
CREATE INDEX IF NOT EXISTS ix_news_scan_ticker_checked ON canonical_news_scan_runs(ticker, checked_at DESC);

CREATE OR REPLACE VIEW api_latest_news_events AS
SELECT n.event_id, n.ticker, c.name_ja AS company_name, n.headline, n.published_at, n.checked_at, n.source_type, n.source_name, n.source_url,
       category, direction, importance, earnings_relevance, summary, why_it_matters,
       evidence_excerpt, temporal_status, valid_until, tags, n.created_at,
       CASE importance WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END AS importance_rank
FROM canonical_news_events n
LEFT JOIN companies c ON c.ticker_code = n.ticker;

CREATE OR REPLACE VIEW api_latest_news_scan_runs AS
SELECT DISTINCT ON (ticker) scan_run_id, ticker, checked_at, status, items_found, sources_checked_count
FROM canonical_news_scan_runs ORDER BY ticker, checked_at DESC;

ALTER TABLE canonical_news_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_news_scan_runs ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON api_latest_news_events, api_latest_news_scan_runs TO authenticated;
