# =============================================================================
# 【维护注释】文件：shared_contracts/event_types.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：unknown_event_policy, validate_event_type, schema_version_for
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Central event type and schema-version registry."""

from __future__ import annotations

import os
from typing import Final

VOICE_RECEIVED: Final = "voice.received"
VOICE_CALLBACK_ACCEPTED: Final = "voice.callback.accepted"
VOICE_CALLBACK_DUPLICATE: Final = "voice.callback.duplicate"
VOICE_CALLBACK_REJECTED: Final = "voice.callback.rejected"
ASR_COMPLETED: Final = "asr.completed"
ASR_FAILED: Final = "asr.failed"
TOOL_REQUESTED: Final = "tool.requested"
TOOL_COMPLETED: Final = "tool.completed"
TOOL_FAILED: Final = "tool.failed"
OPERATION_REQUESTED: Final = "operation.requested"
OPERATION_COMPLETED: Final = "operation.completed"
OPERATION_FAILED: Final = "operation.failed"
CALL_REQUESTED: Final = "call.requested"
CALL_STARTED: Final = "call.started"
CALL_RINGING: Final = "call.ringing"
CALL_ANSWERED: Final = "call.answered"
CALL_AUDIO_STARTED: Final = "call.audio_started"
CALL_AUDIO_ACTIVITY: Final = "call.audio_activity"
CALL_ENDED: Final = "call.ended"
CALL_FAILED: Final = "call.failed"
CALL_REJECTED: Final = "call.rejected"
CALL_TIMEOUT: Final = "call.timeout"
CALL_ADK_OBSERVED: Final = "call.adk_observed"
DEVICE_ACTION_REQUESTED: Final = "device.action.requested"
DEVICE_ACTION_COMPLETED: Final = "device.action.completed"
DEVICE_ACTION_FAILED: Final = "device.action.failed"

CURRENT_SCHEMA_VERSION: Final = 1

EVENT_TYPE_SCHEMA_VERSIONS: Final[dict[str, int]] = {
    VOICE_RECEIVED: 1,
    VOICE_CALLBACK_ACCEPTED: 1,
    VOICE_CALLBACK_DUPLICATE: 1,
    VOICE_CALLBACK_REJECTED: 1,
    ASR_COMPLETED: 1,
    ASR_FAILED: 1,
    TOOL_REQUESTED: 1,
    TOOL_COMPLETED: 1,
    TOOL_FAILED: 1,
    OPERATION_REQUESTED: 1,
    OPERATION_COMPLETED: 1,
    OPERATION_FAILED: 1,
    CALL_REQUESTED: 1,
    CALL_STARTED: 1,
    CALL_RINGING: 1,
    CALL_ANSWERED: 1,
    CALL_AUDIO_STARTED: 1,
    CALL_AUDIO_ACTIVITY: 1,
    CALL_ENDED: 1,
    CALL_FAILED: 1,
    CALL_REJECTED: 1,
    CALL_TIMEOUT: 1,
    CALL_ADK_OBSERVED: 1,
    DEVICE_ACTION_REQUESTED: 1,
    DEVICE_ACTION_COMPLETED: 1,
    DEVICE_ACTION_FAILED: 1,
}


def unknown_event_policy() -> str:
    value = os.environ.get("EVENT_TYPE_UNKNOWN_POLICY", "allow").strip().lower()
    return value if value in {"allow", "reject"} else "allow"


def validate_event_type(event_type: str, *, policy: str | None = None) -> str:
    name = str(event_type or "").strip()
    if not name:
        raise ValueError("event_type is required")
    selected = (policy or unknown_event_policy()).strip().lower()
    if name not in EVENT_TYPE_SCHEMA_VERSIONS and selected == "reject":
        raise ValueError(f"unknown event_type: {name}")
    return name


def schema_version_for(event_type: str) -> int:
    return EVENT_TYPE_SCHEMA_VERSIONS.get(str(event_type), CURRENT_SCHEMA_VERSION)
