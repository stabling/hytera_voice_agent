# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/services/voice_delivery.py
# 所属层次：x86 网关服务层
# 主要职责：承接 HTTP 路由并协调 ADK backend，保持协议转换、设备调用与 API 层解耦。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：VoiceDeliveryService
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""WAV delivery extension point.

The public method signature is intentionally retained while implementation is
left blank for the next development phase.
"""

from __future__ import annotations

from typing import Any, Callable


class VoiceDeliveryService:
    """Reserved service boundary for future ``send-wav`` development."""

    def __init__(self, get_backend: Callable[[], Any]) -> None:
        self._get_backend = get_backend

    @staticmethod
    def validate_parameters(
        *, target_id: int, frame_ms: int, packet_interval_ms: int, max_packets: int
    ) -> None:
        if not 1 <= target_id <= 16_777_215:
            raise ValueError("target_id 必须在 1..16777215 范围内")
        if not 20 <= frame_ms <= 1000:
            raise ValueError("frame_ms 必须在 20..1000 范围内")
        if not 0 <= packet_interval_ms <= 5000:
            raise ValueError("packet_interval_ms 必须在 0..5000 范围内")
        if max_packets < 0:
            raise ValueError("max_packets 必须 >= 0")

    def send_private_wav(
        self,
        wav_bytes: bytes,
        *,
        filename: str,
        target_id: int,
        channel_id: int | None,
        frame_ms: int,
        payload_type: int,
        ssrc: int,
        transmit: bool,
        start_call: bool,
        stop_call: bool,
        packet_interval_ms: int,
        max_packets: int,
    ) -> dict[str, Any]:
        """Reserved ``send-wav`` service API; implementation intentionally blank."""

        del (
            wav_bytes,
            filename,
            target_id,
            channel_id,
            frame_ms,
            payload_type,
            ssrc,
            transmit,
            start_call,
            stop_call,
            packet_interval_ms,
            max_packets,
        )
        raise NotImplementedError("send-wav service is reserved for future development")
