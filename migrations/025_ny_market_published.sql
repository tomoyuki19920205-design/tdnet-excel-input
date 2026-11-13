-- Add the requested normal-publication state without changing any report rows.
ALTER TABLE canonical_ny_market_reports DROP CONSTRAINT IF EXISTS canonical_ny_market_quality_status_check;
ALTER TABLE canonical_ny_market_reports ADD CONSTRAINT canonical_ny_market_quality_status_check
 CHECK (quality_status IN ('provisional','published','complete'));
ALTER TABLE canonical_ny_market_reports DROP CONSTRAINT IF EXISTS canonical_ny_market_unresolved_check;
ALTER TABLE canonical_ny_market_reports ADD CONSTRAINT canonical_ny_market_unresolved_check CHECK (
 jsonb_typeof(unresolved_items)='array' AND
 ((quality_status='complete' AND jsonb_array_length(unresolved_items)=0) OR
  (quality_status IN ('published','provisional') AND jsonb_array_length(unresolved_items)>0)));
ALTER TABLE canonical_ny_market_report_runs DROP CONSTRAINT IF EXISTS canonical_ny_market_runs_status_check;
ALTER TABLE canonical_ny_market_report_runs ADD CONSTRAINT canonical_ny_market_runs_status_check
 CHECK (status IN ('pending','running','provisional','published','success','failed','retry_pending'));
-- The existing ten-company / ten-news / twenty-gainer guards also apply to published.
-- Preserve the actual live view, including any unrelated service changes.
DO $$
DECLARE v text; revised text;
BEGIN
 SELECT pg_get_viewdef('public.api_latest_news_stream'::regclass,true) INTO v;
 IF strpos(v,'【通常公開版・NY市場モーニング】')=0 THEN
  revised=replace(v, 'ELSE ''【NY市場モーニング】''::text',
   'WHEN m.quality_status = ''published''::text THEN ''【通常公開版・NY市場モーニング】''::text ELSE ''【完全版・NY市場モーニング】''::text');
  IF revised=v THEN RAISE EXCEPTION 'NY title expression changed; inspect live view before migration'; END IF;
  EXECUTE 'CREATE OR REPLACE VIEW public.api_latest_news_stream AS ' || revised;
 END IF;
END $$;
NOTIFY pgrst,'reload schema';
