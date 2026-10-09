# =============================================================================
# 【维护注释】文件：voice_agent/storage/postgres/event_repository.py
# 所属层次：PostgreSQL 持久化适配层
# 主要职责：实现事件、检查点、投影等 PostgreSQL Repository，并与 SQLite/迁移模式保持可切换。
# 项目内直接依赖：shared_contracts.event_models, shared_runtime.event_journal, voice_agent.storage.canonical, voice_agent.storage.interfaces
# 主要公开符号：PostgresEventRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Partitioned PostgreSQL domain-event repository."""
from __future__ import annotations
import hashlib
from typing import Any
from shared_contracts.event_models import DomainEvent, EventQuery, upcast_event_payload
from shared_runtime.event_journal import JournalAppendResult
from voice_agent.storage.canonical import canonical_json, redact
from voice_agent.storage.interfaces import RepositoryScope


class PostgresEventRepository:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def append(self, event: DomainEvent | dict[str, Any]) -> JournalAppendResult:
        domain = event if isinstance(event, DomainEvent) else DomainEvent.model_validate(event)
        payload = domain.model_dump(mode="json", exclude_none=True)
        payload["data"] = redact(payload.get("data") or {})
        payload_json = canonical_json(payload)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        tenant = str((domain.data or {}).get("tenant_id") or "").strip() or None
        terminal = str((domain.data or {}).get("terminal_id") or "").strip() or None
        scope=RepositoryScope(tenant_id=tenant, actor_id=domain.actor_id, terminal_id=terminal)
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO domain_event(event_id,occurred_at,event_type,schema_version,tenant_id,actor_id,terminal_id,
                    trace_id,correlation_id,causation_id,command_id,operation_id,call_id,payload,payload_hash,recorded_at,source_service)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT(event_id,occurred_at) DO NOTHING
            """, (domain.event_id,domain.occurred_at,domain.event_type,domain.schema_version,tenant or "default",domain.actor_id,terminal,
                  domain.trace_id,domain.correlation_id,domain.causation_id,domain.command_id,domain.operation_id,domain.call_id,
                  payload_json,digest,domain.recorded_at,domain.source_service))
            inserted = cur.rowcount == 1
        return JournalAppendResult(event_id=domain.event_id, inserted=inserted, duplicate=not inserted)

    def get(self, event_id: str, *, redacted: bool = True, scope: RepositoryScope | None = None) -> dict[str, Any] | None:
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload,payload_hash FROM domain_event WHERE event_id=%s ORDER BY occurred_at DESC LIMIT 1", (str(event_id),))
            row=cur.fetchone()
        if row is None: return None
        payload=upcast_event_payload(dict(row[0]))
        if redacted: payload["data"] = redact(payload.get("data") or {})
        payload["payload_hash"] = str(row[1])
        return payload

    def query(self, query: EventQuery | dict[str, Any], *, scope: RepositoryScope | None = None) -> list[dict[str, Any]]:
        q=query if isinstance(query,EventQuery) else EventQuery.model_validate(query)
        clauses=[]; params=[]
        for column in ("event_id","trace_id","correlation_id","call_id","event_type"):
            value=getattr(q,column)
            if value:
                clauses.append(f"{column}=%s"); params.append(value)
        where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
        params.extend([q.limit,q.offset])
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload,payload_hash FROM domain_event"+where+" ORDER BY occurred_at,event_id LIMIT %s OFFSET %s", params)
            rows=cur.fetchall()
        out=[]
        for payload,digest in rows:
            item=upcast_event_payload(dict(payload)); item["data"]=redact(item.get("data") or {}); item["payload_hash"]=str(digest); out.append(item)
        return out
