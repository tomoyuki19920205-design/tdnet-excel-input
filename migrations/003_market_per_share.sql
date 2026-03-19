-- ============================================================
-- 003_market_per_share.sql
-- market_data + per_share_data テーブル追加
-- SQLite 用。Supabase (PostgreSQL) 版は別途 schema に記載。
-- ============================================================

-- =========================
-- 1) market_data — 日次株価
-- =========================
CREATE TABLE IF NOT EXISTS market_data (
  ticker         TEXT    NOT NULL,   -- 4桁コード
  date           TEXT    NOT NULL,   -- YYYY-MM-DD
  open           REAL,
  high           REAL,
  low            REAL,
  close          REAL,               -- 終値（調整前）
  volume         INTEGER,
  turnover       REAL,               -- 取引代金
  adj_factor     REAL,               -- 調整係数
  adj_close      REAL,               -- 調整済終値
  adj_volume     INTEGER,            -- 調整済出来高
  market_cap     REAL,               -- 時価総額（算出: close * 流通株式数）
  source         TEXT    NOT NULL DEFAULT 'jquants',
  fetched_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE(ticker, date)
);

CREATE INDEX IF NOT EXISTS ix_market_data_ticker
ON market_data(ticker);

CREATE INDEX IF NOT EXISTS ix_market_data_date
ON market_data(date DESC);

-- =========================
-- 2) per_share_data — 1株当たり指標
-- =========================
CREATE TABLE IF NOT EXISTS per_share_data (
  ticker                   TEXT NOT NULL,   -- 4桁コード
  period                   TEXT NOT NULL,   -- YYYY-MM-DD (fiscal_year_end)
  quarter                  TEXT NOT NULL,   -- 1Q/2Q/3Q/4Q/FY
  disclosed_date           TEXT,            -- 開示日
  -- 実績
  eps                      REAL,            -- EarningsPerShare
  diluted_eps              REAL,            -- DilutedEarningsPerShare
  bps                      REAL,            -- BookValuePerShare
  dividend_q1              REAL,            -- 1Q末配当実績
  dividend_q2              REAL,            -- 2Q末配当実績
  dividend_q3              REAL,            -- 3Q末配当実績
  dividend_fy_end          REAL,            -- 期末配当実績
  dividend_annual          REAL,            -- 年間配当実績合計
  payout_ratio             REAL,            -- 配当性向実績
  -- 予想
  forecast_eps             REAL,            -- EPS予想（期末）
  forecast_dividend_annual REAL,            -- 年間配当予想合計
  forecast_payout_ratio    REAL,            -- 配当性向予想
  -- 株式数
  shares_outstanding       INTEGER,         -- 期末発行済株式数（自己株式含む）
  treasury_stock           INTEGER,         -- 期末自己株式数
  avg_shares               INTEGER,         -- 期中平均株式数
  -- BS指標
  total_assets             INTEGER,         -- 総資産
  equity                   INTEGER,         -- 純資産
  equity_ratio             REAL,            -- 自己資本比率
  -- メタ
  source                   TEXT NOT NULL DEFAULT 'jquants',
  updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(ticker, period, quarter)
);

CREATE INDEX IF NOT EXISTS ix_per_share_data_ticker
ON per_share_data(ticker);

CREATE INDEX IF NOT EXISTS ix_per_share_data_period
ON per_share_data(ticker, period DESC);
