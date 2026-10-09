# =============================================================================
# 【维护注释】文件：voice_agent/approvals/models.py
# 所属层次：人工审批层
# 主要职责：负责高风险操作的审批请求、不可变审批事件和审核结果持久化。
# 项目内直接依赖：shared_contracts.models
# 主要公开符号：Approval
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Human approval persistence contracts."""
from __future__ import annotations

import uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from shared_contracts.models import utc_now_iso

ApprovalStatus = Literal["PENDING", "APPROVED", "EDITED", "REJECTED", "EXPIRED", "CONSUMED"]


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1)
    approval_id: str = Field(default_factory=lambda: "apr_" + uuid.uuid4().hex, min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=128)
    origin_command_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=256)
    plan_hash: str = Field(min_length=1, max_length=160)
    policy_id: str = Field(min_length=1, max_length=128)
    risk_level: str = Field(min_length=2, max_length=2)
    status: ApprovalStatus = "PENDING"
    requested_payload: dict[str, Any] = Field(default_factory=dict)
    approved_payload: dict[str, Any] | None = None
    reviewer_id: str | None = Field(default=None, max_length=128)
    reviewer_role: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=2000)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
    updated_at: str = Field(default_factory=utc_now_iso, max_length=64)
