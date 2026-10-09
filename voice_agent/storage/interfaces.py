# =============================================================================
# 【维护注释】文件：voice_agent/storage/interfaces.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：RepositoryScope, StorageHealth, EventRepositoryProtocol, CallRepositoryProtocol, OperationRepositoryProtocol, MemoryRepositoryProtocol, ContextSnapshotRepositoryProtocol, CheckpointRepositoryProtocol, ShadowRecordSink
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Stable repository protocols used to keep business code storage-agnostic."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class RepositoryScope:
    tenant_id: str | None = None
    actor_id: str | None = None
    terminal_id: str | None = None

    def normalized(self) -> "RepositoryScope":
        def clean(value: str | None) -> str | None:
            if value is None:
                return None
            text = str(value).strip()
            return text or None
        return RepositoryScope(clean(self.tenant_id), clean(self.actor_id), clean(self.terminal_id))


@dataclass(frozen=True)
class StorageHealth:
    backend: str
    ok: bool
    detail: str = ""


@runtime_checkable
class EventRepositoryProtocol(Protocol):
    def append(self, event: Any) -> Any: ...
    def get(self, event_id: str, *, redacted: bool = True) -> dict[str, Any] | None: ...
    def query(self, query: Any) -> list[dict[str, Any]]: ...


@runtime_checkable
class CallRepositoryProtocol(Protocol):
    def get_call(self, call_id: str, *, include_transcript: bool = True) -> dict[str, Any] | None: ...
    def list_sessions(self, selector: Any) -> list[dict[str, Any]]: ...


@runtime_checkable
class OperationRepositoryProtocol(Protocol):
    def get(self, operation_id: str) -> Any | None: ...
    def query(self, **filters: Any) -> list[dict[str, Any]]: ...


@runtime_checkable
class MemoryRepositoryProtocol(Protocol):
    def get(self, memory_id: str) -> Any | None: ...
    def search(self, **filters: Any) -> list[dict[str, Any]]: ...


@runtime_checkable
class ContextSnapshotRepositoryProtocol(Protocol):
    def save_snapshot(self, snapshot: Any) -> Any: ...
    def get_snapshot(self, snapshot_id: str, **scope: Any) -> Any | None: ...




@runtime_checkable
class CheckpointRepositoryProtocol(Protocol):
    """Subset of the LangGraph saver contract consumed by the application."""

    def get_tuple(self, config: Mapping[str, Any]) -> Any | None: ...
    def list(self, config: Mapping[str, Any] | None, **kwargs: Any) -> Any: ...
    def put(self, config: Mapping[str, Any], checkpoint: Mapping[str, Any], metadata: Mapping[str, Any], new_versions: Mapping[str, Any]) -> Mapping[str, Any]: ...

@runtime_checkable
class ShadowRecordSink(Protocol):
    def upsert_record(self, *, record_type: str, record_id: str, payload: Mapping[str, Any], scope: RepositoryScope | None = None) -> str: ...
