CREATE TABLE IF NOT EXISTS public.ipo_analysis_reports (
    id uuid PRIMARY KEY,
    stable_key text NOT NULL UNIQUE,
    schema_version text NOT NULL,
    report_type text NOT NULL DEFAULT 'ipo_analysis',
    report_version integer NOT NULL DEFAULT 1,
    event_id uuid NOT NULL REFERENCES public.tdnet_events(id) ON DELETE CASCADE,
    ticker text NOT NULL,
    company_name text NOT NULL,
    listing_date date NOT NULL,
    market text,
    status text NOT NULL CHECK (status IN ('pending','collecting','completed','partial','failed')),
    source_manifest jsonb NOT NULL DEFAULT '[]'::jsonb,
    facts jsonb NOT NULL DEFAULT '{}'::jsonb,
    calculations jsonb NOT NULL DEFAULT '[]'::jsonb,
    validation_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    report_markdown text NOT NULL DEFAULT '',
    generation_model text,
    prompt_version text NOT NULL,
    content_sha256 text,
    previous_content_sha256 text,
    last_error text,
    generated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ipo_analysis_event ON public.ipo_analysis_reports(event_id);
CREATE INDEX IF NOT EXISTS ix_ipo_analysis_status ON public.ipo_analysis_reports(status, listing_date);

ALTER TABLE public.ipo_analysis_reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Authenticated users can read IPO analysis reports" ON public.ipo_analysis_reports;
CREATE POLICY "Authenticated users can read IPO analysis reports"
ON public.ipo_analysis_reports FOR SELECT TO authenticated USING (true);

GRANT SELECT ON public.ipo_analysis_reports TO authenticated;
GRANT ALL ON public.ipo_analysis_reports TO service_role;
