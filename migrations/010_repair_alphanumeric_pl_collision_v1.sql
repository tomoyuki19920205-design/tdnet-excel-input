-- Atomic, bounded repair for the 418A/472A PL collision.
CREATE TABLE IF NOT EXISTS public.alphanumeric_pl_collision_archive_v1 (
 repair_key text NOT NULL, archived_at timestamptz NOT NULL DEFAULT now(), source_table text NOT NULL,
 original_primary_key text, ticker text NOT NULL, category text NOT NULL, row_json jsonb NOT NULL,
 row_sha256 text NOT NULL, root_cause text NOT NULL, PRIMARY KEY(repair_key,source_table,original_primary_key)
);
CREATE TABLE IF NOT EXISTS public.alphanumeric_pl_collision_runs_v1 (
 repair_key text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now(), result jsonb NOT NULL
);
CREATE OR REPLACE FUNCTION public.repair_alphanumeric_pl_collision_v1(p_canonical jsonb,p_financials jsonb,p_preview boolean default false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $f$
DECLARE k constant text:='alphanumeric-pl-collision-418A-472A-v1'; ncf int; nfi int; acf int; afi int; result jsonb;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(k,0));
 IF EXISTS(SELECT 1 FROM public.alphanumeric_pl_collision_runs_v1 WHERE repair_key=k) THEN RETURN jsonb_build_object('status','ALREADY_APPLIED','changed',0); END IF;
 IF jsonb_typeof(p_canonical)<>'array' OR jsonb_typeof(p_financials)<>'array' OR jsonb_array_length(p_canonical)=0 OR jsonb_array_length(p_financials)=0 THEN RAISE EXCEPTION 'nonempty JSON arrays required'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_canonical) x WHERE x->>'ticker' NOT IN ('418A','472A') OR x->>'ticker' IS NULL OR (x->>'ticker'='418A' AND x->>'period' NOT LIKE '%-11-30') OR (x->>'ticker'='472A' AND x->>'period' NOT LIKE '%-12-31')) THEN RAISE EXCEPTION 'invalid canonical rebuild payload'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_financials) x WHERE x->>'ticker' NOT IN ('418A','472A') OR x->>'ticker' IS NULL OR (x->>'ticker'='418A' AND x->>'period' NOT LIKE '%-11-30') OR (x->>'ticker'='472A' AND x->>'period' NOT LIKE '%-12-31')) THEN RAISE EXCEPTION 'invalid financial rebuild payload'; END IF;
 SELECT count(*) INTO ncf FROM public.canonical_financials WHERE ticker IN ('418A','472A'); SELECT count(*) INTO nfi FROM public.financials WHERE ticker IN ('418A','472A');
 INSERT INTO public.alphanumeric_pl_collision_archive_v1(repair_key,source_table,original_primary_key,ticker,category,row_json,row_sha256,root_cause)
 SELECT k,'canonical_financials',id::text,ticker,'ARCHIVED_UNREPRODUCIBLE',to_jsonb(c),md5(to_jsonb(c)::text),'JQUANTS_ALPHA_MAP_NUMERIC_TO_ALPHA_COLLISION' FROM public.canonical_financials c WHERE ticker IN ('418A','472A');
 INSERT INTO public.alphanumeric_pl_collision_archive_v1(repair_key,source_table,original_primary_key,ticker,category,row_json,row_sha256,root_cause)
 SELECT k,'financials',ticker||'|'||period||'|'||quarter,ticker,'ARCHIVED_UNREPRODUCIBLE',to_jsonb(f),md5(to_jsonb(f)::text),'JQUANTS_ALPHA_MAP_NUMERIC_TO_ALPHA_COLLISION' FROM public.financials f WHERE ticker IN ('418A','472A');
 IF p_preview THEN RAISE EXCEPTION 'preview rollback'; END IF;
 DELETE FROM public.canonical_financials WHERE ticker IN ('418A','472A'); DELETE FROM public.financials WHERE ticker IN ('418A','472A');
 INSERT INTO public.canonical_financials(ticker,period,quarter,metric,value,unit,source,source_priority,source_row_key,extracted_at,updated_at)
 SELECT x->>'ticker',x->>'period',x->>'quarter',x->>'metric',(x->>'value')::numeric,'millions_jpy','jquants',2,x->>'source_row_key',now(),now() FROM jsonb_array_elements(p_canonical)x;
 GET DIAGNOSTICS acf=ROW_COUNT;
 INSERT INTO public.financials(ticker,period,quarter,sales,gross_profit,operating_profit,source,unit,updated_at)
 SELECT x->>'ticker',x->>'period',x->>'quarter',(x->>'sales')::numeric,(x->>'gross_profit')::numeric,(x->>'operating_profit')::numeric,'jquants','million_yen',now() FROM jsonb_array_elements(p_financials)x;
 GET DIAGNOSTICS afi=ROW_COUNT;
 IF EXISTS(SELECT 1 FROM public.canonical_financials WHERE (ticker='418A' AND period NOT LIKE '%-11-30') OR (ticker='472A' AND period NOT LIKE '%-12-31')) THEN RAISE EXCEPTION 'postflight fiscal month'; END IF;
 result:=jsonb_build_object('status','APPLIED','archived_canonical',ncf,'archived_financials',nfi,'inserted_canonical',acf,'inserted_financials',afi);
 INSERT INTO public.alphanumeric_pl_collision_runs_v1 VALUES(k,now(),result); RETURN result;
END;$f$;
REVOKE ALL ON FUNCTION public.repair_alphanumeric_pl_collision_v1(jsonb,jsonb,boolean) FROM PUBLIC,anon,authenticated; GRANT EXECUTE ON FUNCTION public.repair_alphanumeric_pl_collision_v1(jsonb,jsonb,boolean) TO service_role;
