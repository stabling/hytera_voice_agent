# =============================================================================
# 【维护注释】文件：shared_contracts/models.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：.identifiers
# 主要公开符号：utc_now_iso, canonical_payload_hash, decoded_audio_size, ensure_voice_event_contract, VoiceEvent, VoiceEventAck, ProcessHealth
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Pydantic contracts for the local x86↔x64 HTTP boundary."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .identifiers import new_event_id, new_trace_id


def utc_now_iso() -> str:
    """Return a stable UTC timestamp suitable for persisted callback contracts."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Hash callback content without mutable delivery metadata.

    The hash is safe to log and compare. It deliberately excludes delivery timestamps,
    identifiers and retry metadata so a retried event keeps the same content hash.
    """

    excluded = {
        "event_id",
        "correlation_id",
        "trace_id",
        "causation_id",
        "command_id",
        "operation_id",
        "call_id",
        "accepted_event_id",
        "request_id",
        "created_at",
        "occurred_at",
        "sent_at",
        "payload_hash",
        "payload_bytes",
    }
    content = {key: value for key, value in payload.items() if key not in excluded}
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decoded_audio_size(payload: dict[str, Any]) -> int:
    """Return decoded audio size without raising on legacy malformed Base64."""

    value = payload.get("audio_payload_b64") or payload.get("audio_b64")
    if not value:
        return 0
    try:
        return len(base64.b64decode(str(value), validate=False))
    except Exception:
        return 0


def ensure_voice_event_contract(
    payload: dict[str, Any],
    *,
    generate_event_id: bool = True,
    deterministic_legacy_id: bool = False,
) -> dict[str, Any]:
    """Return a copy carrying immutable runtime.

    Gateways generate a random ``event_id`` once before Outbox persistence. The receiver
    may derive a deterministic ID for legacy clients that did not send one, allowing repeated
    legacy deliveries of the same payload to remain idempotent.
    """

    event = dict(payload)
    payload_hash = str(event.get("payload_hash") or canonical_payload_hash(event))
    event_id = str(event.get("event_id") or "").strip()
    if not event_id and generate_event_id:
        event_id = (
            "legacy-" + payload_hash[:40]
            if deterministic_legacy_id
            else new_event_id()
        )
    if event_id:
        event["event_id"] = event_id[:128]
        event.setdefault("correlation_id", event["event_id"])
        event.setdefault("trace_id", new_trace_id())
    event.setdefault("terminal_id", event.get("source_id"))
    event.setdefault("created_at", event.get("sent_at") or utc_now_iso())
    event.setdefault("occurred_at", event.get("sent_at") or event.get("created_at") or utc_now_iso())
    event["payload_hash"] = payload_hash
    event["payload_bytes"] = int(event.get("payload_bytes") or decoded_audio_size(event))
    return event


class VoiceEvent(BaseModel):
    """Audio callback sent by the 32-bit ADK gateway to the 64-bit agent."""

    model_config = ConfigDict(extra="allow")

    event_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=64)
    causation_id: str | None = Field(default=None, max_length=128)
    call_id: str | None = Field(default=None, max_length=128)
    command_id: str | None = Field(default=None, max_length=128)
    operation_id: str | None = Field(default=None, max_length=128)
    occurred_at: str | None = Field(default=None, max_length=64)
    terminal_id: int | str | None = None
    session_id: str | None = Field(default=None, max_length=256)
    created_at: str | None = Field(default=None, max_length=64)
    payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    payload_bytes: int | None = Field(default=None, ge=0)
    audio_payload_b64: str | None = None
    audio_b64: str | None = None
    source_id: int | str | None = None
    target_id: int | str | None = None
    group_id: int | str | None = None
    channel_id: int | str | None = None
    codec: str | None = None
    audio_codec: str | None = None
    payload_type: int | str | None = None
    sample_rate: int | None = Field(default=None, ge=1)


class VoiceEventAck(BaseModel):
    ok: bool
    message: str
    event_id: str | None = None
    task_id: int | str | None = None
    request_id: int | str | None = None
    queued: bool = False
    duplicate: bool = False
    dropped_reason: str | None = None
    queue_depth: int = 0
    queue_size: int = 0
    ack_ms: float = 0.0


class ProcessHealth(BaseModel):
    ok: bool
    service: str
    process_bits: int
    python_version: str
    backend: str | None = None
    peer: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)
