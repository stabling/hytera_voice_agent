-- Planning audit schema. Runtime uses the same statements in repository.py.
CREATE TABLE IF NOT EXISTS plan_record(
 decision_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, origin_command_id TEXT NOT NULL,
 context_snapshot_id TEXT, context_snapshot_hash TEXT, content_hash TEXT NOT NULL,
 status TEXT NOT NULL, risk_level TEXT NOT NULL, approval_required INTEGER NOT NULL,
 planner_id TEXT NOT NULL, planner_model TEXT, policy_id TEXT,
 plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
