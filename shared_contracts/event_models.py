# =============================================================================
# 【维护注释】文件：shared_contracts/event_models.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：.event_types, .identifiers, .models
# 主要公开符号：DomainEvent, EventEnvelope, EventQuery, upcast_event_payload
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Versioned domain-event contracts shared across local processes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .event_types import CURRENT_SCHEMA_VERSION, schema_version_for, validate_event_type
from .identifiers import new_event_id, new_trace_id
from .models import VoiceEvent, ensure_voice_event_contract, utc_now_iso


class DomainEvent(BaseModel):
    """Append-only business fact envelope.

    The field names follow the project's runtime
    CloudEvents-compatible separation between event metadata and ``data``.
    """

    model_config = ConfigDict(extra="allow")

    event_id: str = Field(default_factory=new_event_id, max_length=128)
    event_type: str = Field(min_length=1, max_length=256)
    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION, ge=1)
    occurred_at: str = Field(default_factory=utc_now_iso, max_length=64)
    recorded_at: str = Field(default_factory=utc_now_iso, max_length=64)
    source_service: str = Field(min_length=1, max_length=128)
    subject: str | None = Field(default=None, max_length=256)
    trace_id: str = Field(default_factory=new_trace_id, min_length=32, max_length=32)
    correlation_id: str | None = Field(default=None, max_length=128)
    causation_id: str | None = Field(default=None, max_length=128)
    command_id: str | None = Field(default=None, max_length=128)
    operation_id: str | None = Field(default=None, max_length=128)
    call_id: str | None = Field(default=None, max_length=128)
    actor_id: str | None = Field(default=None, max_length=128)
    data: dict[str, Any] = Field(default_factory=dict)
    provenance: Literal["observed", "derived", "imported"] = "observed"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def model_post_init(self, __context: Any) -> None:
        self.event_type = validate_event_type(self.event_type)

    @classmethod
    def from_voice_event(
        cls,
        voice_event: VoiceEvent | dict[str, Any],
        *,
        event_type: str,
        source_service: str,
        event_id: str | None = None,
        causation_id: str | None = None,
        data: dict[str, Any] | None = None,
        provenance: Literal["observed", "derived", "imported"] = "observed",
    ) -> "DomainEvent":
        raw = voice_event.model_dump(exclude_none=True) if isinstance(voice_event, VoiceEvent) else dict(voice_event)
        contract = ensure_voice_event_contract(raw, generate_event_id=True)
        safe_data = data if data is not None else {
            key: value
            for key, value in contract.items()
            if key
            not in {
                "audio_payload_b64",
                "audio_b64",
                "udp_payload_b64",
                "packet",
            }
        }
        return cls(
            event_id=event_id or new_event_id(),
            event_type=event_type,
            schema_version=schema_version_for(event_type),
            occurred_at=str(contract.get("occurred_at") or contract.get("created_at") or utc_now_iso()),
            source_service=source_service,
            subject=str(contract.get("session_id") or contract.get("terminal_id") or "") or None,
            trace_id=str(contract.get("trace_id") or new_trace_id()),
            correlation_id=contract.get("correlation_id"),
            causation_id=causation_id or contract.get("causation_id"),
            command_id=contract.get("command_id"),
            operation_id=contract.get("operation_id"),
            call_id=contract.get("call_id"),
            actor_id=str(contract.get("source_id") or contract.get("terminal_id") or "") or None,
            data=safe_data,
            provenance=provenance,
        )

    def to_voice_event(self, base: dict[str, Any] | None = None) -> VoiceEvent:
        payload = dict(base or {})
        payload.update(
            {
                "trace_id": self.trace_id,
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
                "command_id": self.command_id,
                "operation_id": self.operation_id,
                "call_id": self.call_id,
                "occurred_at": self.occurred_at,
            }
        )
        payload.update(self.data)
        return VoiceEvent.model_validate(payload)


class EventEnvelope(DomainEvent):
    """Compatibility name used by callers that prefer envelope terminology."""


class EventQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=64)
    correlation_id: str | None = Field(default=None, max_length=128)
    call_id: str | None = Field(default=None, max_length=128)
    event_type: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


def upcast_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a persisted event to the current in-memory shape."""

    value = dict(payload)
    raw_version = value.get("schema_version", 1)
    version = int(1 if raw_version is None else raw_version)
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"event schema_version {version} is newer than supported {CURRENT_SCHEMA_VERSION}"
        )
    if version < 1:
        value["schema_version"] = 1
    value.setdefault("recorded_at", value.get("occurred_at") or utc_now_iso())
    value.setdefault("provenance", "observed")
    value.setdefault("confidence", 1.0)
    value.setdefault("data", {})
    return value
