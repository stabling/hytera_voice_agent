# =============================================================================
# 【维护注释】文件：voice_agent/storage/postgres/repositories.py
# 所属层次：PostgreSQL 持久化适配层
# 主要职责：实现事件、检查点、投影等 PostgreSQL Repository，并与 SQLite/迁移模式保持可切换。
# 项目内直接依赖：voice_agent.calls.models, voice_agent.memory.models, voice_agent.operations.models, voice_agent.commands.models, voice_agent.context.models, voice_agent.context.redaction, voice_agent.storage.canonical, voice_agent.storage.interfaces
# 主要公开符号：PostgresCallRepository, PostgresOperationRepository, PostgresCommandRepository, PostgresMemoryRepository, PostgresContextRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""PostgreSQL projection repositories used for read cutover and governed writes.

These adapters intentionally expose the same high-level methods consumed by the
runtime/7 context path.  Writes are canonical JSONB rows so dual-write parity can
be established before older SQLite schemas are retired.
"""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from typing import Any,Iterable

from voice_agent.calls.models import CallSelector,CallSession
from voice_agent.memory.models import MemoryItem,MemoryStatus,MemoryType,transition_allowed
from voice_agent.operations.models import AtomicOperation
from voice_agent.commands.models import Command
from voice_agent.context.models import ContextSnapshot
from voice_agent.context.redaction import canonical_json
from voice_agent.storage.canonical import content_hash,redact
from voice_agent.storage.interfaces import RepositoryScope
from .projection_repository import PostgresProjectionRepository

_ALLOWED_FILTER_COLUMNS={
 'call': {'call_id','observed_source_id','observed_target_id','group_id','initial_channel_id','status','direction','contact_id'},
 'operation': {'origin_command_id','operation_id','actor_id','target_id','operation_type','status'},
 'command': {'command_id','actor_id','status','source_type','source_ref'},
}

class _JsonRepo:
    def __init__(self,pool:Any,record_type:str): self.pool=pool; self.record_type=record_type; self.projection=PostgresProjectionRepository(pool,record_type)
    def _scope(self,*,tenant_id:str|None=None,actor_id:str|None=None,terminal_id:str|None=None)->RepositoryScope:
        return RepositoryScope(tenant_id or 'default',actor_id,terminal_id)
    def get_payload(self,record_id:str,*,scope:RepositoryScope|None=None): return self.projection.get(record_id,scope=scope)
    def _query_payloads(self,filters:dict[str,Any],*,scope:RepositoryScope|None=None,limit:int=100,offset:int=0,order_field:str='created_at'):
        allowed=_ALLOWED_FILTER_COLUMNS.get(self.record_type,set())
        clauses=['record_type=%s']; params:[Any]=[self.record_type]
        for field,value in filters.items():
            if value is None: continue
            if field not in allowed: raise ValueError(f'unsupported {self.record_type} filter: {field}')
            clauses.append(f"payload ->> '{field}' = %s"); params.append(str(value))
        if order_field not in {'created_at','updated_at','requested_at'}: order_field='created_at'
        params.extend([max(1,min(int(limit),1000)),max(0,int(offset))])
        sql="SELECT payload,payload_hash FROM storage_projection WHERE "+' AND '.join(clauses)+f" ORDER BY COALESCE(payload ->> '{order_field}','') DESC,record_id DESC LIMIT %s OFFSET %s"
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute(sql,params); rows=cur.fetchall()
        return [{**dict(p),'payload_hash':str(h)} for p,h in rows]

class PostgresCallRepository(_JsonRepo):
    def __init__(self,pool:Any,*,tenant_id:str='default'): super().__init__(pool,'call'); self.tenant_id=tenant_id
    def upsert_session(self,session:CallSession)->CallSession:
        payload=session.model_dump(mode='json',exclude_none=True); self.projection.upsert(session.call_id,payload,scope=self._scope(tenant_id=self.tenant_id,actor_id=session.observed_source_id)); return session
    def get_session(self,call_id:str,*,authorized_terminal_id:str|None=None)->CallSession|None:
        p=self.get_payload(str(call_id),scope=self._scope(tenant_id=self.tenant_id,actor_id=authorized_terminal_id,terminal_id=authorized_terminal_id)); return None if p is None else CallSession.model_validate({k:v for k,v in p.items() if k!='payload_hash'})
    def list_sessions(self,selector:CallSelector|dict[str,Any])->list[dict[str,Any]]:
        q=selector if isinstance(selector,CallSelector) else CallSelector.model_validate(selector); filters={}
        mapping={'call_id':'call_id','source_id':'observed_source_id','target_id':'observed_target_id','group_id':'group_id','channel_id':'initial_channel_id','status':'status','direction':'direction'}
        for src,dst in mapping.items():
            value=getattr(q,src,None)
            if value is not None: filters[dst]=value
        scope=self._scope(tenant_id=self.tenant_id,terminal_id=getattr(q,'authorized_terminal_id',None))
        rows=self._query_payloads(filters,scope=scope,limit=q.limit,offset=getattr(q,'offset',0),order_field='requested_at')
        # Remaining range/status-list filtering stays deterministic in application code.
        statuses=set(getattr(q,'statuses',None) or [])
        if statuses: rows=[r for r in rows if str(r.get('status')) in statuses]
        after=getattr(q,'started_after',None)
        if after: rows=[r for r in rows if str(r.get('requested_at') or r.get('created_at') or '')>=str(after)]
        return rows[:q.limit]
    def get_call(self,call_id:str,*,include_transcript:bool=True,authorized_terminal_id:str|None=None)->dict[str,Any]|None:
        del include_transcript
        p=self.get_payload(str(call_id),scope=self._scope(tenant_id=self.tenant_id,actor_id=authorized_terminal_id,terminal_id=authorized_terminal_id)); return None if p is None else p
    def find_by_event_id(self,event_id:str)->str|None: return None

class PostgresOperationRepository(_JsonRepo):
    def __init__(self,pool:Any,*,tenant_id:str='default'): super().__init__(pool,'operation'); self.tenant_id=tenant_id
    def upsert_operation(self,operation:AtomicOperation)->AtomicOperation:
        self.projection.upsert(operation.operation_id,operation.model_dump(mode='json',exclude_none=True),scope=self._scope(tenant_id=self.tenant_id,actor_id=operation.actor_id)); return operation
    def get(self,operation_id:str)->AtomicOperation|None:
        p=self.get_payload(str(operation_id),scope=self._scope(tenant_id=self.tenant_id)); return None if p is None else AtomicOperation.model_validate({k:v for k,v in p.items() if k!='payload_hash'})
    def query(self,*,command_id:str|None=None,operation_id:str|None=None,actor_id:str|None=None,target_id:str|None=None,operation_type:str|None=None,status:str|None=None,limit:int=100,offset:int=0)->list[dict[str,Any]]:
        f={'origin_command_id':command_id,'operation_id':operation_id,'actor_id':actor_id,'target_id':target_id,'operation_type':operation_type,'status':status}
        return self._query_payloads(f,scope=self._scope(tenant_id=self.tenant_id,actor_id=actor_id),limit=limit,offset=offset)

class PostgresCommandRepository(_JsonRepo):
    def __init__(self,pool:Any,*,tenant_id:str='default'): super().__init__(pool,'command'); self.tenant_id=tenant_id
    def get(self,command_id:str)->Command|None:
        p=self.get_payload(str(command_id),scope=self._scope(tenant_id=self.tenant_id)); return None if p is None else Command.model_validate({k:v for k,v in p.items() if k!='payload_hash'})
    def query(self,*,actor_id:str|None=None,status:str|None=None,source_type:str|None=None,limit:int=100,offset:int=0,**_:Any)->list[dict[str,Any]]:
        return self._query_payloads({'actor_id':actor_id,'status':status,'source_type':source_type},scope=self._scope(tenant_id=self.tenant_id,actor_id=actor_id),limit=limit,offset=offset)

class PostgresMemoryRepository(_JsonRepo):
    _VERIFY_ROLES = {'memory_verifier', 'admin'}
    _DISPUTE_ROLES = {'memory_verifier', 'privacy_officer', 'admin', 'subject_owner'}
    _RETIRE_ROLES = {'privacy_officer', 'admin'}

    def __init__(self, pool: Any, *, tenant_id: str = 'default'):
        super().__init__(pool, 'memory')
        self.tenant_id = tenant_id

    @property
    def fts_available(self) -> bool:
        return False

    def _effective_scope(
        self,
        scope: RepositoryScope | None = None,
        *,
        tenant_id: str | None = None,
        actor_id: str | None = None,
        terminal_id: str | None = None,
    ) -> RepositoryScope:
        if scope is not None:
            normalized = scope.normalized()
            return RepositoryScope(normalized.tenant_id or self.tenant_id, normalized.actor_id, normalized.terminal_id)
        return RepositoryScope(tenant_id or self.tenant_id, actor_id, terminal_id)

    @staticmethod
    def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        if str(value.get('sensitivity') or '').upper() in {'RESTRICTED', 'SECRET'}:
            value['content'] = '<redacted>'
        return redact(value)

    def create(self, item: MemoryItem | dict[str, Any]) -> MemoryItem:
        memory = item if isinstance(item, MemoryItem) else MemoryItem.model_validate(item)
        if memory.verification_status == MemoryStatus.VERIFIED and memory.source_type not in {'directory', 'human'}:
            raise ValueError('model/extractor output cannot be created directly as VERIFIED')
        scope = self._effective_scope(
            tenant_id=memory.tenant_id or self.tenant_id,
            actor_id=memory.actor_id,
            terminal_id=memory.terminal_id,
        )
        self.projection.upsert(
            memory.memory_id,
            memory.model_dump(mode='json', exclude_none=True),
            scope=scope,
        )
        return memory

    def get(self, memory_id: str, *, scope: RepositoryScope | None = None) -> MemoryItem | None:
        effective = self._effective_scope(scope)
        payload = self.get_payload(str(memory_id), scope=effective)
        return None if payload is None else MemoryItem.model_validate({k: v for k, v in payload.items() if k != 'payload_hash'})

    def get_authorized(
        self,
        memory_id: str,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
    ) -> MemoryItem | None:
        rows = self.search(
            tenant_id=tenant_id,
            actor_id=actor_id,
            terminal_id=terminal_id,
            memory_ids=(str(memory_id),),
            statuses=tuple(status.value for status in MemoryStatus),
            limit=1,
        )
        if not rows:
            return None
        return MemoryItem.model_validate({k: v for k, v in rows[0].items() if k != 'payload_hash'})

    def get_authorized_public(
        self,
        memory_id: str,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
    ) -> dict[str, Any] | None:
        rows = self.search(
            tenant_id=tenant_id,
            actor_id=actor_id,
            terminal_id=terminal_id,
            memory_ids=(str(memory_id),),
            statuses=tuple(status.value for status in MemoryStatus),
            limit=1,
        )
        return None if not rows else rows[0]

    def search(
        self,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
        query: str | None = None,
        statuses: Iterable[str] = ('VERIFIED',),
        memory_types: Iterable[str] | None = None,
        namespace: str | None = None,
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        memory_ids: Iterable[str] | None = None,
        limit: int = 30,
        offset: int = 0,
        use_fts: bool = False,
    ) -> list[dict[str, Any]]:
        del use_fts  # PostgreSQL semantic search is a separate, permission-rechecked optional layer.
        scope = self._effective_scope(
            tenant_id=tenant_id,
            actor_id=actor_id,
            terminal_id=terminal_id,
        )
        clauses = ['record_type=%s']
        params: list[Any] = ['memory']
        status_values = [str(x.value if isinstance(x, MemoryStatus) else x) for x in statuses]
        if not status_values:
            return []
        clauses.append("payload ->> 'verification_status' = ANY(%s)")
        params.append(status_values)
        for field, value in (
            ('namespace', namespace),
            ('subject_entity_id', subject_entity_id),
            ('predicate', predicate),
        ):
            if value is not None:
                clauses.append(f"payload ->> '{field}'=%s")
                params.append(str(value))
        if memory_types is not None:
            type_values = [str(x.value if isinstance(x, MemoryType) else x) for x in memory_types]
            if not type_values:
                return []
            clauses.append("payload ->> 'memory_type' = ANY(%s)")
            params.append(type_values)
        if memory_ids is not None:
            id_values = [str(x) for x in memory_ids]
            if not id_values:
                return []
            clauses.append('record_id = ANY(%s)')
            params.append(id_values)
        if query:
            clauses.append(
                "(payload ->> 'summary' ILIKE %s ESCAPE '\\' "
                "OR payload ->> 'content' ILIKE %s ESCAPE '\\' "
                "OR payload ->> 'predicate' ILIKE %s ESCAPE '\\')"
            )
            escaped = str(query).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            pattern = '%' + escaped + '%'
            params.extend([pattern] * 3)
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        sql = (
            "SELECT payload,payload_hash FROM storage_projection WHERE "
            + ' AND '.join(clauses)
            + " AND (payload ->> 'expires_at' IS NULL OR (payload ->> 'expires_at')::timestamptz>clock_timestamp()) "
              "ORDER BY COALESCE(payload ->> 'updated_at','') DESC, record_id DESC LIMIT %s OFFSET %s"
        )
        with self.pool.connection(scope) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {**self._public_payload(dict(payload)), 'payload_hash': str(digest)}
            for payload, digest in rows
        ]

    def transition(
        self,
        memory_id: str,
        to_status: MemoryStatus | str,
        *,
        actor_role: str,
        actor_id: str | None,
        reason: str,
        scope: RepositoryScope | None = None,
    ) -> MemoryItem:
        target = to_status if isinstance(to_status, MemoryStatus) else MemoryStatus(str(to_status))
        role = str(actor_role).strip().lower()
        if target == MemoryStatus.VERIFIED and role not in self._VERIFY_ROLES:
            raise PermissionError('VERIFIED requires memory_verifier or admin')
        if target == MemoryStatus.DISPUTED and role not in self._DISPUTE_ROLES:
            raise PermissionError('DISPUTED transition is not authorized')
        if target == MemoryStatus.RETIRED and role not in self._RETIRE_ROLES:
            raise PermissionError('RETIRED requires privacy_officer or admin')
        effective = self._effective_scope(scope)
        current = self.get(memory_id, scope=effective)
        if current is None:
            raise KeyError(f'memory not found or outside repository scope: {memory_id}')
        if not transition_allowed(current.verification_status, target):
            raise ValueError(f'invalid memory transition: {current.verification_status.value}->{target.value}')
        if target == MemoryStatus.VERIFIED and current.memory_type == MemoryType.PROCEDURAL_CANDIDATE:
            raise ValueError('procedural candidates remain offline review artifacts')
        now = datetime.now(timezone.utc).isoformat()
        updated = current.model_copy(update={'verification_status': target, 'updated_at': now})
        # Upsert under the subject scope, not the reviewer identity, so RLS remains exact.
        subject_scope = self._effective_scope(
            tenant_id=current.tenant_id or effective.tenant_id,
            actor_id=current.actor_id or effective.actor_id,
            terminal_id=current.terminal_id or effective.terminal_id,
        )
        self.projection.upsert(
            updated.memory_id,
            updated.model_dump(mode='json', exclude_none=True),
            scope=subject_scope,
        )
        with self.pool.connection(subject_scope) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memory_transition(memory_id,tenant_id,actor_id,terminal_id,schema_version,from_status,to_status,actor_role,reviewer_id,reason,occurred_at) "
                "VALUES(%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)",
                (
                    current.memory_id,
                    subject_scope.tenant_id,
                    subject_scope.actor_id,
                    subject_scope.terminal_id,
                    current.verification_status.value,
                    target.value,
                    role,
                    actor_id,
                    str(reason),
                    now,
                ),
            )
        return updated

    def expire_due(
        self,
        *,
        now: str | None = None,
        actor_id: str = 'system',
        scope: RepositoryScope | None = None,
    ) -> int:
        effective = self._effective_scope(scope)
        reference = str(now or datetime.now(timezone.utc).isoformat())
        with self.pool.connection(effective) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT record_id FROM storage_projection "
                "WHERE record_type='memory' "
                "AND payload ->> 'verification_status' = ANY(%s) "
                "AND payload ->> 'expires_at' IS NOT NULL "
                "AND (payload ->> 'expires_at')::timestamptz <= %s::timestamptz "
                "ORDER BY record_id",
                (['CANDIDATE', 'VERIFIED', 'DISPUTED'], reference),
            )
            ids = [str(row[0]) for row in cur.fetchall()]
        count = 0
        for memory_id in ids:
            self.transition(
                memory_id,
                MemoryStatus.EXPIRED,
                actor_role='admin',
                actor_id=actor_id,
                reason='retention_expired',
                scope=effective,
            )
            count += 1
        return count

    def conflicts(
        self,
        memory_id: str,
        *,
        scope: RepositoryScope | None = None,
    ) -> list[dict[str, Any]]:
        effective = self._effective_scope(scope)
        item = self.get(memory_id, scope=effective)
        if item is None or not item.conflict_key:
            return []
        rows = self.search(
            tenant_id=effective.tenant_id,
            actor_id=effective.actor_id,
            terminal_id=effective.terminal_id,
            statuses=('CANDIDATE', 'VERIFIED', 'DISPUTED'),
            namespace=item.namespace,
            subject_entity_id=item.subject_entity_id,
            predicate=item.predicate,
            limit=100,
        )
        return [
            row
            for row in rows
            if str(row.get('memory_id')) != item.memory_id and row.get('object') != item.object
        ]

    def transitions(
        self,
        memory_id: str,
        *,
        scope: RepositoryScope | None = None,
    ) -> list[dict[str, Any]]:
        effective = self._effective_scope(scope)
        item = self.get(memory_id, scope=effective)
        if item is None:
            return []
        with self.pool.connection(effective) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT schema_version,memory_id,from_status,to_status,actor_role,reviewer_id AS actor_id,reason,occurred_at "
                "FROM memory_transition WHERE memory_id=%s ORDER BY transition_id",
                (memory_id,),
            )
            columns = [d.name if hasattr(d, 'name') else d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

class PostgresContextRepository:
    def __init__(self,pool:Any,*,tenant_id:str='default',call_repository:Any=None,operation_repository:Any=None,command_repository:Any=None,event_journal:Any=None,memory_repository:Any=None):
        self.pool=pool; self.tenant_id=tenant_id; self.projection=PostgresProjectionRepository(pool,'context_snapshot'); self.call_repository=call_repository; self.operation_repository=operation_repository; self.command_repository=command_repository; self.event_journal=event_journal; self.memory_repository=memory_repository
    @staticmethod
    def _after_days(days:int)->str: return (datetime.now(timezone.utc)-timedelta(days=max(1,int(days)))).isoformat()
    def recent_calls(self,*,terminal_id:str,days:int=7,limit:int=20):
        if self.call_repository is None: return []
        return self.call_repository.list_sessions(CallSelector(authorized_terminal_id=terminal_id,started_after=self._after_days(days),limit=limit))
    def active_call(self,*,terminal_id:str):
        if self.call_repository is None:return None
        rows=self.call_repository.list_sessions(CallSelector(authorized_terminal_id=terminal_id,statuses=['observed','requested','ringing','answered','active'],limit=10)); return rows[0] if rows else None
    def recent_operations(self,*,actor_id:str,limit:int=30): return [] if self.operation_repository is None else self.operation_repository.query(actor_id=actor_id,limit=limit)
    def similar_outcomes(self,*,actor_id:str,operation_type:str,limit:int=20): return [] if self.operation_repository is None else self.operation_repository.query(actor_id=actor_id,operation_type=operation_type,limit=limit)
    def open_tasks(self,*,actor_id:str,limit:int=30):
        if self.command_repository is None:return []
        out=[]
        for status in ('CREATED','CLASSIFIED','PLANNED','RUNNING','WAITING'): out.extend(self.command_repository.query(actor_id=actor_id,status=status,limit=limit))
        out.sort(key=lambda x:(str(x.get('created_at') or ''),str(x.get('command_id') or '')),reverse=True); return out[:limit]
    def entity_timeline(self,*,entity_id:str,terminal_id:str,actor_id:str,limit:int=50):
        rows=[]
        if self.call_repository is not None:
            for x in self.call_repository.list_sessions(CallSelector(authorized_terminal_id=terminal_id,contact_id=entity_id,limit=limit)): rows.append({'kind':'call','occurred_at':x.get('requested_at') or x.get('created_at'),**x})
        if self.operation_repository is not None:
            for x in self.operation_repository.query(actor_id=actor_id,target_id=entity_id,limit=limit): rows.append({'kind':'operation','occurred_at':x.get('created_at'),**x})
        rows.sort(key=lambda x:str(x.get('occurred_at') or ''),reverse=True); return rows[:limit]
    def verified_memories(self,*,tenant_id:str|None,actor_id:str|None,terminal_id:str|None,query:str|None=None,limit:int=30,use_semantic:bool=False):
        return [] if self.memory_repository is None else self.memory_repository.search(tenant_id=tenant_id,actor_id=actor_id,terminal_id=terminal_id,query=query,statuses=('VERIFIED',),limit=limit,use_fts=False)
    def save_snapshot(self,snapshot:ContextSnapshot)->ContextSnapshot:
        payload=snapshot.model_dump(mode='json',exclude_none=True); self.projection.upsert(snapshot.snapshot_id,payload,scope=RepositoryScope(snapshot.tenant_id or self.tenant_id,snapshot.actor_id,snapshot.terminal_id)); return snapshot
    def get_snapshot(self,snapshot_id:str,*,actor_id:str|None=None,terminal_id:str|None=None,tenant_id:str|None=None)->ContextSnapshot|None:
        p=self.projection.get(snapshot_id,scope=RepositoryScope(tenant_id or self.tenant_id,actor_id,terminal_id)); return None if p is None else ContextSnapshot.model_validate({k:v for k,v in p.items() if k!='payload_hash'})
    def replay(self,snapshot_id:str,**scope:Any):
        s=self.get_snapshot(snapshot_id,**scope)
        if s is None:return None
        return {'snapshot_id':s.snapshot_id,'content_hash':s.content_hash,'recomputed_hash':s.context.content_hash(),'matches':s.content_hash==s.context.content_hash(),'context':s.context.planner_payload()}
