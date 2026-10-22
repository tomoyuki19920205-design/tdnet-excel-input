-- Weekly snapshots retain actual application-date volumes. Amounts before
-- 2026-09-25 are NULL because J-Quants did not publish issue-level yen values.
CREATE TABLE IF NOT EXISTS public.margin_weekly_aggregate (
  date date PRIMARY KEY,
  row_count integer NOT NULL CHECK (row_count > 0),
  long_volume bigint NOT NULL CHECK (long_volume >= 0),
  sector_long_volume jsonb NOT NULL,
  outside_long_volume bigint NOT NULL,
  unknown_long_volume bigint NOT NULL,
  outside_count integer NOT NULL,
  unknown_count integer NOT NULL,
  long_value_yen numeric(22,0),
  sector_long_yen jsonb NOT NULL DEFAULT '{}'::jsonb,
  outside_long_yen numeric(22,0),
  unknown_long_yen numeric(22,0),
  money_reason text,
  money_source text,
  market_scope text NOT NULL DEFAULT '東京証券取引所',
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((long_value_yen IS NULL AND money_reason IS NOT NULL) OR
         (long_value_yen IS NOT NULL AND money_source IS NOT NULL))
);

-- Only genuine complete-week candles are stored here. The current provisional
-- week continues to be assembled from saved daily bars through its balance day.
CREATE TABLE IF NOT EXISTS public.margin_index_weekly (
  date date NOT NULL,
  code text NOT NULL CHECK (code = 'TOPIX' OR code ~ '^(004[0-9A-F]|005[0-9A-F]|0060)$'),
  open numeric(18,6) NOT NULL CHECK (open > 0),
  high numeric(18,6) NOT NULL CHECK (high > 0),
  low numeric(18,6) NOT NULL CHECK (low > 0),
  close numeric(18,6) NOT NULL CHECK (close > 0),
  trading_days integer NOT NULL CHECK (trading_days > 0),
  source text NOT NULL DEFAULT 'J-Quants V2 /indices/bars/daily',
  fetched_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (date, code),
  CHECK (high >= GREATEST(open, close) AND low <= LEAST(open, close) AND low <= high)
);

ALTER TABLE public.margin_weekly_aggregate ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.margin_index_weekly ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.margin_weekly_aggregate, public.margin_index_weekly TO authenticated;
REVOKE ALL ON public.margin_weekly_aggregate, public.margin_index_weekly FROM anon;
CREATE POLICY margin_weekly_aggregate_authenticated_read ON public.margin_weekly_aggregate
  FOR SELECT TO authenticated USING (true);
CREATE POLICY margin_index_weekly_authenticated_read ON public.margin_index_weekly
  FOR SELECT TO authenticated USING (true);

CREATE OR REPLACE FUNCTION public.refresh_margin_weekly_aggregate(p_date date)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_count integer;
  v_expected integer;
  v_volume bigint;
  v_outside bigint;
  v_unknown bigint;
  v_outside_count integer;
  v_unknown_count integer;
  v_money numeric(22,0);
  v_outside_money numeric(22,0);
  v_unknown_money numeric(22,0);
  v_sector_volume jsonb;
  v_sector_money jsonb;
  v_missing_money integer;
BEGIN
  SELECT row_count INTO v_expected FROM public.margin_weekly_batch WHERE date = p_date;
  IF v_expected IS NULL THEN RAISE EXCEPTION 'missing weekly batch for %', p_date; END IF;
  SELECT count(*), sum(long_volume),
         coalesce(sum(long_volume) FILTER (WHERE category = 'outside'), 0),
         coalesce(sum(long_volume) FILTER (WHERE category = 'unknown'), 0),
         count(*) FILTER (WHERE category = 'outside'),
         count(*) FILTER (WHERE category = 'unknown'),
         count(*) FILTER (WHERE long_value_yen IS NULL),
         sum(long_value_yen),
         coalesce(sum(long_value_yen) FILTER (WHERE category = 'outside'), 0),
         coalesce(sum(long_value_yen) FILTER (WHERE category = 'unknown'), 0)
  INTO v_count, v_volume, v_outside, v_unknown, v_outside_count,
       v_unknown_count, v_missing_money,
       v_money, v_outside_money, v_unknown_money
  FROM public.margin_security_weekly WHERE date = p_date;
  IF v_count <> v_expected THEN
    RAISE EXCEPTION 'weekly count mismatch for %: % != %', p_date, v_count, v_expected;
  END IF;
  IF (p_date < DATE '2026-09-25' AND v_missing_money <> v_count) OR
     (p_date >= DATE '2026-09-25' AND v_missing_money <> 0) THEN
    RAISE EXCEPTION 'weekly money availability mismatch for %', p_date;
  END IF;
  SELECT jsonb_object_agg(code, volume),
         CASE WHEN p_date < DATE '2026-09-25' THEN '{}'::jsonb
              ELSE jsonb_object_agg(code, money) END
  INTO v_sector_volume, v_sector_money
  FROM (
    SELECT sector.code,
           coalesce(sum(stock.long_volume), 0)::bigint AS volume,
           coalesce(sum(stock.long_value_yen), 0)::numeric(22,0) AS money
    FROM unnest(ARRAY[
      '0050','1050','2050','3050','3100','3150','3200','3250','3300','3350','3400',
      '3450','3500','3550','3600','3650','3700','3750','3800','4050','5050','5100',
      '5150','5200','5250','6050','6100','7050','7100','7150','7200','8050','9050'
    ]) AS sector(code)
    LEFT JOIN public.margin_security_weekly stock
      ON stock.date = p_date AND stock.category = 'sector' AND stock.sector_code = sector.code
    GROUP BY sector.code
  ) grouped;
  IF v_volume <> coalesce((SELECT sum(value::bigint) FROM jsonb_each_text(v_sector_volume)), 0)
                 + v_outside + v_unknown THEN
    RAISE EXCEPTION 'weekly volume reconciliation failed for %', p_date;
  END IF;
  IF p_date >= DATE '2026-09-25' AND
     v_money <> coalesce((SELECT sum(value::numeric) FROM jsonb_each_text(v_sector_money)), 0)
               + v_outside_money + v_unknown_money THEN
    RAISE EXCEPTION 'weekly money reconciliation failed for %', p_date;
  END IF;
  INSERT INTO public.margin_weekly_aggregate (
    date, row_count, long_volume, sector_long_volume, outside_long_volume,
    unknown_long_volume, outside_count, unknown_count, long_value_yen,
    sector_long_yen, outside_long_yen,
    unknown_long_yen, money_reason, money_source
  ) VALUES (
    p_date, v_count, v_volume, v_sector_volume, v_outside, v_unknown,
    v_outside_count, v_unknown_count,
    CASE WHEN p_date < DATE '2026-09-25' THEN NULL ELSE v_money END,
    v_sector_money,
    CASE WHEN p_date < DATE '2026-09-25' THEN NULL ELSE v_outside_money END,
    CASE WHEN p_date < DATE '2026-09-25' THEN NULL ELSE v_unknown_money END,
    CASE WHEN p_date < DATE '2026-09-25' THEN
      'J-Quants個別銘柄の金額データ未提供（2026-09-25より前）' ELSE NULL END,
    CASE WHEN p_date >= DATE '2026-09-25' THEN
      'J-Quants V2 /markets/margin-interest 銘柄別金額合計' ELSE NULL END
  ) ON CONFLICT (date) DO UPDATE SET
    row_count = EXCLUDED.row_count,
    long_volume = EXCLUDED.long_volume,
    sector_long_volume = EXCLUDED.sector_long_volume,
    outside_long_volume = EXCLUDED.outside_long_volume,
    unknown_long_volume = EXCLUDED.unknown_long_volume,
    outside_count = EXCLUDED.outside_count,
    unknown_count = EXCLUDED.unknown_count,
    long_value_yen = CASE WHEN EXCLUDED.date < DATE '2026-09-25'
      AND public.margin_weekly_aggregate.money_source LIKE 'JPX東京市場分%'
      THEN public.margin_weekly_aggregate.long_value_yen ELSE EXCLUDED.long_value_yen END,
    sector_long_yen = EXCLUDED.sector_long_yen,
    outside_long_yen = EXCLUDED.outside_long_yen,
    unknown_long_yen = EXCLUDED.unknown_long_yen,
    money_reason = EXCLUDED.money_reason,
    money_source = CASE WHEN EXCLUDED.date < DATE '2026-09-25'
      AND public.margin_weekly_aggregate.money_source LIKE 'JPX東京市場分%'
      THEN public.margin_weekly_aggregate.money_source ELSE EXCLUDED.money_source END,
    updated_at = now();
  RETURN jsonb_build_object('date', p_date, 'rows', v_count,
                            'volume', v_volume, 'money_available', p_date >= DATE '2026-09-25');
END;
$$;
REVOKE ALL ON FUNCTION public.refresh_margin_weekly_aggregate(date) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.refresh_margin_weekly_aggregate(date) TO service_role;
