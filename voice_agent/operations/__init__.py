# =============================================================================
# 【维护注释】文件：voice_agent/operations/__init__.py
# 所属层次：原子操作执行层
# 主要职责：负责把计划步骤固化为可审计、可幂等、可重试的原子操作并记录执行结果。
# 项目内直接依赖：.models, .repository
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""runtime."""

from .models import (
    AtomicOperation,
    OperationAttempt,
    OperationPlan,
    OperationResult,
    WaitCondition,
    build_idempotency_key,
)
from .repository import IdempotencyConflictError, OperationRepository

__all__ = [
    "AtomicOperation",
    "OperationAttempt",
    "OperationPlan",
    "OperationResult",
    "WaitCondition",
    "build_idempotency_key",
    "IdempotencyConflictError",
    "OperationRepository",
]
