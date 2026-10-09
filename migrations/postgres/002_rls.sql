-- RLS must be tested with the runtime role; FORCE RLS also covers table owners unless bypass privileges are used.
BEGIN;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['domain_event','storage_projection','storage_shadow_record','call_session','command_record','operation_record','memory_item','memory_transition','context_snapshot','object_manifest']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION app_scope_match(row_tenant text,row_actor text,row_terminal text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT row_tenant = NULLIF(current_setting('app.tenant_id',true),'')
   AND (
     (row_actor IS NULL AND row_terminal IS NULL)
     OR row_actor = NULLIF(current_setting('app.actor_id',true),'')
     OR row_actor = NULLIF(current_setting('app.terminal_id',true),'')
     OR row_terminal = NULLIF(current_setting('app.terminal_id',true),'')
     OR row_terminal = NULLIF(current_setting('app.actor_id',true),'')
   );
$$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['domain_event','storage_projection','storage_shadow_record','call_session','command_record','operation_record','memory_item','memory_transition','context_snapshot','object_manifest']
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS tenant_scope ON %I',t);
    EXECUTE format('CREATE POLICY tenant_scope ON %I USING (app_scope_match(tenant_id,actor_id,terminal_id)) WITH CHECK (app_scope_match(tenant_id,actor_id,terminal_id))',t);
  END LOOP;
END $$;
COMMIT;
