# =============================================================================
# 【维护注释】文件：voice_agent/storage/postgres/checkpoint_repository.py
# 所属层次：PostgreSQL 持久化适配层
# 主要职责：实现事件、检查点、投影等 PostgreSQL Repository，并与 SQLite/迁移模式保持可切换。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：PostgresCheckpointProvider
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""PostgreSQL-backed LangGraph checkpoint provider.

The business repository layer does not reimplement LangGraph's checkpoint schema.
It delegates to the official ``langgraph-checkpoint-postgres`` saver so persisted
interrupt/resume semantics remain compatible with the graph runtime.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from typing import Any


class PostgresCheckpointProvider:
    """Own the lifetime of an official PostgresSaver and initialize its schema."""

    def __init__(self, dsn: str, *, setup: bool = True) -> None:
        self.dsn = str(dsn).strip()
        if not self.dsn:
            raise ValueError("POSTGRES_DSN is required for PostgreSQL checkpoints")
        # Safe default documented by the checkpoint package for persisted data.
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError(
                "PostgreSQL checkpoints require langgraph-checkpoint-postgres"
            ) from exc
        self._stack = ExitStack()
        self.saver: Any = self._stack.enter_context(PostgresSaver.from_conn_string(self.dsn))
        if setup:
            self.saver.setup()

    def close(self) -> None:
        self._stack.close()

    def __enter__(self) -> "PostgresCheckpointProvider":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
