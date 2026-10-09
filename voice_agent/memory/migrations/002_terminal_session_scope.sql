PRAGMA foreign_keys=ON;
ALTER TABLE memory_item ADD COLUMN terminal_session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_memory_terminal_session ON memory_item(tenant_id, actor_id, terminal_id, terminal_session_id, verification_status, updated_at DESC);
INSERT OR IGNORE INTO memory_migration_version(version, applied_at, description)
VALUES(2, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'terminal plus session memory scope');
