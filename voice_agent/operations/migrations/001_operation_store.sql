-- Command/AtomicOperation audit store.
-- Runtime uses the equivalent idempotent schema in voice_agent.operations.schema.
CREATE TABLE IF NOT EXISTS operation_migration_version (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY(component, version)
);
