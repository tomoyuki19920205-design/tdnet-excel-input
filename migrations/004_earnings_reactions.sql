-- 2026-07-15 などの決算発表翌営業日リターン検証結果。
-- 既存 market_data は変更せず、イベント単位の検証結果を別表に保存する。
CREATE TABLE IF NOT EXISTS earnings_reactions (
  source_event_id              TEXT PRIMARY KEY,
  code                         TEXT NOT NULL,
  jquants_code                 TEXT,
  company_name                 TEXT NOT NULL,
  fiscal_quarter               TEXT,
  earnings_date                TEXT NOT NULL,
  close_2026_07_15_raw         REAL,
  open_2026_07_16_raw          REAL,
  close_2026_07_16_raw         REAL,
  close_2026_07_15_adjusted    REAL,
  open_2026_07_16_adjusted     REAL,
  close_2026_07_16_adjusted    REAL,
  open_gap_return_pct          REAL,
  next_close_return_pct        REAL,
  intraday_return_pct          REAL,
  volume_2026_07_16            INTEGER,
  trading_value_2026_07_16     REAL,
  upper_limit_flag             INTEGER,
  lower_limit_flag             INTEGER,
  data_status                  TEXT NOT NULL,
  missing_reason               TEXT,
  primary_event_id             TEXT,
  primary_event_title          TEXT,
  primary_earnings_published_at_jst TEXT,
  release_time_jst             TEXT,
  release_session              TEXT,
  reaction_window_valid        INTEGER NOT NULL DEFAULT 0,
  release_time_source          TEXT,
  release_time_status          TEXT,
  release_time_note            TEXT,
  outcome_eligible             INTEGER NOT NULL DEFAULT 0,
  primary_outcome_label        TEXT,
  primary_outcome_band         TEXT,
  outcome_label_basis          TEXT,
  outcome_label_version        TEXT,
  outcome_labeled_at           TEXT,
  outcome_exclusion_reason     TEXT,
  created_at                   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at                   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_earnings_reactions_date
ON earnings_reactions(earnings_date);

CREATE INDEX IF NOT EXISTS ix_earnings_reactions_code
ON earnings_reactions(code);
