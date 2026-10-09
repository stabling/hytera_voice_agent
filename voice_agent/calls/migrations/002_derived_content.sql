CREATE TABLE IF NOT EXISTS derived_content (
    derived_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    operation_type TEXT NOT NULL,
    call_id TEXT NOT NULL,
    ok INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_turn_ids_json TEXT NOT NULL,
    model TEXT,
    prompt_hash TEXT NOT NULL,
    options_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY(call_id) REFERENCES call_session(call_id)
);
CREATE INDEX IF NOT EXISTS idx_derived_call_time
    ON derived_content(call_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_derived_type_time
    ON derived_content(operation_type, generated_at DESC);

CREATE TABLE IF NOT EXISTS result_delivery (
    delivery_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(operation_id, target_id, target_type),
    FOREIGN KEY(derived_id) REFERENCES derived_content(derived_id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_status
    ON result_delivery(status, updated_at);

INSERT OR IGNORE INTO migration_version(version, applied_at, description)
VALUES(2, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'derived content and result delivery');
