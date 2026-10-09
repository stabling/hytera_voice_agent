PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS memory_migration_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_item (
    memory_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    namespace TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    tenant_id TEXT,
    actor_id TEXT,
    terminal_id TEXT,
    subject_entity_id TEXT,
    predicate TEXT,
    object_json TEXT,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL,
    verification_status TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    expires_at TEXT,
    evidence_event_ids_json TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_model TEXT,
    source_operation_id TEXT,
    source_call_id TEXT,
    sensitivity TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(confidence >= 0.0 AND confidence <= 1.0),
    CHECK(verification_status IN ('CANDIDATE','VERIFIED','DISPUTED','EXPIRED','RETIRED')),
    CHECK(memory_type IN ('EPISODIC','SEMANTIC','PROCEDURAL_CANDIDATE')),
    CHECK(sensitivity IN ('PUBLIC','INTERNAL','RESTRICTED','SECRET'))
);
CREATE INDEX IF NOT EXISTS idx_memory_namespace_status ON memory_item(namespace, verification_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_subject_predicate ON memory_item(subject_entity_id, predicate, verification_status);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_item(tenant_id, actor_id, terminal_id, verification_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_expiry ON memory_item(expires_at, verification_status);
CREATE INDEX IF NOT EXISTS idx_memory_source_operation ON memory_item(source_operation_id);
CREATE TABLE IF NOT EXISTS memory_transition (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    memory_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(memory_id) REFERENCES memory_item(memory_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_transition_memory ON memory_transition(memory_id, transition_id);
INSERT OR IGNORE INTO memory_migration_version(version, applied_at, description)
VALUES(1, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'governed memory store');
