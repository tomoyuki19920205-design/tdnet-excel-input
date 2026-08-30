-- Dedicated Sector Weekly work queue. This is intentionally separate from Company News.
CREATE TABLE IF NOT EXISTS sector_weekly_work_assignments (
    assignment_id UUID PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'sector_weekly_assignment_v1'),
    stable_key TEXT NOT NULL UNIQUE,
    sector_code INTEGER NOT NULL CHECK (sector_code BETWEEN 1 AND 33),
    sector_name TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'ready', 'claimed', 'running', 'success', 'retry_pending', 'failed'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at TIMESTAMPTZ NOT NULL,
    claim_owner TEXT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error_type TEXT,
    last_error_message TEXT,
    submitted_payload_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (period_end >= period_start),
    CHECK (submitted_payload_hash IS NULL OR submitted_payload_hash ~ '^[0-9a-f]{64}$'),
    CHECK (
        status NOT IN ('claimed', 'running') OR
        (claim_owner IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_sector_weekly_work_ready
    ON sector_weekly_work_assignments(status, available_at, sector_code);
CREATE INDEX IF NOT EXISTS ix_sector_weekly_work_lease
    ON sector_weekly_work_assignments(lease_expires_at)
    WHERE status IN ('claimed', 'running');

ALTER TABLE sector_weekly_work_assignments ENABLE ROW LEVEL SECURITY;
