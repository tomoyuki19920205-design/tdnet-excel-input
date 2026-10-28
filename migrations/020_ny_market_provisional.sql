-- NY-only provisional state. Existing complete rows retain their contract.
ALTER TABLE canonical_ny_market_reports
    ADD COLUMN IF NOT EXISTS quality_status TEXT NOT NULL DEFAULT 'complete',
    ADD COLUMN IF NOT EXISTS unresolved_items JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE canonical_ny_market_reports
    ADD CONSTRAINT canonical_ny_market_quality_status_check CHECK (quality_status IN ('complete', 'provisional')),
    ADD CONSTRAINT canonical_ny_market_unresolved_check CHECK (
        jsonb_typeof(unresolved_items) = 'array' AND
        ((quality_status = 'complete' AND jsonb_array_length(unresolved_items) = 0) OR
         (quality_status = 'provisional' AND jsonb_array_length(unresolved_items) > 0))
    );
ALTER TABLE canonical_ny_market_reports
    DROP CONSTRAINT canonical_ny_market_reports_summary_bullets_check,
    DROP CONSTRAINT canonical_ny_market_reports_sector_moves_check,
    DROP CONSTRAINT canonical_ny_market_reports_notable_gainers_check,
    DROP CONSTRAINT canonical_ny_market_reports_notable_losers_check,
    DROP CONSTRAINT canonical_ny_market_reports_top_gainers_20_check,
    DROP CONSTRAINT canonical_ny_market_reports_major_news_check;
ALTER TABLE canonical_ny_market_reports
    ADD CONSTRAINT canonical_ny_market_complete_summary_check CHECK (quality_status = 'provisional' OR jsonb_array_length(summary_bullets) BETWEEN 5 AND 8),
    ADD CONSTRAINT canonical_ny_market_complete_sectors_check CHECK (quality_status = 'provisional' OR jsonb_array_length(sector_moves) = 11),
    ADD CONSTRAINT canonical_ny_market_complete_gainers_check CHECK (quality_status = 'provisional' OR jsonb_array_length(notable_gainers) = 10),
    ADD CONSTRAINT canonical_ny_market_complete_losers_check CHECK (quality_status = 'provisional' OR jsonb_array_length(notable_losers) = 10),
    ADD CONSTRAINT canonical_ny_market_complete_top20_check CHECK (quality_status = 'provisional' OR jsonb_array_length(top_gainers_20) = 20),
    ADD CONSTRAINT canonical_ny_market_complete_news_check CHECK (quality_status = 'provisional' OR jsonb_array_length(major_news) = 10);
ALTER TABLE canonical_ny_market_report_runs
    DROP CONSTRAINT canonical_ny_market_report_runs_status_check;
ALTER TABLE canonical_ny_market_report_runs
    ADD CONSTRAINT canonical_ny_market_runs_status_check CHECK (status IN ('pending', 'running', 'provisional', 'success', 'failed', 'retry_pending'));

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
    NULL::jsonb AS commodities, NULL::text AS quality_status, NULL::jsonb AS unresolved_items
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
    NULL::jsonb AS commodities, NULL::text AS quality_status, NULL::jsonb AS unresolved_items
FROM canonical_sector_reports r
UNION ALL
SELECT
    m.id AS stream_id, m.report_type, (CASE WHEN m.quality_status = 'provisional' THEN '【暫定版・NY市場モーニング】' ELSE '【NY市場モーニング】' END || to_char(m.report_date_jst, 'YYYY/MM/DD'))::text AS title,
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
    m.earnings, m.after_hours_earnings, m.major_news, m.commodities,
    m.quality_status, m.unresolved_items
FROM canonical_ny_market_reports m;

NOTIFY pgrst, 'reload schema';
