# =============================================================================
# 【维护注释】文件：voice_agent/agent/checkpointing.py
# 所属层次：LangGraph 编排层
# 主要职责：负责状态图、节点、依赖绑定、检查点和工作流路由；业务能力通过注册表和 runtime 注入，不直接硬编码设备协议。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：CheckpointerRuntime
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Optional official LangGraph checkpointer construction and lifetime management."""

from __future__ import annotations

import os
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from typing import Any


class CheckpointerRuntime:
    def __init__(self, *, backend: str, sqlite_path: str | Path, postgres_dsn: str = "") -> None:
        self.backend = str(backend).strip().lower()
        self.sqlite_path = Path(sqlite_path).expanduser().resolve(strict=False)
        self.postgres_dsn = str(postgres_dsn).strip() or os.environ.get("POSTGRES_DSN", "").strip()
        self._stack = ExitStack()
        self._connection: sqlite3.Connection | None = None
        self.checkpointer: Any | None = None
        self._initialize()

    def _initialize(self) -> None:
        if self.backend == "off":
            return
        # Official checkpoint packages recommend strict MessagePack allow-listing
        # when persisted databases could be modified outside the process.
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        if self.backend == "sqlite":
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
            except ImportError as exc:  # pragma: no cover - production dependency boundary
                raise RuntimeError(
                    "WORKING_MEMORY_BACKEND=sqlite requires langgraph-checkpoint-sqlite"
                ) from exc
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(self.sqlite_path), check_same_thread=False)
            self.checkpointer = SqliteSaver(self._connection)
            return
        if self.backend == "postgres":
            if not self.postgres_dsn:
                raise ValueError("WORKING_MEMORY_POSTGRES_DSN is required for postgres")
            try:
                from langgraph.checkpoint.postgres import PostgresSaver
            except ImportError as exc:  # pragma: no cover - production dependency boundary
                raise RuntimeError(
                    "WORKING_MEMORY_BACKEND=postgres requires langgraph-checkpoint-postgres"
                ) from exc
            self.checkpointer = self._stack.enter_context(
                PostgresSaver.from_conn_string(self.postgres_dsn)
            )
            self.checkpointer.setup()
            return
        raise ValueError(f"unsupported working-memory backend: {self.backend}")

    @property
    def enabled(self) -> bool:
        return self.checkpointer is not None

    def close(self) -> None:
        self._stack.close()
        if self._connection is not None:
            self._connection.close()
            self._connection = None
