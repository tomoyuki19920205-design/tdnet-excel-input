-- J-Quants margin-interest: one published daily row is the atomic reader boundary.
CREATE TABLE IF NOT EXISTS public.margin_daily (
  date date PRIMARY KEY,
  published_date date,
  fetched_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  row_count integer NOT NULL CHECK (row_count > 0),
  master_matched_count integer NOT NULL,
  long_value_yen numeric(22,0) NOT NULL,
  short_value_yen numeric(22,0) NOT NULL,
  sector_long_yen jsonb NOT NULL,
  outside_long_yen numeric(22,0) NOT NULL,
  outside_count integer NOT NULL,
  unknown_long_yen numeric(22,0) NOT NULL,
  unknown_count integer NOT NULL,
  topix_close numeric(12,2),
  nikkei_close numeric(12,2),
  is_week_final boolean NOT NULL,
  status text NOT NULL DEFAULT 'complete' CHECK (status = 'complete')
);

CREATE TABLE IF NOT EXISTS public.margin_security_daily (
  date date NOT NULL,
  code text NOT NULL,
  long_value_yen numeric(22,0) NOT NULL,
  long_volume bigint NOT NULL,
  sector_code text,
  category text NOT NULL CHECK (category IN ('sector','outside','unknown')),
  PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS public.margin_security_weekly (
  date date NOT NULL,
  code text NOT NULL,
  long_value_yen numeric(22,0),
  long_volume bigint NOT NULL,
  sector_code text,
  category text NOT NULL CHECK (category IN ('sector','outside','unknown')),
  PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS public.margin_weekly_batch (
  date date PRIMARY KEY,
  row_count integer NOT NULL CHECK (row_count > 0),
  fetched_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS margin_security_daily_date_idx ON public.margin_security_daily(date);
CREATE INDEX IF NOT EXISTS margin_security_weekly_date_idx ON public.margin_security_weekly(date);

ALTER TABLE public.margin_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.margin_security_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.margin_security_weekly ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.margin_weekly_batch ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON public.margin_daily TO authenticated;
REVOKE ALL ON public.margin_daily FROM anon;
REVOKE ALL ON public.margin_security_daily, public.margin_security_weekly, public.margin_weekly_batch FROM anon,authenticated;
CREATE POLICY margin_daily_authenticated_read ON public.margin_daily
  FOR SELECT TO authenticated USING (true);
-- Individual rows are deliberately unavailable to browser roles.

CREATE OR REPLACE FUNCTION public.publish_margin_snapshot(
  p_date date, p_published_date date, p_rows jsonb,
  p_store_daily boolean, p_store_weekly boolean, p_is_week_final boolean,
  p_topix_close numeric DEFAULT NULL, p_nikkei_close numeric DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_count integer;
  v_matched integer;
  v_long numeric(22,0);
  v_short numeric(22,0);
  v_outside numeric(22,0);
  v_unknown numeric(22,0);
  v_outside_count integer;
  v_unknown_count integer;
  v_invalid_sector_count integer;
  v_sectors jsonb;
  v_cutoff_daily date := ((now() AT TIME ZONE 'Asia/Tokyo')::date - interval '1 month')::date;
  v_cutoff_weekly date := ((now() AT TIME ZONE 'Asia/Tokyo')::date - interval '6 months')::date;
BEGIN
  IF p_date IS NULL OR jsonb_typeof(p_rows) IS DISTINCT FROM 'array' OR jsonb_array_length(p_rows) = 0 THEN
    RAISE EXCEPTION 'empty or invalid margin snapshot';
  END IF;
  IF p_store_daily AND p_date < v_cutoff_daily THEN
    RAISE EXCEPTION 'daily retention expired for %', p_date;
  END IF;
  IF p_store_weekly AND (NOT p_is_week_final OR p_date < v_cutoff_weekly) THEN
    RAISE EXCEPTION 'weekly retention or calendar invalid for %', p_date;
  END IF;
  CREATE TEMP TABLE IF NOT EXISTS margin_stage (
    code text, long_vol bigint, long_val numeric(22,0), short_val numeric(22,0),
    sector_code text, category text
  ) ON COMMIT DROP;
  TRUNCATE margin_stage;
  INSERT INTO margin_stage
    SELECT code, long_vol, long_val, short_val, sector_code, category
    FROM jsonb_to_recordset(p_rows) AS x(
      code text, long_vol bigint, long_val numeric, short_val numeric,
      sector_code text, category text
    );
  SELECT count(*), count(DISTINCT code),
         count(*) FILTER (WHERE code IS NULL OR code = '' OR long_vol IS NULL OR long_vol < 0),
         count(*) FILTER (WHERE category NOT IN ('sector','outside','unknown') OR category IS NULL),
         count(*) FILTER (WHERE category = 'sector' AND sector_code NOT IN
           ('0050','1050','2050','3050','3100','3150','3200','3250','3300','3350','3400','3450',
            '3500','3550','3600','3650','3700','3750','3800','4050','5050','5100','5150','5200',
            '5250','6050','6100','7050','7100','7150','7200','8050','9050'))
    INTO v_count, v_matched, v_outside_count, v_unknown_count, v_invalid_sector_count
    FROM margin_stage;
  IF v_count <> v_matched OR v_outside_count <> 0 OR v_unknown_count <> 0 OR v_invalid_sector_count <> 0 THEN
    RAISE EXCEPTION 'invalid, duplicate, or unclassified margin rows';
  END IF;
  IF p_date >= DATE '2026-09-25' AND EXISTS (
    SELECT 1 FROM margin_stage WHERE long_val IS NULL OR short_val IS NULL OR long_val < 0 OR short_val < 0
  ) THEN
    RAISE EXCEPTION 'missing monetary values for %', p_date;
  END IF;
  IF p_date < DATE '2026-09-25' AND EXISTS (
    SELECT 1 FROM margin_stage WHERE long_val IS NOT NULL OR short_val IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'unexpected old monetary values for %', p_date;
  END IF;

  IF p_date >= DATE '2026-09-25' THEN
    SELECT sum(long_val), sum(short_val),
           coalesce(sum(long_val) FILTER (WHERE category = 'outside'),0),
           coalesce(sum(long_val) FILTER (WHERE category = 'unknown'),0),
           count(*) FILTER (WHERE category = 'outside'),
           count(*) FILTER (WHERE category = 'unknown'),
           count(*) FILTER (WHERE category <> 'unknown')
    INTO v_long, v_short, v_outside, v_unknown,
         v_outside_count, v_unknown_count, v_matched
    FROM margin_stage;
    SELECT coalesce(jsonb_object_agg(sector_code, total), '{}'::jsonb)
      INTO v_sectors FROM (
        SELECT sector_code, sum(long_val) AS total FROM margin_stage
        WHERE category = 'sector' GROUP BY sector_code
      ) s;
    IF v_long <> coalesce((SELECT sum(value::numeric) FROM jsonb_each_text(v_sectors)),0) + v_outside + v_unknown THEN
      RAISE EXCEPTION 'margin sector reconciliation failed';
    END IF;
    INSERT INTO public.margin_daily (
      date,published_date,fetched_at,row_count,master_matched_count,long_value_yen,short_value_yen,
      sector_long_yen,outside_long_yen,outside_count,unknown_long_yen,unknown_count,
      topix_close,nikkei_close,is_week_final
    ) VALUES (
      p_date,p_published_date,now(),v_count,v_matched,v_long,v_short,v_sectors,
      v_outside,v_outside_count,v_unknown,v_unknown_count,p_topix_close,p_nikkei_close,p_is_week_final
    ) ON CONFLICT (date) DO UPDATE SET
      published_date=EXCLUDED.published_date,fetched_at=EXCLUDED.fetched_at,updated_at=now(),
      row_count=EXCLUDED.row_count,master_matched_count=EXCLUDED.master_matched_count,
      long_value_yen=EXCLUDED.long_value_yen,short_value_yen=EXCLUDED.short_value_yen,
      sector_long_yen=EXCLUDED.sector_long_yen,outside_long_yen=EXCLUDED.outside_long_yen,
      outside_count=EXCLUDED.outside_count,unknown_long_yen=EXCLUDED.unknown_long_yen,
      unknown_count=EXCLUDED.unknown_count,
      topix_close=coalesce(EXCLUDED.topix_close,public.margin_daily.topix_close),
      nikkei_close=coalesce(EXCLUDED.nikkei_close,public.margin_daily.nikkei_close),
      is_week_final=EXCLUDED.is_week_final;
  END IF;

  DELETE FROM public.margin_security_daily WHERE date = p_date;
  IF p_store_daily THEN
    INSERT INTO public.margin_security_daily
      SELECT p_date,code,long_val,long_vol,sector_code,category FROM margin_stage;
  END IF;
  DELETE FROM public.margin_security_weekly WHERE date = p_date;
  DELETE FROM public.margin_weekly_batch WHERE date = p_date;
  IF p_store_weekly THEN
    INSERT INTO public.margin_security_weekly
      SELECT p_date,code,long_val,long_vol,sector_code,category FROM margin_stage;
    INSERT INTO public.margin_weekly_batch VALUES (p_date,v_count,now());
  END IF;
  RETURN jsonb_build_object('date',p_date,'row_count',v_count,'long_value_yen',v_long,
                            'short_value_yen',v_short,'outside_count',v_outside_count,
                            'unknown_count',v_unknown_count);
END;
$$;

CREATE OR REPLACE FUNCTION public.fill_margin_indices(
  p_date date, p_topix_close numeric DEFAULT NULL, p_nikkei_close numeric DEFAULT NULL
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  UPDATE public.margin_daily SET
    topix_close=coalesce(p_topix_close,topix_close),
    nikkei_close=coalesce(p_nikkei_close,nikkei_close),updated_at=now()
  WHERE date=p_date;
  RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION public.cleanup_margin_security()
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_daily_count bigint;
  v_weekly_count bigint;
  v_daily_cutoff date := ((now() AT TIME ZONE 'Asia/Tokyo')::date - interval '1 month')::date;
  v_weekly_cutoff date := ((now() AT TIME ZONE 'Asia/Tokyo')::date - interval '6 months')::date;
BEGIN
  DELETE FROM public.margin_security_daily d USING public.margin_daily s
  WHERE d.date=s.date AND d.date < v_daily_cutoff AND s.status='complete'
    AND (NOT s.is_week_final OR EXISTS (
      SELECT 1 FROM public.margin_weekly_batch w
      WHERE w.date=d.date AND w.row_count=s.row_count
    ));
  GET DIAGNOSTICS v_daily_count = ROW_COUNT;
  DELETE FROM public.margin_security_weekly WHERE date < v_weekly_cutoff;
  GET DIAGNOSTICS v_weekly_count = ROW_COUNT;
  DELETE FROM public.margin_weekly_batch WHERE date < v_weekly_cutoff;
  RETURN jsonb_build_object('daily_deleted',v_daily_count,'weekly_deleted',v_weekly_count,
    'daily_cutoff',v_daily_cutoff,'weekly_cutoff',v_weekly_cutoff);
END;
$$;

REVOKE ALL ON FUNCTION public.publish_margin_snapshot(date,date,jsonb,boolean,boolean,boolean,numeric,numeric) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.fill_margin_indices(date,numeric,numeric) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.cleanup_margin_security() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.publish_margin_snapshot(date,date,jsonb,boolean,boolean,boolean,numeric,numeric) TO service_role;
GRANT EXECUTE ON FUNCTION public.fill_margin_indices(date,numeric,numeric) TO service_role;
GRANT EXECUTE ON FUNCTION public.cleanup_margin_security() TO service_role;
