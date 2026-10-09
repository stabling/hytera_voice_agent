# =============================================================================
# 【维护注释】文件：shared_contracts/event_publisher.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：.event_models
# 主要公开符号：PublishResult, EventPublisherProtocol
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Small shared publisher protocol for gateway, agent and MCP boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .event_models import DomainEvent


@dataclass(frozen=True)
class PublishResult:
    event_id: str
    published: bool
    duplicate: bool = False
    disabled: bool = False
    error: str | None = None


class EventPublisherProtocol(Protocol):
    def publish(self, event: DomainEvent) -> PublishResult: ...
