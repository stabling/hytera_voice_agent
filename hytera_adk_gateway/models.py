# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/models.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：AdkEvent, CommandResult, VoiceSession
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class AdkEvent:
    service: str
    kind: str
    option: str
    channel_id: int | None = None
    request_id: int | None = None
    source_id: int | None = None
    target_id: int | None = None
    text: str | None = None
    result_code: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "service": self.service,
            "kind": self.kind,
            "option": self.option,
            "channel_id": self.channel_id,
            "request_id": self.request_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "text": self.text,
            "result_code": self.result_code,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    operation: str
    option: str
    target_id: int | None
    channel_id: int
    request_id: int
    note: str = ""
    error_code: int | None = None
    error_name: str | None = None
    timing_ms: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "operation": self.operation,
            "option": self.option,
            "target_id": self.target_id,
            "channel_id": self.channel_id,
            "request_id": self.request_id,
            "note": self.note,
            "error_code": self.error_code,
            "error_name": self.error_name,
            "timing_ms": dict(self.timing_ms),
        }


@dataclass
class VoiceSession:
    target_id: int
    call_type: str
    channel_id: int
    call_request_id: int
    ptt_request_id: int | None = None
    release_request_id: int | None = None
    state: str = "waiting_call_target_ack"
