-- Browser access to news requires a signed-in, allowlisted user. The raw
-- api_latest_news_* views are postgres-owned and must remain unavailable to
-- browser roles, including after 016/017/019 are run again.

CREATE OR REPLACE VIEW public.api_authorized_news_events
WITH (security_barrier = true) AS
SELECT * FROM public.api_latest_news_events
WHERE auth.role() = 'authenticated'
  AND auth.uid() IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM public.allowed_users AS a
      WHERE a.email = auth.jwt() ->> 'email'
  );

CREATE OR REPLACE VIEW public.api_authorized_news_scan_runs
WITH (security_barrier = true) AS
SELECT * FROM public.api_latest_news_scan_runs
WHERE auth.role() = 'authenticated'
  AND auth.uid() IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM public.allowed_users AS a
      WHERE a.email = auth.jwt() ->> 'email'
  );

CREATE OR REPLACE VIEW public.api_authorized_news_stream
WITH (security_barrier = true) AS
SELECT * FROM public.api_latest_news_stream
WHERE auth.role() = 'authenticated'
  AND auth.uid() IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM public.allowed_users AS a
      WHERE a.email = auth.jwt() ->> 'email'
  );

REVOKE ALL ON public.api_latest_news_events,
              public.api_latest_news_scan_runs,
              public.api_latest_news_stream
    FROM PUBLIC, anon, authenticated;

REVOKE ALL ON public.api_authorized_news_events,
              public.api_authorized_news_scan_runs,
              public.api_authorized_news_stream
    FROM PUBLIC, anon, authenticated;

GRANT SELECT ON public.api_authorized_news_events,
                public.api_authorized_news_scan_runs,
                public.api_authorized_news_stream
    TO authenticated, service_role;
