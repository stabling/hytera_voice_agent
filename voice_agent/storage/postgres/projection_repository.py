# =============================================================================
# 【维护注释】文件：voice_agent/storage/postgres/projection_repository.py
# 所属层次：PostgreSQL 持久化适配层
# 主要职责：实现事件、检查点、投影等 PostgreSQL Repository，并与 SQLite/迁移模式保持可切换。
# 项目内直接依赖：voice_agent.storage.canonical, voice_agent.storage.interfaces
# 主要公开符号：PostgresProjectionRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""PostgreSQL JSONB projections for Call/Operation/Memory/Context read cutover.

The normalized business tables remain authoritative for schema/RLS.  This adapter
provides a deterministic migration target that can be populated before every old
SQLite writer has been switched, enabling dual-write and reconciliation first.
"""
from __future__ import annotations
from typing import Any, Iterable
from voice_agent.storage.canonical import canonical_json, content_hash, redact
from voice_agent.storage.interfaces import RepositoryScope


class PostgresProjectionRepository:
    def __init__(self, pool: Any, record_type: str) -> None:
        self.pool=pool; self.record_type=str(record_type)

    def upsert(self, record_id: str, payload: dict[str, Any], *, scope: RepositoryScope | None = None) -> str:
        clean=redact(payload); digest=content_hash(clean); s=(scope or RepositoryScope()).normalized()
        with self.pool.connection(s) as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO storage_projection(record_type,record_id,tenant_id,actor_id,terminal_id,payload,payload_hash,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,clock_timestamp())
              ON CONFLICT(record_type,record_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,actor_id=EXCLUDED.actor_id,
              terminal_id=EXCLUDED.terminal_id,payload=EXCLUDED.payload,payload_hash=EXCLUDED.payload_hash,updated_at=clock_timestamp()""",
              (self.record_type,str(record_id),s.tenant_id,s.actor_id,s.terminal_id,canonical_json(clean),digest))
        return digest

    def get(self, record_id: str, *, scope: RepositoryScope | None = None) -> dict[str, Any] | None:
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload,payload_hash FROM storage_projection WHERE record_type=%s AND record_id=%s",(self.record_type,str(record_id)))
            row=cur.fetchone()
        return None if row is None else {**dict(row[0]),"payload_hash":str(row[1])}

    def list(self, *, scope: RepositoryScope | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload,payload_hash FROM storage_projection WHERE record_type=%s ORDER BY updated_at DESC,record_id DESC LIMIT %s OFFSET %s",(self.record_type,max(1,min(int(limit),1000)),max(0,int(offset))))
            rows=cur.fetchall()
        return [{**dict(payload),"payload_hash":str(digest)} for payload,digest in rows]
