-- Optional. Run only after VECTOR_SEARCH_ENABLE has an approved retrieval evaluation.
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS memory_embedding(
  memory_id text PRIMARY KEY REFERENCES memory_item(memory_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  embedding vector(1536) NOT NULL,
  embedding_model text NOT NULL,
  content_hash text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE memory_embedding ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_embedding FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_scope ON memory_embedding;
CREATE POLICY tenant_scope ON memory_embedding
  USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),''))
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),''));
CREATE INDEX IF NOT EXISTS idx_memory_embedding_tenant ON memory_embedding(tenant_id);
-- Build the HNSW index only after capacity/recall benchmarking. Exact search is the safe initial mode.
-- CREATE INDEX CONCURRENTLY idx_memory_embedding_hnsw ON memory_embedding USING hnsw (embedding vector_cosine_ops);
COMMIT;
