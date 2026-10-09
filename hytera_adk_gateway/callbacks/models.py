# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/models.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：shared_contracts.routes
# 主要公开符号：VoiceCallbackSettings, CallbackSendResult
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared_contracts.routes import DEFAULT_AGENT_BASE_URL, AGENT_VOICE_EVENT_PATH

DEFAULT_CALLBACK_URL = (
    os.environ.get("HYTERA_AGENT_BASE_URL", DEFAULT_AGENT_BASE_URL).rstrip("/")
    + AGENT_VOICE_EVENT_PATH
)


def _codec_from_payload_type(value: Any) -> str | None:
    try:
        payload_type = int(value)
    except (TypeError, ValueError):
        return None
    if payload_type == 0:
        return "pcmu"
    if payload_type == 8:
        return "pcma"
    return None


def _configured_or_default_callback_url(value: str | None, enabled: bool) -> str | None:
    text = str(value or "").strip()
    if text:
        return text
    env_url = os.environ.get("HYTERA_VOICE_CALLBACK_URL", "").strip()
    if env_url:
        return env_url
    if enabled:
        return DEFAULT_CALLBACK_URL
    return None


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return default if not value else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return default if not value else float(value)


@dataclass
class VoiceCallbackSettings:
    """Voice callback capture and reliable-delivery configuration."""

    enabled: bool = False
    callback_url: str | None = None
    interval_seconds: float = 2.0
    timeout_seconds: float = 5.0
    max_packets_per_tick: int = 100
    merge_continuous_audio: bool = True
    # 静音确认阈值：最后一个 RTP 包之后等待多久才认定语音段结束。
    # 默认 0.4s（原 2.0s→0.8s→0.4s）：RTP 包间隔通常 ≤ 60ms，0.4s 静音即可高置信判定
    # 说话结束；配合 lifecycle 细粒度 tick，静音确认总时长可压到 1s 内；
    # 可通过 VOICE_CALLBACK_MERGE_GAP_SECONDS 回退到旧值。
    merge_gap_seconds: float = _env_float("VOICE_CALLBACK_MERGE_GAP_SECONDS", 0.4)
    merge_max_packets: int = 500
    merge_max_audio_bytes: int = 2 * 1024 * 1024
    merge_flush_on_idle_tick: bool = True
    merge_flush_on_batch_end: bool = False
    fallback_read_audio_raw: bool = True

    # runtime. ``outbox`` is the reliable default; ``legacy`` is the
    # single-variable rollback path preserving the old synchronous deque.
    delivery_mode: str = os.environ.get("VOICE_CALLBACK_DELIVERY_MODE", "outbox")
    outbox_path: str = os.environ.get(
        "VOICE_CALLBACK_OUTBOX_PATH", "voice_capture/callback_outbox.sqlite3"
    )
    outbox_max_events: int = _env_int("VOICE_CALLBACK_OUTBOX_MAX_EVENTS", 10_000)
    outbox_max_bytes: int = _env_int(
        "VOICE_CALLBACK_OUTBOX_MAX_BYTES", 512 * 1024 * 1024
    )
    outbox_retention_seconds: float = _env_float(
        "VOICE_CALLBACK_OUTBOX_RETENTION_SECONDS", 7 * 24 * 3600.0
    )
    outbox_lease_seconds: float = _env_float("VOICE_CALLBACK_OUTBOX_LEASE_SECONDS", 30.0)
    sender_workers: int = _env_int("VOICE_CALLBACK_SENDER_WORKERS", 2)
    sender_poll_seconds: float = _env_float("VOICE_CALLBACK_SENDER_POLL_SECONDS", 0.05)
    retry_max_attempts: int = _env_int("VOICE_CALLBACK_RETRY_MAX_ATTEMPTS", 12)
    circuit_failure_threshold: int = _env_int(
        "VOICE_CALLBACK_CIRCUIT_FAILURE_THRESHOLD", 5
    )
    circuit_reset_seconds: float = _env_float(
        "VOICE_CALLBACK_CIRCUIT_RESET_SECONDS", 30.0
    )

    def normalized(self) -> "VoiceCallbackSettings":
        enabled = bool(self.enabled)
        callback_url = _configured_or_default_callback_url(self.callback_url, enabled)
        delivery_mode = str(self.delivery_mode or "outbox").strip().lower()
        if delivery_mode not in {"outbox", "legacy"}:
            raise ValueError("delivery_mode must be one of: outbox, legacy")
        return VoiceCallbackSettings(
            enabled=enabled,
            callback_url=callback_url,
            interval_seconds=max(0.2, float(self.interval_seconds or 2.0)),
            timeout_seconds=max(0.2, float(self.timeout_seconds or 5.0)),
            max_packets_per_tick=max(1, int(self.max_packets_per_tick or 100)),
            merge_continuous_audio=bool(self.merge_continuous_audio),
            merge_gap_seconds=max(0.1, float(self.merge_gap_seconds or 0.4)),
            merge_max_packets=max(1, int(self.merge_max_packets or 500)),
            merge_max_audio_bytes=max(1, int(self.merge_max_audio_bytes or (2 * 1024 * 1024))),
            merge_flush_on_idle_tick=bool(self.merge_flush_on_idle_tick),
            merge_flush_on_batch_end=bool(self.merge_flush_on_batch_end),
            fallback_read_audio_raw=bool(self.fallback_read_audio_raw),
            delivery_mode=delivery_mode,
            outbox_path=str(Path(self.outbox_path or "voice_capture/callback_outbox.sqlite3")),
            outbox_max_events=max(1, int(self.outbox_max_events)),
            outbox_max_bytes=max(1, int(self.outbox_max_bytes)),
            outbox_retention_seconds=max(60.0, float(self.outbox_retention_seconds)),
            outbox_lease_seconds=max(1.0, float(self.outbox_lease_seconds)),
            sender_workers=max(1, min(16, int(self.sender_workers))),
            sender_poll_seconds=max(0.05, float(self.sender_poll_seconds)),
            retry_max_attempts=max(1, int(self.retry_max_attempts)),
            circuit_failure_threshold=max(1, int(self.circuit_failure_threshold)),
            circuit_reset_seconds=max(0.1, float(self.circuit_reset_seconds)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "callback_url": self.callback_url,
            "interval_seconds": self.interval_seconds,
            "timeout_seconds": self.timeout_seconds,
            "max_packets_per_tick": self.max_packets_per_tick,
            "merge_continuous_audio": self.merge_continuous_audio,
            "merge_gap_seconds": self.merge_gap_seconds,
            "merge_max_packets": self.merge_max_packets,
            "merge_max_audio_bytes": self.merge_max_audio_bytes,
            "merge_flush_on_idle_tick": self.merge_flush_on_idle_tick,
            "merge_flush_on_batch_end": self.merge_flush_on_batch_end,
            "fallback_read_audio_raw": self.fallback_read_audio_raw,
            "delivery_mode": self.delivery_mode,
            "outbox_path": self.outbox_path,
            "outbox_max_events": self.outbox_max_events,
            "outbox_max_bytes": self.outbox_max_bytes,
            "outbox_retention_seconds": self.outbox_retention_seconds,
            "outbox_lease_seconds": self.outbox_lease_seconds,
            "sender_workers": self.sender_workers,
            "sender_poll_seconds": self.sender_poll_seconds,
            "retry_max_attempts": self.retry_max_attempts,
            "circuit_failure_threshold": self.circuit_failure_threshold,
            "circuit_reset_seconds": self.circuit_reset_seconds,
        }


@dataclass(frozen=True)
class CallbackSendResult:
    ok: bool
    status_code: int | None
    category: str
    message: str
    acknowledgement: dict[str, Any]
    payload_bytes: int
    total_ms: float
    connect_ms: float = 0.0
    write_ms: float = 0.0
    read_ms: float = 0.0
