-- PostgreSQL production schema.
BEGIN;
CREATE TABLE IF NOT EXISTS schema_migrations(
  version integer PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  description text NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_event(
  event_id text NOT NULL,
  occurred_at timestamptz NOT NULL,
  event_type text NOT NULL,
  schema_version integer NOT NULL CHECK(schema_version>=1),
  tenant_id text NOT NULL,
  actor_id text,
  terminal_id text,
  trace_id text,
  correlation_id text,
  causation_id text,
  command_id text,
  operation_id text,
  call_id text,
  payload jsonb NOT NULL,
  payload_hash text NOT NULL CHECK(length(payload_hash)=64),
  recorded_at timestamptz NOT NULL,
  source_service text NOT NULL,
  PRIMARY KEY(event_id,occurred_at)
) PARTITION BY RANGE(occurred_at);
CREATE TABLE IF NOT EXISTS domain_event_default PARTITION OF domain_event DEFAULT;
CREATE INDEX IF NOT EXISTS idx_domain_event_tenant_time ON domain_event(tenant_id,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_domain_event_trace_time ON domain_event(trace_id,occurred_at) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_domain_event_call_time ON domain_event(call_id,occurred_at) WHERE call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_domain_event_command_time ON domain_event(command_id,occurred_at) WHERE command_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_domain_event_type_time ON domain_event(event_type,occurred_at DESC);

-- Canonical JSONB projections enable shadow dual-write before every SQLite writer is retired.
CREATE TABLE IF NOT EXISTS storage_projection(
  record_type text NOT NULL,
  record_id text NOT NULL,
  tenant_id text,
  actor_id text,
  terminal_id text,
  payload jsonb NOT NULL,
  payload_hash text NOT NULL CHECK(length(payload_hash)=64),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(record_type,record_id)
);
CREATE INDEX IF NOT EXISTS idx_storage_projection_scope ON storage_projection(record_type,tenant_id,actor_id,terminal_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS storage_shadow_record(LIKE storage_projection INCLUDING ALL);

CREATE TABLE IF NOT EXISTS call_session(
  call_id text PRIMARY KEY,
  schema_version integer NOT NULL DEFAULT 1,
  tenant_id text NOT NULL,
  actor_id text,
  terminal_id text,
  direction text NOT NULL,
  call_type text NOT NULL,
  status text NOT NULL,
  source_id text,
  target_id text,
  group_id text,
  channel_id text,
  request_id text,
  requested_at timestamptz,
  answered_at timestamptz,
  ended_at timestamptz,
  duration_ms bigint,
  version integer NOT NULL DEFAULT 1,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_session_scope_time ON call_session(tenant_id,terminal_id,requested_at DESC NULLS LAST,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_session_target_time ON call_session(tenant_id,target_id,requested_at DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS command_record(
  command_id text PRIMARY KEY,
  schema_version integer NOT NULL,
  tenant_id text NOT NULL,
  actor_id text,
  terminal_id text,
  status text NOT NULL,
  trace_id text,
  correlation_id text,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_command_scope_status ON command_record(tenant_id,actor_id,status,created_at DESC);

CREATE TABLE IF NOT EXISTS operation_record(
  operation_id text PRIMARY KEY,
  schema_version integer NOT NULL,
  tenant_id text NOT NULL,
  actor_id text,
  terminal_id text,
  origin_command_id text NOT NULL,
  operation_type text NOT NULL,
  target_id text,
  status text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  arguments_hash text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_operation_scope_time ON operation_record(tenant_id,actor_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_command ON operation_record(origin_command_id,created_at);
CREATE INDEX IF NOT EXISTS idx_operation_target ON operation_record(tenant_id,target_id,created_at DESC) WHERE target_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_item(
  memory_id text PRIMARY KEY,
  schema_version integer NOT NULL,
  namespace text NOT NULL,
  memory_type text NOT NULL,
  tenant_id text NOT NULL,
  actor_id text,
  terminal_id text,
  subject_entity_id text,
  predicate text,
  object_json jsonb,
  summary text NOT NULL,
  content text NOT NULL,
  confidence double precision NOT NULL CHECK(confidence>=0 AND confidence<=1),
  verification_status text NOT NULL,
  valid_from timestamptz,
  valid_to timestamptz,
  expires_at timestamptz,
  evidence_event_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_type text NOT NULL,
  source_model text,
  source_operation_id text,
  source_call_id text,
  sensitivity text NOT NULL,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  legal_hold boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope_status ON memory_item(tenant_id,actor_id,terminal_id,verification_status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_subject ON memory_item(tenant_id,subject_entity_id,predicate,verification_status);
CREATE INDEX IF NOT EXISTS idx_memory_expiry ON memory_item(expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS memory_transition(
  transition_id bigserial PRIMARY KEY, memory_id text NOT NULL, tenant_id text NOT NULL, actor_id text, terminal_id text,
  schema_version integer NOT NULL DEFAULT 1, from_status text NOT NULL, to_status text NOT NULL, actor_role text NOT NULL,
  reviewer_id text, reason text NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_memory_transition_memory ON memory_transition(tenant_id,memory_id,transition_id);


CREATE TABLE IF NOT EXISTS context_snapshot(
  snapshot_id text PRIMARY KEY,
  schema_version integer NOT NULL,
  tenant_id text NOT NULL,
  actor_id text,
  terminal_id text,
  command_id text,
  item_ids jsonb NOT NULL,
  event_ids jsonb NOT NULL,
  memory_ids jsonb NOT NULL,
  content_hash text NOT NULL CHECK(length(content_hash)=64),
  context jsonb NOT NULL,
  legal_hold boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_snapshot_scope_time ON context_snapshot(tenant_id,actor_id,terminal_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_snapshot_command ON context_snapshot(command_id,created_at DESC);

CREATE TABLE IF NOT EXISTS object_manifest(
  object_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  actor_id text,
  terminal_id text,
  object_key text NOT NULL UNIQUE,
  sha256 text NOT NULL CHECK(length(sha256)=64),
  byte_size bigint NOT NULL CHECK(byte_size>=0),
  media_type text,
  encryption text NOT NULL,
  category text NOT NULL,
  source_call_id text,
  source_event_id text,
  legal_hold boolean NOT NULL DEFAULT false,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_object_manifest_expiry ON object_manifest(tenant_id,category,expires_at) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS migration_checkpoint(
  migration_name text PRIMARY KEY,
  source_kind text NOT NULL,
  source_offset text,
  migrated_count bigint NOT NULL DEFAULT 0,
  payload_hash text,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS reconciliation_run(
  run_id text PRIMARY KEY,
  source_name text NOT NULL,
  target_name text NOT NULL,
  record_type text NOT NULL,
  source_count bigint NOT NULL,
  target_count bigint NOT NULL,
  missing_count bigint NOT NULL,
  mismatch_count bigint NOT NULL,
  report jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO schema_migrations(version,description) VALUES(1,'production core schema')
ON CONFLICT(version) DO NOTHING;
COMMIT;
