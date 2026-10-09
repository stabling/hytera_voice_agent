# =============================================================================
# 【维护注释】文件：voice_agent/reasoning/models.py
# 所属层次：语义规划层
# 主要职责：把自然语言请求转换为受 CapabilityRegistry 约束的能力计划；模型只负责选择和填参，最终校验始终由服务端执行。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：CapabilityStep, CapabilityPlanProposal
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

PlanDecision = Literal["EXECUTE", "NO_CAPABILITY", "NEEDS_CLARIFICATION"]


class CapabilityStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    capability: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_dependencies(self) -> "CapabilityStep":
        if self.id in self.depends_on:
            raise ValueError("step cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on contains duplicates")
        return self


class CapabilityPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: PlanDecision
    goal: str = Field(default="", max_length=2000)
    steps: list[CapabilityStep] = Field(default_factory=list, max_length=16)
    clarification: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_decision(self) -> "CapabilityPlanProposal":
        if self.decision == "EXECUTE" and not self.steps:
            raise ValueError("EXECUTE requires at least one step")
        if self.decision != "EXECUTE" and self.steps:
            raise ValueError("non-execute decision cannot contain steps")
        if self.decision == "NEEDS_CLARIFICATION" and not str(self.clarification or "").strip():
            raise ValueError("NEEDS_CLARIFICATION requires clarification")
        return self
