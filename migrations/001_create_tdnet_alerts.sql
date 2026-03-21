-- ============================================================
-- TDNET Alerts: テーブル + Index + RLS + Realtime
-- Supabase SQL Editorで手動実行すること
-- ============================================================

-- ============================================================
-- 1. tdnet_events — イベント本体
-- ============================================================
CREATE TABLE IF NOT EXISTS public.tdnet_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    detected_at     timestamptz NOT NULL,
    disclosed_at    timestamptz NULL,
    ticker          text NOT NULL,
    company_name    text NOT NULL DEFAULT '',
    market          text NULL,
    event_type      text NOT NULL,
    event_subtype   text NULL,
    headline        text NOT NULL DEFAULT '',
    summary         text NOT NULL DEFAULT '',
    source_title    text NULL,
    source_url      text NULL,
    pdf_url         text NULL,
    raw_payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    strength_score  numeric NULL,
    priority_rank   integer NOT NULL DEFAULT 999,
    primary_metric_name  text NULL,
    primary_metric_value text NULL,
    primary_metric_yoy   text NULL,
    display_title   text NOT NULL DEFAULT '',
    display_summary text NOT NULL DEFAULT '',
    sort_key        text NULL,
    dedupe_key      text NOT NULL,
    notify_to_discord    boolean NOT NULL DEFAULT false,
    discord_sent_at      timestamptz NULL,
    archived_at     timestamptz NULL,
    status          text NOT NULL DEFAULT 'active',
    schema_version  integer NOT NULL DEFAULT 1,
    CONSTRAINT tdnet_events_status_check CHECK (status IN ('active', 'archived'))
);

-- Indexes
CREATE UNIQUE INDEX IF NOT EXISTS idx_tdnet_events_dedupe_key ON public.tdnet_events(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_tdnet_events_detected_at ON public.tdnet_events(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_tdnet_events_ticker ON public.tdnet_events(ticker);
CREATE INDEX IF NOT EXISTS idx_tdnet_events_event_type ON public.tdnet_events(event_type);
CREATE INDEX IF NOT EXISTS idx_tdnet_events_archived_at ON public.tdnet_events(archived_at);
CREATE INDEX IF NOT EXISTS idx_tdnet_events_priority_rank ON public.tdnet_events(priority_rank);
CREATE INDEX IF NOT EXISTS idx_tdnet_events_status ON public.tdnet_events(status);

-- ============================================================
-- 2. tdnet_event_reads — 既読管理
-- ============================================================
CREATE TABLE IF NOT EXISTS public.tdnet_event_reads (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    event_id    uuid NOT NULL REFERENCES public.tdnet_events(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL,
    read_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tdnet_event_reads_unique UNIQUE (event_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_tdnet_event_reads_user ON public.tdnet_event_reads(user_id, read_at DESC);
CREATE INDEX IF NOT EXISTS idx_tdnet_event_reads_event ON public.tdnet_event_reads(event_id);

-- ============================================================
-- 3. tdnet_event_stars — スター管理
-- ============================================================
CREATE TABLE IF NOT EXISTS public.tdnet_event_stars (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    event_id    uuid NOT NULL REFERENCES public.tdnet_events(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL,
    starred_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tdnet_event_stars_unique UNIQUE (event_id, user_id)
);

-- ============================================================
-- 4. tdnet_event_comments — コメント
-- ============================================================
CREATE TABLE IF NOT EXISTS public.tdnet_event_comments (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    event_id    uuid NOT NULL REFERENCES public.tdnet_events(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL,
    comment     text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tdnet_event_comments_event ON public.tdnet_event_comments(event_id, created_at ASC);

-- ============================================================
-- 5. RLS Policies
-- ============================================================

-- Enable RLS on all tables
ALTER TABLE public.tdnet_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tdnet_event_reads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tdnet_event_stars ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tdnet_event_comments ENABLE ROW LEVEL SECURITY;

-- tdnet_events: SELECT for authenticated, INSERT/UPDATE/DELETE for service_role only
CREATE POLICY "tdnet_events_select_authenticated"
    ON public.tdnet_events FOR SELECT
    USING (auth.uid() IS NOT NULL);

-- tdnet_event_reads: SELECT for authenticated, INSERT/DELETE for own user_id
CREATE POLICY "tdnet_event_reads_select_authenticated"
    ON public.tdnet_event_reads FOR SELECT
    USING (auth.uid() IS NOT NULL);

CREATE POLICY "tdnet_event_reads_insert_own"
    ON public.tdnet_event_reads FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "tdnet_event_reads_delete_own"
    ON public.tdnet_event_reads FOR DELETE
    USING (auth.uid() = user_id);

-- tdnet_event_stars: SELECT for authenticated, INSERT/DELETE for own user_id
CREATE POLICY "tdnet_event_stars_select_authenticated"
    ON public.tdnet_event_stars FOR SELECT
    USING (auth.uid() IS NOT NULL);

CREATE POLICY "tdnet_event_stars_insert_own"
    ON public.tdnet_event_stars FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "tdnet_event_stars_delete_own"
    ON public.tdnet_event_stars FOR DELETE
    USING (auth.uid() = user_id);

-- tdnet_event_comments: SELECT for authenticated, INSERT/UPDATE/DELETE for own user_id
CREATE POLICY "tdnet_event_comments_select_authenticated"
    ON public.tdnet_event_comments FOR SELECT
    USING (auth.uid() IS NOT NULL);

CREATE POLICY "tdnet_event_comments_insert_own"
    ON public.tdnet_event_comments FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "tdnet_event_comments_update_own"
    ON public.tdnet_event_comments FOR UPDATE
    USING (auth.uid() = user_id);

CREATE POLICY "tdnet_event_comments_delete_own"
    ON public.tdnet_event_comments FOR DELETE
    USING (auth.uid() = user_id);

-- ============================================================
-- 6. Realtime有効化
-- ============================================================
-- tdnet_events をリアルタイム購読対象に追加
ALTER PUBLICATION supabase_realtime ADD TABLE public.tdnet_events;

-- コメントもリアルタイム購読対象に追加（共同運用用）
ALTER PUBLICATION supabase_realtime ADD TABLE public.tdnet_event_comments;
