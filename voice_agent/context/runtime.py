# =============================================================================
# 【维护注释】文件：voice_agent/context/runtime.py
# 所属层次：决策上下文层
# 主要职责：负责事件、历史、终端状态等上下文的构建、裁剪、脱敏和快照，为规划器提供受控信息。
# 项目内直接依赖：voice_agent, voice_agent.calls.repository, voice_agent.commands.repository, voice_agent.memory.repository, voice_agent.memory.verifier, voice_agent.operations.repository, shared_runtime.event_journal, .builder
# 主要公开符号：ContextMemoryRuntime, get_default_context_memory_runtime, reset_default_context_memory_runtime
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Configuration-backed runtime/memory runtime for the x64 processes."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from voice_agent import config
from voice_agent.calls.repository import CallStoreRepository
from voice_agent.commands.repository import CommandRepository
from voice_agent.memory.repository import MemoryRepository
from voice_agent.memory.verifier import MemoryVerifier
from voice_agent.operations.repository import OperationRepository
from shared_runtime.event_journal import EventJournal

from .builder import ContextBuilder
from .history import HistoryQueryService
from .repository import ContextRepository


@dataclass(frozen=True)
class ContextMemoryRuntime:
    context_repository: Any
    memory_repository: Any
    context_builder: ContextBuilder
    history_service: HistoryQueryService
    memory_verifier: MemoryVerifier


_LOCK = threading.RLock()
_DEFAULT: ContextMemoryRuntime | None = None

# 归档权限视图的惰性缓存：避免装配期引入 messages 层静态依赖。
_USER_HISTORY_CACHE: Any = None


def _archive_call_access(actor_terminal_id: str, call_id: str) -> bool | None:
    """以消息归档库为权限视图判断 call_id 可访问性（需求 §22）。

    供 HistoryQueryService 剔除控制通话；惰性解析 messages 层单例，
    归档库不可用时返回 None（CallStore 旧数据兼容放行）。
    """
    global _USER_HISTORY_CACHE
    try:
        from voice_agent.messages import UserHistoryService, get_message_archive_service

        if _USER_HISTORY_CACHE is None:
            service = get_message_archive_service()
            _USER_HISTORY_CACHE = UserHistoryService(service.store, service.metrics)
        return _USER_HISTORY_CACHE.call_access(actor_terminal_id, call_id)
    except Exception:
        return None


def get_default_context_memory_runtime() -> ContextMemoryRuntime:
    global _DEFAULT
    if _DEFAULT is None:
        with _LOCK:
            if _DEFAULT is None:
                if config.STORAGE_BACKEND == "postgres":
                    from voice_agent.storage.postgres.factory import build_postgres_context_repositories
                    repositories = build_postgres_context_repositories(
                        tenant_id=config.CONTEXT_DEFAULT_TENANT_ID or "default"
                    )
                    memory = repositories["memory"]
                    context_repository = repositories["context"]
                else:
                    # sqlite and dual write through the proven local path; dual canary reads are
                    # deterministic and automatically fall back to SQLite on PostgreSQL errors.
                    memory = MemoryRepository(config.MEMORY_STORE_PATH)
                    local_context = ContextRepository(
                        config.CONTEXT_STORE_PATH,
                        call_repository=CallStoreRepository(config.CALL_STORE_PATH),
                        operation_repository=OperationRepository(config.OPERATION_STORE_PATH),
                        command_repository=CommandRepository(config.OPERATION_STORE_PATH),
                        event_journal=EventJournal(config.EVENT_JOURNAL_PATH),
                        memory_repository=memory,
                    )
                    context_repository = local_context
                    if config.STORAGE_BACKEND == "dual" and config.STORAGE_READ_PERCENT_POSTGRES > 0:
                        from voice_agent.storage.postgres.factory import build_postgres_context_repositories
                        from voice_agent.storage.read_canary import CanaryReadProxy
                        secondary = build_postgres_context_repositories(tenant_id=config.CONTEXT_DEFAULT_TENANT_ID or "default")["context"]
                        context_repository = CanaryReadProxy(local_context, secondary, percent=config.STORAGE_READ_PERCENT_POSTGRES)
                _DEFAULT = ContextMemoryRuntime(
                    context_repository=context_repository,
                    memory_repository=memory,
                    context_builder=ContextBuilder(
                        context_repository,
                        semantic_enabled=config.MEMORY_FTS_ENABLE or config.VECTOR_SEARCH_ENABLE,
                    ),
                    history_service=HistoryQueryService(
                        context_repository, max_limit=config.CONTEXT_QUERY_MAX_LIMIT,
                        call_access=_archive_call_access,
                    ),
                    memory_verifier=MemoryVerifier(memory),
                )
    return _DEFAULT


def reset_default_context_memory_runtime() -> None:
    global _DEFAULT
    with _LOCK:
        _DEFAULT = None
