-- ============================================================
-- 001_pipeline_tables.sql
-- Webアプリ自動更新パイプライン運用テーブル (Phase 1)
-- Supabase SQL Editor で実行
-- ============================================================

-- =========================
-- 1) pipeline_runs — バッチ実行ログ
-- =========================
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_type          TEXT NOT NULL,            -- ingest|process|rebuild|notify|reconcile|retry
  trigger_type      TEXT DEFAULT 'manual',    -- scheduler|manual|retry|reconcile
  started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at       TIMESTAMPTZ,
  status            TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','done','failed')),
  processed_count   INT DEFAULT 0,
  success_count     INT DEFAULT 0,
  failed_count      INT DEFAULT 0,
  quarantined_count INT DEFAULT 0,
  skipped_count     INT DEFAULT 0,
  duration_sec      NUMERIC(10,2),
  message           TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_job_status
  ON pipeline_runs(job_type, status, started_at DESC);

-- =========================
-- 2) job_queue — 疑似ジョブキュー
-- =========================
CREATE TABLE IF NOT EXISTS job_queue (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_type          TEXT NOT NULL,            -- process|rebuild|notify
  target_type       TEXT,                     -- ticker|filing|batch
  target_id         TEXT,                     -- ticker code / filing ID
  payload_json      JSONB,
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','failed','quarantined','skipped')),
  priority          INT NOT NULL DEFAULT 5,
  attempts          INT NOT NULL DEFAULT 0,
  error_message     TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_job_queue_status
  ON job_queue(status, priority, created_at);
CREATE INDEX IF NOT EXISTS ix_job_queue_target
  ON job_queue(target_type, target_id);

-- 重複制御: 同一 (job_type, target_type, target_id) で pending/running は 1本のみ
-- アプリ層で制御する (UNIQUE 制約だと done 行との競合管理が複雑)

-- =========================
-- 3) rebuild_queue — ticker 再構築キュー
-- =========================
CREATE TABLE IF NOT EXISTS rebuild_queue (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker            TEXT NOT NULL,
  reason            TEXT,                     -- new_filing|correction|retry|reconcile
  source_job_id     BIGINT,
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','failed')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_rebuild_queue_status
  ON rebuild_queue(status, created_at);
CREATE INDEX IF NOT EXISTS ix_rebuild_queue_ticker
  ON rebuild_queue(ticker);

-- =========================
-- 4) quarantine_items — 抽出失敗隔離
-- =========================
CREATE TABLE IF NOT EXISTS quarantine_items (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker            TEXT,
  filing_id         TEXT,
  stage             TEXT NOT NULL,            -- ingest|extract|normalize|push|rebuild
  issue_type        TEXT NOT NULL,            -- parse_error|missing_metric|type_mismatch|...
  severity          TEXT NOT NULL DEFAULT 'warn'
                    CHECK (severity IN ('info','warn','error','critical')),
  detail            TEXT,
  raw_ref           TEXT,
  suggested_action  TEXT,
  status            TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','resolved','wont_fix','duplicate')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_quarantine_status
  ON quarantine_items(status, severity);
CREATE INDEX IF NOT EXISTS ix_quarantine_ticker
  ON quarantine_items(ticker);

-- =========================
-- 5) data_quality_issues — 品質警告
-- =========================
CREATE TABLE IF NOT EXISTS data_quality_issues (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  check_name        TEXT NOT NULL,            -- duplicate_winner|missing_metric|quarter_anomaly|...
  ticker            TEXT,
  detail            TEXT,
  severity          TEXT NOT NULL DEFAULT 'warn'
                    CHECK (severity IN ('info','warn','error','critical')),
  status            TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','resolved','wont_fix')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_dqi_check_status
  ON data_quality_issues(check_name, status);

-- =========================
-- RLS (Row Level Security) — anon read, service_role write
-- =========================
-- pipeline_runs
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY pipeline_runs_anon_read ON pipeline_runs
  FOR SELECT USING (true);
CREATE POLICY pipeline_runs_service_write ON pipeline_runs
  FOR ALL USING (auth.role() = 'service_role');

-- job_queue
ALTER TABLE job_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY job_queue_anon_read ON job_queue
  FOR SELECT USING (true);
CREATE POLICY job_queue_service_write ON job_queue
  FOR ALL USING (auth.role() = 'service_role');

-- rebuild_queue
ALTER TABLE rebuild_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY rebuild_queue_anon_read ON rebuild_queue
  FOR SELECT USING (true);
CREATE POLICY rebuild_queue_service_write ON rebuild_queue
  FOR ALL USING (auth.role() = 'service_role');

-- quarantine_items
ALTER TABLE quarantine_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY quarantine_items_anon_read ON quarantine_items
  FOR SELECT USING (true);
CREATE POLICY quarantine_items_service_write ON quarantine_items
  FOR ALL USING (auth.role() = 'service_role');

-- data_quality_issues
ALTER TABLE data_quality_issues ENABLE ROW LEVEL SECURITY;
CREATE POLICY dqi_anon_read ON data_quality_issues
  FOR SELECT USING (true);
CREATE POLICY dqi_service_write ON data_quality_issues
  FOR ALL USING (auth.role() = 'service_role');
