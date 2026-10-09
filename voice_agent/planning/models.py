# =============================================================================
# 【维护注释】文件：voice_agent/planning/models.py
# 所属层次：计划治理层
# 主要职责：负责计划模型、校验、持久化、策略衔接、等待条件和计划执行前治理。
# 项目内直接依赖：shared_contracts.models
# 主要公开符号：stable_plan_hash, PlanStep, PlanLimits, OperationPlan
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared_contracts.models import utc_now_iso
from shared_runtime.json_utils import canonical_json

PlanStatus = Literal["PROPOSED", "VALIDATED", "POLICY_PENDING", "APPROVAL_PENDING", "READY", "RUNNING", "WAITING", "DISPATCHED", "SUCCEEDED", "FAILED", "REJECTED", "CANCELLED", "TIMED_OUT"]
FailureStrategy = Literal["fail_plan", "continue", "replan"]
RiskLevel = Literal["R0", "R1", "R2", "R3", "R4", "R5"]


def stable_plan_hash(plan: "OperationPlan") -> str:
    data = plan.model_dump(mode="json", exclude={"content_hash", "updated_at"})
    return "sha256:" + hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    step_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    capability: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    failure_strategy: FailureStrategy = "fail_plan"
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _deps(self) -> "PlanStep":
        if self.step_id in self.depends_on:
            raise ValueError("step cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on contains duplicates")
        return self


class PlanLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_steps: int = Field(default=8, ge=1, le=64)
    max_replans: int = Field(default=2, ge=0, le=10)
    max_model_calls: int = Field(default=3, ge=0, le=20)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)


class OperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    decision_id: str = Field(default_factory=lambda: "dec_" + uuid.uuid4().hex, min_length=1, max_length=128)
    origin_command_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=2000)
    steps: list[PlanStep] = Field(default_factory=list, min_length=1, max_length=64)
    risk_level: RiskLevel = "R1"
    approval_required: bool = False
    limits: PlanLimits = Field(default_factory=PlanLimits)
    context_snapshot_id: str | None = Field(default=None, max_length=128)
    context_snapshot_hash: str | None = Field(default=None, max_length=160)
    capability_catalog_hash: str | None = Field(default=None, max_length=80)
    planner_id: str = Field(default="semantic", max_length=128)
    planner_model: str | None = Field(default=None, max_length=160)
    policy_id: str | None = Field(default=None, max_length=128)
    status: PlanStatus = "PROPOSED"
    replan_count: int = Field(default=0, ge=0, le=10)
    model_call_count: int = Field(default=0, ge=0, le=20)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
    updated_at: str = Field(default_factory=utc_now_iso, max_length=64)
    content_hash: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def _finalize(self) -> "OperationPlan":
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step_id must be unique within a plan")
        if len(self.steps) > self.limits.max_steps:
            raise ValueError("plan exceeds max_steps")
        if not self.content_hash:
            self.content_hash = stable_plan_hash(self)
        return self
