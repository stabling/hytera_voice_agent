# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/execution_context.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：current_execution_context, use_execution_context
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_CURRENT: ContextVar[dict[str, Any]] = ContextVar("mcp_device_execution_context", default={})


def current_execution_context() -> dict[str, Any]:
    return dict(_CURRENT.get() or {})


@contextmanager
def use_execution_context(value: dict[str, Any] | None) -> Iterator[None]:
    token = _CURRENT.set(dict(value or {}))
    try:
        yield
    finally:
        _CURRENT.reset(token)
