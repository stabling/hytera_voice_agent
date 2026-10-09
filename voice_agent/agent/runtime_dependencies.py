# =============================================================================
# 【维护注释】文件：voice_agent/agent/runtime_dependencies.py
# 所属层次：LangGraph 编排层
# 主要职责：负责状态图、节点、依赖绑定、检查点和工作流路由；业务能力通过注册表和 runtime 注入，不直接硬编码设备协议。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：set_dependencies, reset_dependencies, get_dependencies
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Per-invocation dependency context, excluded from persisted LangGraph state."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_CURRENT_DEPENDENCIES: ContextVar[Any | None] = ContextVar("voice_agent_dependencies", default=None)


def set_dependencies(value: Any) -> Token[Any | None]:
    return _CURRENT_DEPENDENCIES.set(value)


def reset_dependencies(token: Token[Any | None]) -> None:
    _CURRENT_DEPENDENCIES.reset(token)


def get_dependencies() -> Any | None:
    return _CURRENT_DEPENDENCIES.get()
