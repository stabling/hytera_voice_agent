# =============================================================================
# 【维护注释】文件：voice_agent/memory/verifier.py
# 所属层次：长期记忆层
# 主要职责：负责记忆候选、检索、验证、向量/全文索引与生命周期；记忆不能直接触发设备命令。
# 项目内直接依赖：.models
# 主要公开符号：MemoryVerifier
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Human-governed evidence, conflict and lifecycle review."""

from __future__ import annotations

from typing import Any

from .models import MemoryStatus


class MemoryVerifier:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def review(
        self,
        memory_id: str,
        *,
        decision: str,
        actor_role: str,
        actor_id: str | None,
        reason: str,
        scope: Any | None = None,
    ) -> dict[str, Any]:
        target = MemoryStatus(str(decision).upper())
        conflicts = self.repository.conflicts(memory_id, scope=scope)
        if target == MemoryStatus.VERIFIED and conflicts:
            raise ValueError("memory has unresolved conflicting values")
        item = self.repository.transition(
            memory_id, target, actor_role=actor_role, actor_id=actor_id, reason=reason, scope=scope
        )
        return {
            "schema_version": 1,
            "memory": item.model_dump(mode="json"),
            "conflicts": conflicts,
            "transitions": self.repository.transitions(memory_id, scope=scope),
        }

    def invalidate_evidence(
        self,
        memory_id: str,
        *,
        actor_role: str,
        actor_id: str | None,
        reason: str,
        scope: Any | None = None,
    ) -> dict[str, Any]:
        item = self.repository.transition(
            memory_id, MemoryStatus.DISPUTED,
            actor_role=actor_role, actor_id=actor_id,
            reason=f"evidence_invalidated:{reason}", scope=scope,
        )
        return {"schema_version": 1, "memory": item.model_dump(mode="json")}
