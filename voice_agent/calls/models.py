# =============================================================================
# 【维护注释】文件：voice_agent/calls/models.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.identifiers, shared_contracts.models
# 主要公开符号：CallSession, CallParticipant, CallTurn, CallSelector, CorrelationDecision, CallResolveResult, AtomicContentResult
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""CallStore data contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from shared_contracts.identifiers import new_call_id
from shared_contracts.models import utc_now_iso

CallDirection = Literal["inbound", "outbound", "unknown"]
CallType = Literal["private", "group", "standby", "broadcast", "unknown"]
CallStatus = Literal[
    "observed",
    "requested",
    "ringing",
    "answered",
    "active",
    "ended",
    "failed",
    "rejected",
    "timeout",
]


class CallSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    call_id: str = Field(default_factory=new_call_id, min_length=1, max_length=128)
    direction: CallDirection = "unknown"
    call_type: CallType = "unknown"
    status: CallStatus = "observed"
    observed_source_id: str | None = Field(default=None, max_length=128)
    observed_target_id: str | None = Field(default=None, max_length=128)
    resolved_source_id: str | None = Field(default=None, max_length=128)
    resolved_target_id: str | None = Field(default=None, max_length=128)
    group_id: str | None = Field(default=None, max_length=128)
    initial_channel_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    requested_at: str | None = Field(default=None, max_length=64)
    ringing_at: str | None = Field(default=None, max_length=64)
    answered_at: str | None = Field(default=None, max_length=64)
    first_audio_at: str | None = Field(default=None, max_length=64)
    last_audio_at: str | None = Field(default=None, max_length=64)
    ended_at: str | None = Field(default=None, max_length=64)
    duration_ms: int | None = Field(default=None, ge=0)
    end_reason: str | None = Field(default=None, max_length=256)
    correlation_method: str | None = Field(default=None, max_length=128)
    correlation_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    provenance: Literal["observed", "derived", "imported"] = "observed"
    revision: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
    updated_at: str = Field(default_factory=utc_now_iso, max_length=64)


class CallParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    participant_id: str = Field(min_length=1, max_length=160)
    call_id: str = Field(min_length=1, max_length=128)
    observed_id: str | None = Field(default=None, max_length=128)
    resolved_id: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)
    role: Literal["caller", "callee", "group", "speaker", "unknown"] = "unknown"
    participant_type: Literal["radio", "group", "unknown"] = "unknown"
    resolution_method: str = Field(default="unresolved", max_length=128)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_event_id: str | None = Field(default=None, max_length=128)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)


class CallTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    turn_id: str = Field(min_length=1, max_length=160)
    call_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    speaker_radio_id: str | None = Field(default=None, max_length=128)
    started_at: str | None = Field(default=None, max_length=64)
    ended_at: str | None = Field(default=None, max_length=64)
    transcript: str | None = None
    normalized_text: str | None = None
    asr_ok: bool
    asr_model: str | None = Field(default=None, max_length=256)
    asr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    audio_path: str | None = None
    audio_sha256: str | None = Field(default=None, max_length=64)
    sequence_no: int | None = Field(default=None, ge=1)
    late_event: bool = False
    message_category: str | None = Field(default=None, max_length=64)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)


class CallSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    relative_type: Literal["current_active", "latest_completed", "explicit", "next", "future", "unknown"] | None = None
    contact_id: str | None = Field(default=None, max_length=128)
    start_time: str | None = Field(default=None, max_length=64)
    end_time: str | None = Field(default=None, max_length=64)
    source_terminal_id: str | None = Field(default=None, max_length=128)
    statuses: list[CallStatus] = Field(default_factory=list, max_length=16)

    call_id: str | None = Field(default=None, max_length=128)
    source_id: str | None = Field(default=None, max_length=128)
    target_id: str | None = Field(default=None, max_length=128)
    group_id: str | None = Field(default=None, max_length=128)
    channel_id: str | None = Field(default=None, max_length=128)
    status: CallStatus | None = None
    direction: CallDirection | None = None
    started_after: str | None = Field(default=None, max_length=64)
    started_before: str | None = Field(default=None, max_length=64)
    authorized_terminal_id: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class CorrelationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str | None = None
    method: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    create_new: bool = False


class CallResolveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    ok: bool
    call_id: str | None = None
    error_code: Literal["CALL_NOT_FOUND", "TARGET_AMBIGUOUS", "TARGET_NOT_RESOLVED", "FORBIDDEN"] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    resolution_method: str | None = None


class AtomicContentResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(default=1, ge=1)
    operation_type: Literal["call.summarize", "call.translate"]
    operation_id: str = Field(min_length=1, max_length=160)
    call_id: str = Field(min_length=1, max_length=128)
    ok: bool
    content: str = ""
    source_turn_ids: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=256)
    prompt_hash: str = Field(min_length=64, max_length=64)
    generated_at: str = Field(default_factory=utc_now_iso, max_length=64)
    cached: bool = False
    error: str | None = None
