# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/payload.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：..capture.io_utils, .models
# 主要公开符号：CallbackPayloadMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import logging
import time
from typing import Any

from ..capture.io_utils import now_iso
from .models import _codec_from_payload_type

logger = logging.getLogger("hytera_adk_gateway.callbacks")


class CallbackPayloadMixin:
    """语音回调内部能力分组。"""

    def _build_callback_payload(self, detail: dict[str, Any]) -> dict[str, Any]:
        build_started = time.perf_counter()
        context = {}
        try:
            context = dict(self.get_context() or {})
        except Exception as exc:
            context = {"context_error": f"{type(exc).__name__}: {exc}"}

        # source_id 优先从 ADK 上下文获取；若为空则回退到 UDP/RTP 提取的 Radio ID。
        source_id = context.get("source_id")
        if source_id is None:
            try:
                source_id = self.capture_manager.latest_radio_id
            except Exception:
                pass

        rtp = detail.get("rtp") or {}
        payload = {
            "event_type": "voice_audio_packet",
            "sent_at": now_iso(),
            "session_id": context.get("session_id") or self._guess_session_id(),
            "channel_id": context.get("channel_id"),
            "target_id": context.get("target_id"),
            "group_id": context.get("group_id"),
            "source_id": source_id,
            "trace_id": context.get("trace_id"),
            "correlation_id": context.get("correlation_id"),
            "causation_id": context.get("causation_id"),
            "command_id": context.get("command_id"),
            "operation_id": context.get("operation_id"),
            "call_id": context.get("call_id"),
            "occurred_at": detail.get("captured_at"),
            # 便于接收端直接取用：顶层同时放一份 base64；packet 内保留完整明细。
            "audio_payload_b64": detail.get("audio_payload_b64"),
            "udp_payload_b64": detail.get("udp_payload_b64"),
            "payload_type": rtp.get("payload_type"),
            "audio_codec": detail.get("audio_codec")
            or _codec_from_payload_type(rtp.get("payload_type")),
            "sample_rate": 8000,
            "packet": {
                "index": detail.get("index"),
                "captured_at": detail.get("captured_at"),
                "src_ip": detail.get("src_ip"),
                "src_port": detail.get("src_port"),
                "dst_ip": detail.get("dst_ip"),
                "dst_port": detail.get("dst_port"),
                "is_rtp": detail.get("is_rtp"),
                "rtp_error": detail.get("rtp_error"),
                "udp_payload_size": detail.get("udp_payload_size"),
                "udp_payload_b64": detail.get("udp_payload_b64"),
                "audio_payload_size": rtp.get("audio_payload_size"),
                "audio_payload_b64": detail.get("audio_payload_b64"),
                "rtp": rtp,
            },
        }
        # 过滤 None，减少接收端误判；target_id/group_id 如无则不传。
        payload.setdefault("timing_ms", {})["callback_build_ms"] = round((time.perf_counter() - build_started) * 1000.0, 3)
        return self._drop_none(payload)

    def _guess_session_id(self) -> str | None:
        session = self.capture_manager.session
        if session is not None:
            return session.session_id
        if self._jsonl_path is not None:
            try:
                return self._jsonl_path.parent.name
            except Exception:
                return None
        return None

    @staticmethod
    def _preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """记录最近一次回调的概要，不保存完整 base64，避免状态接口过大。"""
        packet = dict(payload.get("packet") or {})
        if packet.get("udp_payload_b64"):
            packet["udp_payload_b64"] = f"<base64:{len(packet['udp_payload_b64'])} chars>"
        if packet.get("audio_payload_b64"):
            packet["audio_payload_b64"] = f"<base64:{len(packet['audio_payload_b64'])} chars>"
        preview = dict(payload)
        if preview.get("udp_payload_b64"):
            preview["udp_payload_b64"] = f"<base64:{len(preview['udp_payload_b64'])} chars>"
        if preview.get("audio_payload_b64"):
            preview["audio_payload_b64"] = f"<base64:{len(preview['audio_payload_b64'])} chars>"
        preview["packet"] = packet
        return preview

    @classmethod
    def _drop_none(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: cls._drop_none(v) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [cls._drop_none(v) for v in value]
        return value
