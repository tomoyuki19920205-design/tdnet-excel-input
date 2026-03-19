-- EDINET Segment Integration — Supabase Schema Extension
-- canonical_segments テーブルに EDINET 統合用カラムを追加

-- source_system: データ取得元システム (tdnet/edinet/jquants)
ALTER TABLE canonical_segments
  ADD COLUMN IF NOT EXISTS source_system TEXT DEFAULT 'tdnet';

-- segment_type: ordinary/subtotal/total/adjustment/corporate/other
ALTER TABLE canonical_segments
  ADD COLUMN IF NOT EXISTS segment_type TEXT DEFAULT 'ordinary';

-- derivation_method: reported_qtd/derived_from_ytd_diff/ytd_only
ALTER TABLE canonical_segments
  ADD COLUMN IF NOT EXISTS derivation_method TEXT;

-- YTD/QTD 分離値 (EDINET の累計→四半期変換で使用)
ALTER TABLE canonical_segments
  ADD COLUMN IF NOT EXISTS sales_ytd BIGINT;
ALTER TABLE canonical_segments
  ADD COLUMN IF NOT EXISTS profit_ytd BIGINT;
ALTER TABLE canonical_segments
  ADD COLUMN IF NOT EXISTS sales_qtd BIGINT;
ALTER TABLE canonical_segments
  ADD COLUMN IF NOT EXISTS profit_qtd BIGINT;

-- Index for source_system + segment_type (overlap 検証・集計用)
CREATE INDEX IF NOT EXISTS idx_canonical_segments_source_system
  ON canonical_segments (source_system);
CREATE INDEX IF NOT EXISTS idx_canonical_segments_segment_type
  ON canonical_segments (segment_type);
