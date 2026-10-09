# =============================================================================
# 【维护注释】文件：voice_agent/planning/waits.py
# 所属层次：计划治理层
# 主要职责：负责计划模型、校验、持久化、策略衔接、等待条件和计划执行前治理。
# 项目内直接依赖：voice_agent.operations.repository
# 主要公开符号：WaitRegistry
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Exact-subject wait registry. No fuzzy or 'next call' matching is permitted."""
from __future__ import annotations

import hashlib
from typing import Any
from voice_agent.operations.repository import OperationRepository


def _stable_wait_id(*, operation_id: str, subject_type: str, subject_id: str, predicate: str) -> str:
    raw = "\x1f".join((str(operation_id), str(subject_type), str(subject_id), str(predicate))).encode("utf-8")
    return "wait_" + hashlib.sha256(raw).hexdigest()[:40]


class WaitRegistry:
    def __init__(self, repository: OperationRepository) -> None:
        self.repository = repository

    def register(self, *, operation_id: str, subject_type: str, subject_id: str, predicate: str, timeout_at: str | None = None):
        if not str(subject_id).strip():
            raise ValueError("wait subject_id must be explicit")
        from voice_agent.operations.models import WaitCondition
        condition = WaitCondition(
            wait_id=_stable_wait_id(operation_id=operation_id, subject_type=subject_type, subject_id=str(subject_id), predicate=predicate),
            operation_id=operation_id,
            subject_type=subject_type,
            subject_id=str(subject_id),
            predicate=predicate,
            timeout_at=timeout_at,
        )
        self.repository.add_wait(condition)
        return condition

    def dispatch(self, *, subject_type: str, subject_id: str, predicate: str, event: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not str(subject_id).strip():
            raise ValueError("wait subject_id must be explicit")
        return self.repository.satisfy_waits(subject_type=subject_type, subject_id=str(subject_id), predicate=predicate, event=event or {})
