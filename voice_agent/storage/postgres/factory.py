# =============================================================================
# 【维护注释】文件：voice_agent/storage/postgres/factory.py
# 所属层次：PostgreSQL 持久化适配层
# 主要职责：实现事件、检查点、投影等 PostgreSQL Repository，并与 SQLite/迁移模式保持可切换。
# 项目内直接依赖：voice_agent.storage.runtime, .repositories
# 主要公开符号：build_postgres_context_repositories
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations
from typing import Any
from voice_agent.storage.runtime import get_storage_runtime
from .repositories import PostgresCallRepository,PostgresOperationRepository,PostgresCommandRepository,PostgresMemoryRepository,PostgresContextRepository

def build_postgres_context_repositories(*,tenant_id:str='default') -> dict[str,Any]:
    pool=get_storage_runtime().postgres_pool
    if pool is None: raise RuntimeError('PostgreSQL storage is not enabled')
    calls=PostgresCallRepository(pool,tenant_id=tenant_id); ops=PostgresOperationRepository(pool,tenant_id=tenant_id); commands=PostgresCommandRepository(pool,tenant_id=tenant_id); memory=PostgresMemoryRepository(pool,tenant_id=tenant_id)
    context=PostgresContextRepository(pool,tenant_id=tenant_id,call_repository=calls,operation_repository=ops,command_repository=commands,memory_repository=memory)
    return {'pool':pool,'calls':calls,'operations':ops,'commands':commands,'memory':memory,'context':context}
