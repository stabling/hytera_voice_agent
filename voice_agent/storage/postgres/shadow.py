# =============================================================================
# 【维护注释】文件：voice_agent/storage/postgres/shadow.py
# 所属层次：PostgreSQL 持久化适配层
# 主要职责：实现事件、检查点、投影等 PostgreSQL Repository，并与 SQLite/迁移模式保持可切换。
# 项目内直接依赖：voice_agent.storage.canonical, voice_agent.storage.interfaces
# 主要公开符号：PostgresShadowStore
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Canonical shadow table used for dual-write reconciliation and safe cutover evidence."""
from __future__ import annotations
from typing import Any, Mapping
from voice_agent.storage.canonical import canonical_json, content_hash, redact
from voice_agent.storage.interfaces import RepositoryScope


class PostgresShadowStore:
    def __init__(self, pool: Any, *, table: str = "storage_shadow_record") -> None:
        self.pool = pool
        if table not in {"storage_shadow_record", "storage_projection"}:
            raise ValueError("invalid storage table")
        self.table = table

    def upsert_record(self, *, record_type: str, record_id: str, payload: Mapping[str, Any], scope: RepositoryScope | None = None) -> str:
        clean = redact(dict(payload))
        digest = content_hash(clean)
        s = (scope or RepositoryScope()).normalized()
        with self.pool.connection(s) as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO {self.table}(record_type,record_id,tenant_id,actor_id,terminal_id,payload,payload_hash,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,clock_timestamp())
                ON CONFLICT(record_type,record_id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id,actor_id=EXCLUDED.actor_id,terminal_id=EXCLUDED.terminal_id,
                  payload=EXCLUDED.payload,payload_hash=EXCLUDED.payload_hash,updated_at=clock_timestamp()
            """.format(self=self), (str(record_type), str(record_id), s.tenant_id, s.actor_id, s.terminal_id, canonical_json(clean), digest))
        return digest

    def get_record(self, *, record_type: str, record_id: str, scope: RepositoryScope | None = None) -> dict[str, Any] | None:
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload,payload_hash FROM {self.table} WHERE record_type=%s AND record_id=%s".format(self=self), (record_type, record_id))
            row=cur.fetchone()
        if row is None:
            return None
        return {"payload": row[0], "payload_hash": row[1]}
