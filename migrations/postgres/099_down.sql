-- Destructive rollback. Normal production rollback should switch STORAGE_BACKEND, not drop data.
BEGIN;
DROP FUNCTION IF EXISTS ensure_domain_event_month(date);
DROP TABLE IF EXISTS memory_embedding;
DROP TABLE IF EXISTS reconciliation_run,migration_checkpoint,object_manifest,context_snapshot,memory_transition,memory_item,operation_record,command_record,call_session,storage_shadow_record,storage_projection CASCADE;
DROP TABLE IF EXISTS domain_event CASCADE;
DROP FUNCTION IF EXISTS app_scope_match(text,text,text);
DROP TABLE IF EXISTS schema_migrations;
COMMIT;
