CREATE TABLE IF NOT EXISTS canonical_ny_market_reports (
 id TEXT PRIMARY KEY, stable_key TEXT NOT NULL UNIQUE, schema_version TEXT NOT NULL, report_type TEXT NOT NULL,
 report_date_jst TEXT NOT NULL, market_session_date TEXT NOT NULL, market_status TEXT NOT NULL,
 generated_at TEXT NOT NULL, headline TEXT NOT NULL, summary_bullets TEXT NOT NULL, index_moves TEXT NOT NULL,
 sector_moves TEXT NOT NULL, notable_gainers TEXT NOT NULL, notable_losers TEXT NOT NULL,
 top_gainers_20 TEXT NOT NULL, earnings TEXT NOT NULL, after_hours_earnings TEXT NOT NULL,
 major_news TEXT NOT NULL, commodities TEXT NOT NULL, report_markdown TEXT NOT NULL, sources TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_ny_market_report_runs (
 run_id TEXT PRIMARY KEY, stable_key TEXT NOT NULL UNIQUE, report_date_jst TEXT NOT NULL, status TEXT NOT NULL,
 attempt INTEGER NOT NULL DEFAULT 0, started_at TEXT, completed_at TEXT, error_type TEXT, error_message TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ny_market_reports_generated ON canonical_ny_market_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_ny_market_runs_status ON canonical_ny_market_report_runs(status, report_date_jst DESC);
