# =============================================================================
# 【维护注释】文件：voice_agent/commands/models.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.identifiers, shared_contracts.models
# 主要公开符号：Command
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Durable command contracts for every business instruction source."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from shared_contracts.identifiers import new_command_id
from shared_contracts.models import utc_now_iso

CommandSource = Literal["voice", "http", "scheduled", "manual", "recovery"]
CommandStatus = Literal[
    "RECEIVED",
    "CLASSIFIED",
    "PLANNED",
    "RUNNING",
    "DISPATCHED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "UNKNOWN",
]

COMMAND_STATUSES = {"RECEIVED", "CLASSIFIED", "PLANNED", "RUNNING", "DISPATCHED", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"}


class Command(BaseModel):
    """Durable instruction record. Raw payloads are deliberately redacted."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    command_id: str = Field(default_factory=new_command_id, min_length=1, max_length=128)
    source_type: CommandSource
    source_ref: str | None = Field(default=None, max_length=256)
    actor_id: str | None = Field(default=None, max_length=128)
    raw_text: str | None = Field(default=None, max_length=20_000)
    payload_redacted: dict[str, Any] = Field(default_factory=dict)
    status: CommandStatus = "RECEIVED"
    trace_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    parent_command_id: str | None = Field(default=None, max_length=128)
    cancel_reason: str | None = Field(default=None, max_length=512)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
    updated_at: str = Field(default_factory=utc_now_iso, max_length=64)
    completed_at: str | None = Field(default=None, max_length=64)
