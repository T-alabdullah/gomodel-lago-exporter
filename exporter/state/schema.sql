-- The exporter's own state. Applied by `exporter init-db`.
-- Every statement is "IF NOT EXISTS", so running it again changes nothing.

-- 1. CURSOR: how far the exporter has read GoModel's usage table.
--    Rows are read in (timestamp, id) order. The cursor is the last row handled.
--    It only moves forward, and only after Lago has accepted the batch.
CREATE TABLE IF NOT EXISTS export_cursor (
    name            TEXT PRIMARY KEY,           -- always 'usage' for now
    last_timestamp  TIMESTAMPTZ NOT NULL,
    last_id         UUID NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. USAGE ROWS: one line per GoModel usage row the exporter has handled.
--    The *_tokens_sent columns are what Lago accepted; reconciliation (Step 10)
--    sums them per subscription and model.
CREATE TABLE IF NOT EXISTS usage_rows (
    usage_row_id              UUID PRIMARY KEY,   -- GoModel's usage.id
    usage_timestamp           TIMESTAMPTZ NOT NULL,
    status                    TEXT NOT NULL
        CHECK (status IN ('sent', 'failed', 'unmapped', 'not_billable')),
    external_subscription_id  TEXT,               -- NULL when unmapped / not billable
    model                     TEXT NOT NULL,
    provider_name             TEXT,
    input_tokens_sent         BIGINT NOT NULL DEFAULT 0,
    cached_input_tokens_sent  BIGINT NOT NULL DEFAULT 0,
    output_tokens_sent        BIGINT NOT NULL DEFAULT 0,
    attempts                  INTEGER NOT NULL DEFAULT 1,
    last_error                TEXT,
    first_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_rows_status_idx ON usage_rows (status);
CREATE INDEX IF NOT EXISTS usage_rows_timestamp_idx ON usage_rows (usage_timestamp);

-- 3. DEAD LETTERS: rows that could not be billed, with the reason.
--    At most one open (unresolved) entry per usage row, so re-reading the
--    overlap window never piles up duplicates.
CREATE TABLE IF NOT EXISTS dead_letters (
    id            BIGSERIAL PRIMARY KEY,
    usage_row_id  UUID NOT NULL,
    reason        TEXT NOT NULL,          -- 'unmapped', 'rejected' or 'retries_exhausted'
    error         TEXT,                   -- details (e.g. Lago's error message)
    payload       JSONB,                  -- the row or events, for debugging and replay
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ             -- set when the row is later billed successfully
);
CREATE UNIQUE INDEX IF NOT EXISTS dead_letters_one_open_per_row
    ON dead_letters (usage_row_id) WHERE resolved_at IS NULL;

-- Saved and committed BEFORE any HTTP send. Retries always reuse this exact
-- destination and payload, even after a source/config change or source deletion.
CREATE TABLE IF NOT EXISTS deliveries (
    usage_row_id UUID PRIMARY KEY,
    payload JSONB NOT NULL,
    pending BOOLEAN NOT NULL DEFAULT TRUE,
    acknowledged JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deliveries_pending_idx
    ON deliveries (usage_row_id) WHERE pending;

CREATE TABLE IF NOT EXISTS event_acknowledgements (
    usage_row_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('in', 'cached', 'out')),
    tokens BIGINT NOT NULL,
    first_ack_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (usage_row_id, kind)
);
CREATE INDEX IF NOT EXISTS acknowledgement_time_idx ON event_acknowledgements(first_ack_at);

CREATE TABLE IF NOT EXISTS worker_status (
    name TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    succeeded BOOLEAN,
    last_error TEXT,
    report JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id BIGSERIAL PRIMARY KEY,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL CHECK (status IN ('matched', 'mismatch', 'incomplete')),
    report JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS reconciliation_period_idx ON reconciliation_runs(period_start, period_end, completed_at DESC);
