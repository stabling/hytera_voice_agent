# =============================================================================
# 【维护注释】文件：shared_contracts/__init__.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：.event_models, .models, .routes
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Shared, serializable contracts for the x86 gateway and x64 agent."""

from .event_models import DomainEvent, EventEnvelope, EventQuery
from .models import ProcessHealth, VoiceEvent, VoiceEventAck
from .routes import *

__all__ = [
    "DomainEvent",
    "EventEnvelope",
    "EventQuery",
    "ProcessHealth",
    "VoiceEvent",
    "VoiceEventAck",
]
