# =============================================================================
# 【维护注释】文件：voice_agent/planning/planner.py
# 所属层次：计划治理层
# 主要职责：负责计划模型、校验、持久化、策略衔接、等待条件和计划执行前治理。
# 项目内直接依赖：voice_agent.capabilities, voice_agent.reasoning, .models, .validator
# 主要公开符号：CapabilityPlanner
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Semantic capability planner adapter."""
from __future__ import annotations

from typing import Any

from voice_agent.capabilities import CapabilityRegistry
from voice_agent.reasoning import CapabilityPlanProposal, SemanticPlanner

from .models import OperationPlan, PlanLimits, PlanStep
from .validator import PlanValidator


class CapabilityPlanner:
    """Turn an unconstrained natural-language request into a validated capability plan."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        validator: PlanValidator,
        *,
        semantic_planner: SemanticPlanner | None = None,
        planner_id: str = "semantic",
    ) -> None:
        self.registry = registry
        self.validator = validator
        self.semantic_planner = semantic_planner or SemanticPlanner(
            registry, max_steps=validator.max_steps
        )
        self.planner_id = planner_id

    def propose(
        self,
        text: str,
        *,
        origin_command_id: str,
        context: dict[str, Any] | None = None,
        context_snapshot_id: str | None = None,
        context_snapshot_hash: str | None = None,
        limits: PlanLimits | None = None,
    ) -> tuple[CapabilityPlanProposal, OperationPlan | None]:
        proposal = self.semantic_planner.plan(text, context=context)
        if proposal.decision != "EXECUTE":
            return proposal, None
        plan_limits = limits or PlanLimits()
        plan = OperationPlan(
            origin_command_id=origin_command_id,
            goal=proposal.goal or str(text).strip(),
            steps=[
                PlanStep(
                    step_id=step.id,
                    capability=step.capability,
                    arguments=dict(step.arguments),
                    depends_on=list(step.depends_on),
                )
                for step in proposal.steps
            ],
            limits=plan_limits,
            context_snapshot_id=context_snapshot_id,
            context_snapshot_hash=context_snapshot_hash,
            capability_catalog_hash=self.registry.catalog_hash,
            planner_id=self.planner_id,
            model_call_count=1,
        )
        return proposal, self.validator.validate(plan)

