# =============================================================================
# 【维护注释】文件：voice_agent/storage/postgres/pool.py
# 所属层次：PostgreSQL 持久化适配层
# 主要职责：实现事件、检查点、投影等 PostgreSQL Repository，并与 SQLite/迁移模式保持可切换。
# 项目内直接依赖：voice_agent.storage.interfaces
# 主要公开符号：PostgresUnavailable, PostgresPool
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Lazy psycopg3 connection-pool wrapper with RLS session scoping."""
from __future__ import annotations
from contextlib import contextmanager
from typing import Any, Iterator

from voice_agent.storage.interfaces import RepositoryScope


class PostgresUnavailable(RuntimeError):
    pass


class PostgresPool:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8, timeout: float = 10.0, application_name: str = "hytera-voice-agent") -> None:
        self.dsn = str(dsn).strip()
        if not self.dsn:
            raise ValueError("POSTGRES_DSN is required")
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise PostgresUnavailable("PostgreSQL backend requires psycopg[binary] and psycopg-pool") from exc
        kwargs = {"autocommit": False, "application_name": str(application_name)}
        self._pool = ConnectionPool(conninfo=self.dsn, min_size=max(1,int(min_size)), max_size=max(1,int(max_size)), timeout=float(timeout), kwargs=kwargs, open=False)
        self._pool.open(wait=True)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self, scope: RepositoryScope | None = None) -> Iterator[Any]:
        with self._pool.connection() as conn:
            normalized = (scope or RepositoryScope()).normalized()
            with conn.cursor() as cur:
                # set_config(..., true) is transaction-local, preventing pooled-connection tenant leakage.
                for name, value in (("app.tenant_id", normalized.tenant_id), ("app.actor_id", normalized.actor_id), ("app.terminal_id", normalized.terminal_id)):
                    cur.execute("SELECT set_config(%s, %s, true)", (name, value or ""))
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def health(self) -> tuple[bool, str]:
        try:
            with self.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
            return bool(row and row[0] == 1), "ok"
        except Exception as exc:  # pragma: no cover - live database boundary
            return False, f"{type(exc).__name__}: {exc}"
