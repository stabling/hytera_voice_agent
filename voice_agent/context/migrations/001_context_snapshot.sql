PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS context_migration_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    command_id TEXT,
    actor_id TEXT,
    terminal_id TEXT,
    tenant_id TEXT,
    item_ids_json TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    memory_ids_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_snapshot_command ON context_snapshot(command_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_snapshot_actor ON context_snapshot(actor_id, created_at DESC);
INSERT OR IGNORE INTO context_migration_version(version, applied_at, description)
VALUES(1, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'context snapshot');
