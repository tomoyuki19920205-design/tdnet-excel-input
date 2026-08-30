-- TSE 33-sector weekly reports (additive; company news schema remains unchanged)
CREATE TABLE IF NOT EXISTS canonical_sector_reports (
    id UUID PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'sector_weekly_v1'),
    report_type TEXT NOT NULL CHECK (report_type = 'sector_weekly'),
    sector_code INTEGER NOT NULL CHECK (sector_code BETWEEN 1 AND 33),
    sector_name TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    importance TEXT NOT NULL CHECK (importance IN ('A+', 'A', 'B', 'C')),
    direction TEXT NOT NULL CHECK (direction IN ('positive', 'negative', 'mixed', 'neutral')),
    summary_bullets JSONB NOT NULL,
    full_report_md TEXT NOT NULL,
    watchlist_companies JSONB NOT NULL DEFAULT '[]'::jsonb,
    next_week_watchpoints JSONB NOT NULL DEFAULT '[]'::jsonb,
    missed_candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    sources JSONB NOT NULL,
    run_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (period_end >= period_start),
    CHECK (jsonb_typeof(summary_bullets) = 'array'),
    CHECK (jsonb_typeof(watchlist_companies) = 'array'),
    CHECK (jsonb_typeof(next_week_watchpoints) = 'array'),
    CHECK (jsonb_typeof(missed_candidates) = 'array'),
    CHECK (jsonb_typeof(sources) = 'array')
);

CREATE TABLE IF NOT EXISTS canonical_sector_report_runs (
    run_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL CHECK (report_type = 'sector_weekly'),
    sector_code INTEGER NOT NULL CHECK (sector_code BETWEEN 1 AND 33),
    sector_name TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'failed', 'retry_pending')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_type TEXT,
    last_error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (period_end >= period_start)
);

CREATE INDEX IF NOT EXISTS ix_sector_reports_generated ON canonical_sector_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_sector_reports_period_code ON canonical_sector_reports(period_end DESC, sector_code);
CREATE INDEX IF NOT EXISTS ix_sector_report_runs_status ON canonical_sector_report_runs(status, period_end DESC);

CREATE OR REPLACE VIEW api_latest_news_stream AS
SELECT
    n.event_id AS stream_id,
    'company_news'::text AS report_type,
    n.headline AS title,
    n.created_at AS sort_at,
    n.published_at,
    n.checked_at,
    n.ticker,
    c.name_ja AS company_name,
    NULL::integer AS sector_code,
    NULL::text AS sector_name,
    n.category,
    n.direction,
    n.importance,
    CASE n.importance WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END AS importance_rank,
    n.earnings_relevance,
    n.summary,
    NULL::jsonb AS summary_bullets,
    n.why_it_matters,
    n.evidence_excerpt,
    n.temporal_status,
    n.valid_until,
    n.tags,
    n.source_type,
    n.source_name,
    n.source_url,
    NULL::timestamptz AS period_start,
    NULL::timestamptz AS period_end,
    NULL::text AS full_report_md,
    NULL::jsonb AS watchlist_companies,
    NULL::jsonb AS next_week_watchpoints,
    NULL::jsonb AS missed_candidates,
    NULL::jsonb AS sources,
    n.created_at
FROM canonical_news_events n
LEFT JOIN companies c ON c.ticker_code = n.ticker
UNION ALL
SELECT
    r.id AS stream_id,
    r.report_type,
    ('【東証33業種週次】' || r.sector_name)::text AS title,
    r.generated_at AS sort_at,
    r.generated_at AS published_at,
    r.generated_at AS checked_at,
    NULL::text AS ticker,
    NULL::text AS company_name,
    r.sector_code,
    r.sector_name,
    'sector_report'::text AS category,
    r.direction,
    r.importance,
    CASE r.importance WHEN 'A+' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 ELSE 4 END AS importance_rank,
    NULL::text AS earnings_relevance,
    NULL::text AS summary,
    r.summary_bullets,
    NULL::text AS why_it_matters,
    NULL::text AS evidence_excerpt,
    NULL::text AS temporal_status,
    NULL::timestamptz AS valid_until,
    '[]'::jsonb AS tags,
    NULL::text AS source_type,
    NULL::text AS source_name,
    NULL::text AS source_url,
    r.period_start,
    r.period_end,
    r.full_report_md,
    r.watchlist_companies,
    r.next_week_watchpoints,
    r.missed_candidates,
    r.sources,
    r.created_at
FROM canonical_sector_reports r;

ALTER TABLE canonical_sector_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_sector_report_runs ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON api_latest_news_stream TO authenticated;
