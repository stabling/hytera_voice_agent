# =============================================================================
# 【维护注释】文件：voice_agent/approvals/service.py
# 所属层次：人工审批层
# 主要职责：负责高风险操作的审批请求、不可变审批事件和审核结果持久化。
# 项目内直接依赖：voice_agent.planning.models, voice_agent.planning.validator, voice_agent.policy.engine, .models, .repository
# 主要公开符号：ApprovalService
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Approval orchestration and edit re-validation boundary."""
from __future__ import annotations

from typing import Any
from voice_agent.planning.models import OperationPlan
from voice_agent.planning.validator import PlanValidator
from voice_agent.policy.engine import PolicyEngine
from .models import Approval
from .repository import ApprovalRepository


class ApprovalService:
    def __init__(
        self, repository: ApprovalRepository, validator: PlanValidator, policy: PolicyEngine,
        *, allowed_roles: Any = ("approver", "admin"),
    ) -> None:
        self.repository = repository
        self.validator = validator
        self.policy = policy
        self.allowed_roles = frozenset(str(role).strip().lower() for role in allowed_roles if str(role).strip())
        if not self.allowed_roles:
            raise ValueError("at least one approval role must be configured")

    def request(self, plan: OperationPlan, *, thread_id: str) -> Approval:
        return self.repository.create(Approval(
            decision_id=plan.decision_id,
            origin_command_id=plan.origin_command_id,
            thread_id=thread_id,
            plan_hash=str(plan.content_hash),
            policy_id=self.policy.policy_id,
            risk_level=plan.risk_level,
            requested_payload=plan.model_dump(mode="json"),
        ))

    def decide(self, approval_id: str, *, action: str, actor_id: str, actor_role: str, reason: str | None = None, edited_payload: dict[str, Any] | None = None) -> Approval:
        if str(actor_role).strip().lower() not in self.allowed_roles:
            raise PermissionError("reviewer role is not authorized for approval state transitions")
        if action == "edit":
            current = self.repository.get(approval_id)
            if current is None:
                raise KeyError("approval not found")
            original = OperationPlan.model_validate(current.requested_payload)
            edited = OperationPlan.model_validate(edited_payload or {})
            immutable_pairs = {
                "decision_id": (edited.decision_id, original.decision_id),
                "origin_command_id": (edited.origin_command_id, original.origin_command_id),
                "context_snapshot_id": (edited.context_snapshot_id, original.context_snapshot_id),
                "context_snapshot_hash": (edited.context_snapshot_hash, original.context_snapshot_hash),
                "capability_catalog_hash": (edited.capability_catalog_hash, original.capability_catalog_hash),
            }
            changed = [name for name, pair in immutable_pairs.items() if pair[0] != pair[1]]
            if changed:
                raise ValueError("approval edit cannot change immutable plan identity/context fields: " + ",".join(changed))
            original_structure = [
                (step.step_id, step.capability, tuple(step.depends_on), step.failure_strategy)
                for step in original.steps
            ]
            edited_structure = [
                (step.step_id, step.capability, tuple(step.depends_on), step.failure_strategy)
                for step in edited.steps
            ]
            if edited_structure != original_structure:
                raise ValueError("approval edit may change step arguments only; plan topology and operation types are immutable")
            if edited.limits != original.limits:
                raise ValueError("approval edit cannot expand or alter plan execution limits")
            validated = self.validator.validate(edited)
            policy = self.policy.evaluate(validated, actor_id=actor_id, actor_roles=[actor_role])
            if policy.audit_decision == "DENY" or policy.decision == "DENY":
                raise PermissionError("edited plan is denied by policy")
            edited_payload = validated.model_dump(mode="json")
        return self.repository.decide(approval_id, action=action, actor_id=actor_id, actor_role=actor_role, reason=reason, edited_payload=edited_payload)
