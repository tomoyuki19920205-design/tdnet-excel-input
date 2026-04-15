-- ============================================================
-- 002: formatted_message カラム追加
-- Discord と完全一致のフォーマット済みメッセージを保存
-- ============================================================
ALTER TABLE public.tdnet_events
    ADD COLUMN IF NOT EXISTS formatted_message text NOT NULL DEFAULT '';
