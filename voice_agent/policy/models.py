# =============================================================================
# 【维护注释】文件：voice_agent/policy/models.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.models
# 主要公开符号：AuthorizationDecision
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Policy and approval decision contracts."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from shared_contracts.models import utc_now_iso

RiskLevel = Literal["R0", "R1", "R2", "R3", "R4", "R5"]
Decision = Literal["AUTHORIZE", "APPROVAL_REQUIRED", "DENY"]


class AuthorizationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1)
    decision_id: str = Field(min_length=1, max_length=128)
    decision: Decision
    audit_decision: Decision | None = None
    rule_id: str = Field(min_length=1, max_length=160)
    policy_id: str = Field(min_length=1, max_length=128)
    risk_level: RiskLevel
    allowed_operations: list[str] = Field(default_factory=list)
    denied_operations: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=2000)
    approval_required: bool = False
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
