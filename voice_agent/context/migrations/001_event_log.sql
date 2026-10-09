CREATE TABLE IF NOT EXISTS event_log (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_service TEXT NOT NULL,
    trace_id TEXT,
    correlation_id TEXT,
    causation_id TEXT,
    command_id TEXT,
    operation_id TEXT,
    call_id TEXT,
    actor_id TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_trace ON event_log(trace_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_call ON event_log(call_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_correlation ON event_log(correlation_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_type ON event_log(event_type, occurred_at);
CREATE TABLE IF NOT EXISTS event_journal_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
