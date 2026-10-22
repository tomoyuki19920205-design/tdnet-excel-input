-- Old JPX current-outstanding reports publish Tokyo+Nagoya in million yen;
-- subtract the same-date official Nagoya total to retain Tokyo only.
ALTER TABLE public.margin_weekly_aggregate
  ADD COLUMN IF NOT EXISTS market_source_jpx_url text,
  ADD COLUMN IF NOT EXISTS market_source_nse_url text,
  ADD COLUMN IF NOT EXISTS market_source_unit_yen bigint;

CREATE OR REPLACE FUNCTION public.fill_margin_weekly_jpx_market(
  p_date date, p_two_market_million bigint, p_nagoya_million bigint,
  p_jpx_url text, p_nse_url text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_tokyo_million bigint;
BEGIN
  IF p_date < DATE '2026-07-17' OR p_date >= DATE '2026-09-25' OR
     p_two_market_million IS NULL OR p_nagoya_million IS NULL OR
     p_two_market_million <= p_nagoya_million OR p_nagoya_million < 0 OR
     p_jpx_url NOT LIKE 'https://www.jpx.co.jp/%' OR
     p_nse_url NOT LIKE 'https://www.nse.or.jp/%' THEN
    RAISE EXCEPTION 'invalid JPX/NSE old-market provenance';
  END IF;
  v_tokyo_million := p_two_market_million - p_nagoya_million;
  UPDATE public.margin_weekly_aggregate SET
    long_value_yen = v_tokyo_million::numeric * 1000000,
    money_source = 'JPX東京市場分（JPX東京・名古屋合計－名証名古屋分、約定代金、百万円単位）',
    market_scope = '東京証券取引所',
    market_source_jpx_url = p_jpx_url,
    market_source_nse_url = p_nse_url,
    market_source_unit_yen = 1000000,
    updated_at = now()
  WHERE date = p_date AND money_reason IS NOT NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'missing old weekly aggregate for %', p_date; END IF;
  RETURN jsonb_build_object('date', p_date, 'tokyo_million_yen', v_tokyo_million,
                            'scope', '東京証券取引所');
END;
$$;
REVOKE ALL ON FUNCTION public.fill_margin_weekly_jpx_market(date,bigint,bigint,text,text)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.fill_margin_weekly_jpx_market(date,bigint,bigint,text,text)
  TO service_role;
