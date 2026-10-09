CREATE TABLE IF NOT EXISTS approval_request(
 approval_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, decision_id TEXT NOT NULL,
 origin_command_id TEXT NOT NULL, thread_id TEXT NOT NULL, plan_hash TEXT NOT NULL,
 policy_id TEXT NOT NULL, risk_level TEXT NOT NULL, status TEXT NOT NULL,
 requested_payload_json TEXT NOT NULL, approved_payload_json TEXT,
 reviewer_id TEXT, reviewer_role TEXT, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
