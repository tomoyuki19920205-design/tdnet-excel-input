-- Daily NY market reports (additive third NEWS content type).
CREATE TABLE IF NOT EXISTS canonical_ny_market_reports (
    id UUID PRIMARY KEY,
    stable_key TEXT NOT NULL UNIQUE CHECK (stable_key ~ '^ny_market_daily:[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    schema_version TEXT NOT NULL CHECK (schema_version = 'ny_market_daily_v1'),
    report_type TEXT NOT NULL CHECK (report_type = 'ny_market_daily'),
    report_date_jst DATE NOT NULL UNIQUE,
    market_session_date DATE NOT NULL,
    market_status TEXT NOT NULL CHECK (market_status IN ('open', 'holiday_or_weekend')),
    generated_at TIMESTAMPTZ NOT NULL,
    headline TEXT NOT NULL,
    summary_bullets JSONB NOT NULL,
    index_moves JSONB NOT NULL,
    sector_moves JSONB NOT NULL,
    notable_gainers JSONB NOT NULL,
    notable_losers JSONB NOT NULL,
    top_gainers_20 JSONB NOT NULL,
    earnings JSONB NOT NULL,
    after_hours_earnings JSONB NOT NULL,
    major_news JSONB NOT NULL,
    commodities JSONB NOT NULL,
    report_markdown TEXT NOT NULL,
    sources JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (market_session_date <= report_date_jst),
    CHECK (jsonb_array_length(summary_bullets) BETWEEN 5 AND 8),
    CHECK (jsonb_array_length(sector_moves) = 11),
    CHECK (jsonb_array_length(notable_gainers) = 10),
    CHECK (jsonb_array_length(notable_losers) = 10),
    CHECK (jsonb_array_length(top_gainers_20) = 20),
    CHECK (jsonb_array_length(major_news) = 10),
    CHECK (jsonb_typeof(sources) = 'array')
);

CREATE TABLE IF NOT EXISTS canonical_ny_market_report_runs (
    run_id UUID PRIMARY KEY,
    stable_key TEXT NOT NULL UNIQUE,
    report_date_jst DATE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'failed', 'retry_pending')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_type TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ny_market_reports_generated ON canonical_ny_market_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_ny_market_runs_status ON canonical_ny_market_report_runs(status, report_date_jst DESC);

CREATE OR REPLACE VIEW api_latest_news_stream AS
SELECT
    n.event_id AS stream_id, 'company_news'::text AS report_type, n.headline AS title,
    n.created_at AS sort_at, n.published_at, n.checked_at, n.ticker, c.name_ja AS company_name,
    NULL::integer AS sector_code, NULL::text AS sector_name, n.category, n.direction, n.importance,
    CASE n.importance WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END AS importance_rank,
    n.earnings_relevance, n.summary, NULL::jsonb AS summary_bullets, n.why_it_matters,
    n.evidence_excerpt, n.temporal_status, n.valid_until, n.tags, n.source_type, n.source_name,
    n.source_url, NULL::timestamptz AS period_start, NULL::timestamptz AS period_end,
    NULL::text AS full_report_md, NULL::jsonb AS watchlist_companies,
    NULL::jsonb AS next_week_watchpoints, NULL::jsonb AS missed_candidates, NULL::jsonb AS sources,
    n.created_at,
    NULL::date AS report_date_jst, NULL::date AS market_session_date, NULL::text AS market_status,
    NULL::text AS report_markdown, NULL::jsonb AS index_moves, NULL::jsonb AS sector_moves,
    NULL::jsonb AS notable_gainers, NULL::jsonb AS notable_losers, NULL::jsonb AS top_gainers_20,
    NULL::jsonb AS earnings, NULL::jsonb AS after_hours_earnings, NULL::jsonb AS major_news,
    NULL::jsonb AS commodities
FROM canonical_news_events n
LEFT JOIN companies c ON c.ticker_code = n.ticker
UNION ALL
SELECT
    r.id AS stream_id, r.report_type, ('【東証33業種週次】' || r.sector_name)::text AS title,
    r.generated_at AS sort_at, r.generated_at AS published_at, r.generated_at AS checked_at,
    NULL::text AS ticker, NULL::text AS company_name, r.sector_code, r.sector_name,
    'sector_report'::text AS category, r.direction, r.importance,
    CASE r.importance WHEN 'A+' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 ELSE 4 END AS importance_rank,
    NULL::text AS earnings_relevance, NULL::text AS summary, r.summary_bullets,
    NULL::text AS why_it_matters, NULL::text AS evidence_excerpt, NULL::text AS temporal_status,
    NULL::timestamptz AS valid_until, '[]'::jsonb AS tags, NULL::text AS source_type,
    NULL::text AS source_name, NULL::text AS source_url, r.period_start, r.period_end,
    r.full_report_md, r.watchlist_companies, r.next_week_watchpoints, r.missed_candidates,
    r.sources, r.created_at,
    NULL::date AS report_date_jst, NULL::date AS market_session_date, NULL::text AS market_status,
    NULL::text AS report_markdown, NULL::jsonb AS index_moves, NULL::jsonb AS sector_moves,
    NULL::jsonb AS notable_gainers, NULL::jsonb AS notable_losers, NULL::jsonb AS top_gainers_20,
    NULL::jsonb AS earnings, NULL::jsonb AS after_hours_earnings, NULL::jsonb AS major_news,
    NULL::jsonb AS commodities
FROM canonical_sector_reports r
UNION ALL
SELECT
    m.id AS stream_id, m.report_type, ('【NY市場モーニング】' || to_char(m.report_date_jst, 'YYYY/MM/DD'))::text AS title,
    m.generated_at AS sort_at, m.generated_at AS published_at, m.generated_at AS checked_at,
    NULL::text AS ticker, NULL::text AS company_name, NULL::integer AS sector_code, NULL::text AS sector_name,
    'ny_market_report'::text AS category, 'neutral'::text AS direction, 'A'::text AS importance,
    1 AS importance_rank, NULL::text AS earnings_relevance, NULL::text AS summary, m.summary_bullets,
    NULL::text AS why_it_matters, NULL::text AS evidence_excerpt, NULL::text AS temporal_status,
    NULL::timestamptz AS valid_until, '[]'::jsonb AS tags, NULL::text AS source_type,
    NULL::text AS source_name, NULL::text AS source_url, NULL::timestamptz AS period_start,
    NULL::timestamptz AS period_end, m.report_markdown AS full_report_md,
    NULL::jsonb AS watchlist_companies, NULL::jsonb AS next_week_watchpoints,
    NULL::jsonb AS missed_candidates, m.sources, m.created_at,
    m.report_date_jst, m.market_session_date, m.market_status, m.report_markdown,
    m.index_moves, m.sector_moves, m.notable_gainers, m.notable_losers, m.top_gainers_20,
    m.earnings, m.after_hours_earnings, m.major_news, m.commodities
FROM canonical_ny_market_reports m;

ALTER TABLE canonical_ny_market_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_ny_market_report_runs ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON api_latest_news_stream TO authenticated;
