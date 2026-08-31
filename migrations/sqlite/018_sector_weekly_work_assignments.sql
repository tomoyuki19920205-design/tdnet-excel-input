-- SQLite counterpart of ../018_sector_weekly_work_assignments.sql.
-- PostgreSQL UUID/TIMESTAMPTZ/NOW()/RLS are represented locally by TEXT keys,
-- fixed UTC RFC3339 timestamps, explicit CHECK constraints, and no RLS clause.
-- MAX_ATTEMPTS is the application invariant 3 (and therefore >= 1); it is an
-- attempt_count bound rather than a 21st column so both queue schemas retain
-- the same 20 logical columns.

CREATE TABLE IF NOT EXISTS sector_weekly_sqlite_migrations (
    migration_id TEXT PRIMARY KEY NOT NULL,
    checksum_sha256 TEXT NOT NULL CHECK (
        length(checksum_sha256) = 64
        AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    applied_at TEXT NOT NULL CHECK (
        length(applied_at) = 20
        AND applied_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
    ),
    runner_version TEXT NOT NULL CHECK (length(trim(runner_version)) > 0)
);

CREATE TABLE IF NOT EXISTS sector_weekly_work_assignments (
    assignment_id TEXT PRIMARY KEY NOT NULL,
    schema_version TEXT NOT NULL CHECK (schema_version = 'sector_weekly_assignment_v1'),
    stable_key TEXT NOT NULL UNIQUE,
    sector_code INTEGER NOT NULL CHECK (
        typeof(sector_code) = 'integer' AND sector_code BETWEEN 1 AND 33
    ),
    sector_name TEXT NOT NULL CHECK (length(trim(sector_name)) > 0),
    period_start TEXT NOT NULL CHECK (
        length(period_start) = 20
        AND period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
    ),
    period_end TEXT NOT NULL CHECK (
        length(period_end) = 20
        AND period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
        AND period_end > period_start
    ),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'ready', 'claimed', 'running', 'success', 'retry_pending', 'failed'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(attempt_count) = 'integer' AND attempt_count BETWEEN 0 AND 3
    ),
    available_at TEXT NOT NULL CHECK (
        length(available_at) = 20
        AND available_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
    ),
    claim_owner TEXT CHECK (claim_owner IS NULL OR length(trim(claim_owner)) > 0),
    claimed_at TEXT CHECK (
        claimed_at IS NULL OR (
            length(claimed_at) = 20
            AND claimed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
        )
    ),
    lease_expires_at TEXT CHECK (
        lease_expires_at IS NULL OR (
            length(lease_expires_at) = 20
            AND lease_expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
        )
    ),
    started_at TEXT CHECK (
        started_at IS NULL OR (
            length(started_at) = 20
            AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
        )
    ),
    completed_at TEXT CHECK (
        completed_at IS NULL OR (
            length(completed_at) = 20
            AND completed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
        )
    ),
    last_error_type TEXT,
    last_error_message TEXT,
    submitted_payload_hash TEXT CHECK (
        submitted_payload_hash IS NULL OR (
            length(submitted_payload_hash) = 64
            AND submitted_payload_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    created_at TEXT NOT NULL CHECK (
        length(created_at) = 20
        AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
    ),
    updated_at TEXT NOT NULL CHECK (
        length(updated_at) = 20
        AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
    ),
    CHECK (
        status NOT IN ('claimed', 'running') OR (
            claim_owner IS NOT NULL
            AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_sector_weekly_work_ready
    ON sector_weekly_work_assignments(status, available_at, sector_code);

CREATE INDEX IF NOT EXISTS ix_sector_weekly_work_lease
    ON sector_weekly_work_assignments(lease_expires_at)
    WHERE status IN ('claimed', 'running');
