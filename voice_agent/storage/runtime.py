# =============================================================================
# 【维护注释】文件：voice_agent/storage/runtime.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：.interfaces
# 主要公开符号：StorageBackendMode, StorageRuntime, get_storage_runtime, reset_storage_runtime, mirror_record
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Rollback-safe storage backend router and dual-write shadow sink."""
from __future__ import annotations
import logging
import os
import threading
from enum import Enum
from typing import Any

from .interfaces import RepositoryScope, StorageHealth

logger=logging.getLogger(__name__)


class StorageBackendMode(str, Enum):
    SQLITE="sqlite"; DUAL="dual"; POSTGRES="postgres"


class StorageRuntime:
    def __init__(self, *, mode: str, postgres_dsn: str, pool_size: int = 8, fail_open: bool = True) -> None:
        normalized=str(mode).strip().lower()
        if normalized not in {x.value for x in StorageBackendMode}: raise ValueError("STORAGE_BACKEND must be sqlite|dual|postgres")
        self.mode=StorageBackendMode(normalized); self.postgres_dsn=str(postgres_dsn).strip(); self.pool_size=max(1,int(pool_size)); self.fail_open=bool(fail_open)
        self._pool=None; self._shadow=None; self.last_error: str|None=None

    @property
    def postgres_enabled(self) -> bool: return self.mode in {StorageBackendMode.DUAL,StorageBackendMode.POSTGRES}

    def _ensure_postgres(self) -> Any:
        if not self.postgres_enabled: return None
        if self._pool is None:
            if not self.postgres_dsn: raise ValueError("POSTGRES_DSN is required when STORAGE_BACKEND is dual/postgres")
            from .postgres.pool import PostgresPool
            from .postgres.shadow import PostgresShadowStore
            self._pool=PostgresPool(self.postgres_dsn,max_size=self.pool_size)
            self._shadow=PostgresShadowStore(self._pool, table="storage_projection" if self.mode is StorageBackendMode.POSTGRES else "storage_shadow_record")
        return self._pool

    @property
    def postgres_pool(self) -> Any | None:
        return self._ensure_postgres() if self.postgres_enabled else None

    @property
    def shadow(self) -> Any | None:
        if not self.postgres_enabled: return None
        self._ensure_postgres(); return self._shadow

    def mirror(self, *, record_type: str, record_id: str, payload: dict[str,Any], scope: RepositoryScope|None=None) -> str|None:
        if not self.postgres_enabled: return None
        try:
            self._ensure_postgres()
            return self._shadow.upsert_record(record_type=record_type,record_id=record_id,payload=payload,scope=scope)
        except Exception as exc:
            self.last_error=f"{type(exc).__name__}: {exc}"; logger.warning("postgres_shadow_write_failed type=%s id=%s error=%s",record_type,record_id,self.last_error)
            if self.mode is StorageBackendMode.POSTGRES and not self.fail_open: raise
            return None

    def health(self) -> StorageHealth:
        if not self.postgres_enabled: return StorageHealth(backend="sqlite",ok=True,detail="local")
        try:
            pool=self._ensure_postgres(); ok,detail=pool.health(); return StorageHealth(backend=self.mode.value,ok=ok,detail=detail)
        except Exception as exc: return StorageHealth(backend=self.mode.value,ok=False,detail=f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        if self._pool is not None: self._pool.close(); self._pool=None; self._shadow=None

_LOCK=threading.RLock(); _DEFAULT: StorageRuntime|None=None

def get_storage_runtime() -> StorageRuntime:
    global _DEFAULT
    if _DEFAULT is None:
        with _LOCK:
            if _DEFAULT is None:
                _DEFAULT=StorageRuntime(mode=os.environ.get("STORAGE_BACKEND","sqlite"),postgres_dsn=os.environ.get("POSTGRES_DSN",""),pool_size=int(os.environ.get("POSTGRES_POOL_SIZE","8")),fail_open=os.environ.get("POSTGRES_FAIL_OPEN","true").strip().lower() in {"1","true","yes","on"})
    return _DEFAULT

def reset_storage_runtime() -> None:
    global _DEFAULT
    with _LOCK:
        if _DEFAULT is not None: _DEFAULT.close()
        _DEFAULT=None

def mirror_record(record_type: str, record_id: str, payload: dict[str,Any], *, tenant_id: str|None=None, actor_id: str|None=None, terminal_id: str|None=None) -> str|None:
    effective_tenant = tenant_id or os.environ.get("CONTEXT_DEFAULT_TENANT_ID", "").strip() or "default"
    return get_storage_runtime().mirror(record_type=record_type,record_id=record_id,payload=payload,scope=RepositoryScope(effective_tenant,actor_id,terminal_id))
