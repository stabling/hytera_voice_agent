# =============================================================================
# 【维护注释】文件：voice_agent/operations/models.py
# 所属层次：原子操作执行层
# 主要职责：负责把计划步骤固化为可审计、可幂等、可重试的原子操作并记录执行结果。
# 项目内直接依赖：shared_contracts.identifiers, shared_contracts.models
# 主要公开符号：build_idempotency_key, AtomicOperation, OperationPlan, WaitCondition, OperationAttempt, OperationResult
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Atomic operation, attempt, wait-condition, and layered result contracts."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared_contracts.identifiers import new_operation_id
from shared_contracts.models import utc_now_iso
from shared_runtime.json_utils import canonical_json

OperationStatus = Literal[
    "CREATED",
    "READY",
    "RUNNING",
    "ACCEPTED",
    "DISPATCHED",
    "DEVICE_CONFIRMED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "UNKNOWN",
]
OPERATION_STATUSES = {"CREATED", "READY", "RUNNING", "ACCEPTED", "DISPATCHED", "DEVICE_CONFIRMED", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "UNKNOWN"}
RiskLevel = Literal["R0", "R1", "R2", "R3", "R4", "R5"]
ExecutorKind = Literal["mcp", "local", "query", "content", "delivery"]
WaitSubjectType = Literal["call_id", "operation_id", "reminder_id"]
WaitStatus = Literal["WAITING", "SATISFIED", "TIMED_OUT", "CANCELLED"]


def build_idempotency_key(
    *,
    origin_command_id: str,
    step_id: str,
    target_type: str | None,
    target_id: str | None,
    arguments: dict[str, Any],
    operation_type: str,
) -> str:
    """Build the stable command/step/target/arguments idempotency boundary."""

    payload = {
        "origin_command_id": str(origin_command_id),
        "step_id": str(step_id),
        "operation_type": str(operation_type),
        "target_type": str(target_type or ""),
        "target_id": str(target_id or ""),
        "arguments": arguments,
    }
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class AtomicOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str = Field(default_factory=new_operation_id, min_length=1, max_length=128)
    origin_command_id: str = Field(min_length=1, max_length=128)
    operation_type: str = Field(min_length=1, max_length=160)
    step_id: str = Field(default="step-1", min_length=1, max_length=128)
    actor_id: str | None = Field(default=None, max_length=128)
    target_type: str | None = Field(default=None, max_length=64)
    target_id: str | None = Field(default=None, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    resolved_arguments: dict[str, Any] = Field(default_factory=dict)
    executor_kind: ExecutorKind = "mcp"
    tool_name: str | None = Field(default=None, max_length=160)
    risk_level: RiskLevel = "R1"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    status: OperationStatus = "READY"
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_retries: int = Field(default=0, ge=0, le=20)
    side_effect: bool = False
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
    updated_at: str = Field(default_factory=utc_now_iso, max_length=64)
    completed_at: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _fill_idempotency_key(self) -> "AtomicOperation":
        if not self.idempotency_key:
            self.idempotency_key = build_idempotency_key(
                origin_command_id=self.origin_command_id,
                step_id=self.step_id,
                operation_type=self.operation_type,
                target_type=self.target_type,
                target_id=self.target_id,
                arguments=self.resolved_arguments or self.arguments,
            )
        return self


class OperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    origin_command_id: str = Field(min_length=1, max_length=128)
    operations: list[AtomicOperation] = Field(default_factory=list, max_length=64)


class WaitCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    wait_id: str = Field(default_factory=lambda: f"wait_{new_operation_id()[3:]}", min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    subject_type: WaitSubjectType
    subject_id: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    status: WaitStatus = "WAITING"
    timeout_at: str | None = Field(default=None, max_length=64)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
    satisfied_at: str | None = Field(default=None, max_length=64)


class OperationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    attempt_id: str = Field(min_length=1, max_length=160)
    operation_id: str = Field(min_length=1, max_length=128)
    attempt_no: int = Field(ge=1)
    executor_mode: str | None = Field(default=None, max_length=64)
    started_at: str = Field(default_factory=utc_now_iso, max_length=64)
    ended_at: str | None = Field(default=None, max_length=64)
    accepted: bool | None = None
    dispatched: bool | None = None
    device_confirmed: bool | None = None
    response: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=160)
    error_message: str | None = Field(default=None, max_length=2000)


class OperationResult(BaseModel):
    """Layered outcome; binary dispatch may succeed without device confirmation."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str = Field(min_length=1, max_length=128)
    ok: bool
    status: OperationStatus
    accepted: bool = False
    dispatched: bool = False
    device_confirmed: bool = False
    final_result: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None
    cached: bool = False
    completed_at: str = Field(default_factory=utc_now_iso, max_length=64)
