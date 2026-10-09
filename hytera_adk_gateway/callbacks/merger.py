# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/merger.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：..capture.io_utils, .models
# 主要公开符号：CallbackMergeMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import base64
import logging
import time
from typing import Any

from ..capture.io_utils import now_iso
from .models import VoiceCallbackSettings, _codec_from_payload_type

logger = logging.getLogger("hytera_adk_gateway.callbacks")


def _to_local_wall_time(value: Any) -> Any:
    """把抓包层 UTC ISO 时间戳转为本地（北京）时间，便于与日志行时间对读。

    仅用于日志展示与 timing_ms 埋点字段；payload 的 occurred_at/started_at
    等契约字段保持原始 UTC 不变，避免影响下游对账。
    解析失败时原样返回。
    """
    if not value:
        return value
    try:
        from datetime import datetime

        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        return value


class CallbackMergeMixin:
    """语音回调内部能力分组。"""

    def _push_merge_item(
        self, detail: dict[str, Any], settings: VoiceCallbackSettings
    ) -> dict[str, Any] | None:
        """写入一个音频包到合并缓存。返回需要立即发送的旧缓存 payload。"""
        audio_b64 = detail.get("audio_payload_b64")
        if not audio_b64:
            return None

        try:
            audio_bytes = base64.b64decode(audio_b64, validate=False)
        except Exception as exc:
            self._remember_error(f"音频 payload base64 解码失败: {type(exc).__name__}: {exc}")
            return None

        with self._lock:
            buffer = self._merge_buffer
            if buffer is None:
                self._merge_buffer = self._new_merge_buffer(detail, audio_bytes)
                return None

            if not self._is_same_stream(buffer, detail) or not self._is_continuous(
                buffer, detail, settings
            ):
                payload = self._build_merged_callback_payload(buffer)
                self._last_flush_reason = (
                    f"stream_changed_or_gap; packets={payload.get('packet_count')}"
                )
                self._merge_buffer = self._new_merge_buffer(detail, audio_bytes)
                return payload

            self._append_merge_buffer(buffer, detail, audio_bytes)
            if self._buffer_exceeds_limits(buffer, settings):
                payload = self._build_merged_callback_payload(buffer)
                self._last_flush_reason = f"limit_reached; packets={payload.get('packet_count')}"
                self._merge_buffer = None
                return payload

        return None

    def _pop_stale_merge_payload(
        self, settings: VoiceCallbackSettings, *, force: bool = False, reason: str = "stale"
    ) -> dict[str, Any] | None:
        """如果合并缓存已经超过静音/断流阈值，则取出并生成回调 payload。"""
        with self._lock:
            buffer = self._merge_buffer
            if buffer is None:
                return None
            last_seen_monotonic = float(buffer.get("last_seen_monotonic") or time.monotonic())
            age = time.monotonic() - last_seen_monotonic
            if not force and age < settings.merge_gap_seconds:
                return None
            payload = self._build_merged_callback_payload(buffer)
            self._merge_buffer = None
            self._last_flush_reason = (
                f"{reason}; idle_seconds={age:.3f}; packets={payload.get('packet_count')}"
            )
            return payload

    def _new_merge_buffer(self, detail: dict[str, Any], audio_bytes: bytes) -> dict[str, Any]:
        rtp = detail.get("rtp") or {}
        stream_key = self._stream_key(detail)
        captured_at = detail.get("captured_at")
        logger.info(
            "voice_segment_first_packet captured_at=%s local=%s stream=%s packet_index=%s ssrc=%s",
            captured_at,
            _to_local_wall_time(captured_at),
            stream_key,
            detail.get("index"),
            rtp.get("ssrc_hex") or rtp.get("ssrc"),
        )
        return {
            "stream_key": stream_key,
            "source_context": self._snapshot_source_context(),
            "first_detail": detail,
            "last_detail": detail,
            "started_at": captured_at,
            "ended_at": captured_at,
            "last_seen_monotonic": time.monotonic(),
            "last_sequence": rtp.get("sequence"),
            "last_rtp_timestamp": rtp.get("timestamp"),
            "last_captured_epoch": self._detail_epoch(detail),
            "audio_parts": [audio_bytes],
            "audio_bytes_total": len(audio_bytes),
            "packet_count": 1,
            "packet_indexes": [detail.get("index")],
            "rtp_sequences": [rtp.get("sequence")],
            "rtp_timestamps": [rtp.get("timestamp")],
        }

    def _snapshot_source_context(self) -> dict[str, Any]:
        """Bind business routing context when a merge buffer is created.

        source_id 优先从 ADK 事件上下文获取；若为空则回退到从 UDP/RTP
        扩展数据中实时提取的 Hytera Radio ID。
        """
        try:
            context = dict(self.get_context() or {})
        except Exception as exc:
            return {"context_error": f"{type(exc).__name__}: {exc}"}
        source_id = context.get("source_id")
        if source_id is None:
            # 回退：从 RTP 扩展数据中提取的 Hytera Radio ID
            try:
                source_id = self.capture_manager.latest_radio_id
            except Exception:
                pass
        return {
            "source_id": source_id,
            "channel_id": context.get("channel_id"),
            "session_id": context.get("session_id"),
            "target_id": context.get("target_id"),
            "group_id": context.get("group_id"),
            "trace_id": context.get("trace_id"),
            "correlation_id": context.get("correlation_id"),
            "causation_id": context.get("causation_id"),
            "command_id": context.get("command_id"),
            "operation_id": context.get("operation_id"),
            "call_id": context.get("call_id"),
        }

    def _append_merge_buffer(
        self, buffer: dict[str, Any], detail: dict[str, Any], audio_bytes: bytes
    ) -> None:
        rtp = detail.get("rtp") or {}
        buffer["last_detail"] = detail
        buffer["ended_at"] = detail.get("captured_at")
        buffer["last_seen_monotonic"] = time.monotonic()
        buffer["last_sequence"] = rtp.get("sequence")
        buffer["last_rtp_timestamp"] = rtp.get("timestamp")
        buffer["last_captured_epoch"] = self._detail_epoch(detail)
        buffer["audio_parts"].append(audio_bytes)
        buffer["audio_bytes_total"] += len(audio_bytes)
        buffer["packet_count"] += 1
        buffer["packet_indexes"].append(detail.get("index"))
        buffer["rtp_sequences"].append(rtp.get("sequence"))
        buffer["rtp_timestamps"].append(rtp.get("timestamp"))

    def _buffer_exceeds_limits(
        self, buffer: dict[str, Any], settings: VoiceCallbackSettings
    ) -> bool:
        return (
            int(buffer.get("packet_count") or 0) >= settings.merge_max_packets
            or int(buffer.get("audio_bytes_total") or 0) >= settings.merge_max_audio_bytes
        )

    def _is_same_stream(self, buffer: dict[str, Any], detail: dict[str, Any]) -> bool:
        return buffer.get("stream_key") == self._stream_key(detail)

    def _is_continuous(
        self, buffer: dict[str, Any], detail: dict[str, Any], settings: VoiceCallbackSettings
    ) -> bool:
        rtp = detail.get("rtp") or {}

        # Capture time has priority over sequence continuity.  Hytera may keep RTP
        # sequence numbers continuous across two PTT calls, so checking sequence first
        # can merge utterances that are actually many seconds apart.
        prev_epoch = buffer.get("last_captured_epoch")
        cur_epoch = self._detail_epoch(detail)
        if prev_epoch is not None and cur_epoch is not None:
            gap = float(cur_epoch) - float(prev_epoch)
            if gap < 0 or gap > settings.merge_gap_seconds:
                return False

        prev_seq = buffer.get("last_sequence")
        cur_seq = rtp.get("sequence")
        if isinstance(prev_seq, int) and isinstance(cur_seq, int):
            return ((prev_seq + 1) & 0xFFFF) == cur_seq

        # With reliable timestamps, the gap check above is sufficient.
        if prev_epoch is not None and cur_epoch is not None:
            return True

        # No reliable timestamp or sequence: keep the old same-batch fallback.
        return True

    @staticmethod
    def _stream_key(detail: dict[str, Any]) -> tuple[Any, ...]:
        rtp = detail.get("rtp") or {}
        return (
            detail.get("src_ip"),
            detail.get("src_port"),
            detail.get("dst_ip"),
            detail.get("dst_port"),
            rtp.get("ssrc"),
            rtp.get("payload_type"),
        )

    @staticmethod
    def _detail_epoch(detail: dict[str, Any]) -> float | None:
        value = detail.get("frame_time_epoch")
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        captured_at = detail.get("captured_at")
        if not captured_at:
            return None
        try:
            from datetime import datetime

            text = str(captured_at)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).timestamp()
        except Exception:
            return None

    def _build_merged_callback_payload(self, buffer: dict[str, Any]) -> dict[str, Any]:
        build_started = time.perf_counter()
        flush_epoch = time.time()
        first = buffer.get("first_detail") or {}
        last = buffer.get("last_detail") or first
        first_rtp = first.get("rtp") or {}
        last_rtp = last.get("rtp") or {}
        # 语音输入总耗时：首包→末包（说话时长）；末包→flush 为静音确认等待。
        first_epoch = self._detail_epoch(first)
        last_epoch = self._detail_epoch(last)
        speech_duration_ms = (
            round(max(0.0, float(last_epoch) - float(first_epoch)) * 1000.0, 3)
            if first_epoch is not None and last_epoch is not None
            else None
        )
        silence_confirm_ms = (
            round(max(0.0, flush_epoch - float(last_epoch)) * 1000.0, 3)
            if last_epoch is not None
            else None
        )
        audio_bytes = b"".join(buffer.get("audio_parts") or [])
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

        context = dict(buffer.get("source_context") or {})
        # 需求 §3：组消息 target_id=组ID、group_id=组ID；两者必须一致可用。
        group_id = context.get("group_id")
        target_id = context.get("target_id")
        if group_id is not None and target_id is None:
            target_id = group_id

        packet = {
            "merged": True,
            "packet_count": buffer.get("packet_count"),
            "started_at": buffer.get("started_at"),
            "ended_at": buffer.get("ended_at"),
            "first_index": first.get("index"),
            "last_index": last.get("index"),
            "packet_indexes": buffer.get("packet_indexes") or [],
            "src_ip": first.get("src_ip"),
            "src_port": first.get("src_port"),
            "dst_ip": first.get("dst_ip"),
            "dst_port": first.get("dst_port"),
            "is_rtp": True,
            "payload_type": first_rtp.get("payload_type"),
            "audio_codec": first.get("audio_codec")
            or _codec_from_payload_type(first_rtp.get("payload_type")),
            "sample_rate": 8000,
            "ssrc": first_rtp.get("ssrc"),
            "ssrc_hex": first_rtp.get("ssrc_hex"),
            "first_sequence": first_rtp.get("sequence"),
            "last_sequence": last_rtp.get("sequence"),
            "first_rtp_timestamp": first_rtp.get("timestamp"),
            "last_rtp_timestamp": last_rtp.get("timestamp"),
            "rtp_sequences": buffer.get("rtp_sequences") or [],
            "rtp_timestamps": buffer.get("rtp_timestamps") or [],
            "audio_payload_size": len(audio_bytes),
            "audio_payload_b64": audio_b64,
        }

        payload = {
            "event_type": "voice_audio_segment",
            "sent_at": now_iso(),
            "session_id": context.get("session_id") or self._guess_session_id(),
            "channel_id": context.get("channel_id"),
            "target_id": target_id,
            "group_id": group_id,
            "source_id": context.get("source_id"),
            "trace_id": context.get("trace_id"),
            "correlation_id": context.get("correlation_id"),
            "causation_id": context.get("causation_id"),
            "command_id": context.get("command_id"),
            "operation_id": context.get("operation_id"),
            "call_id": context.get("call_id"),
            "occurred_at": buffer.get("started_at") or buffer.get("ended_at"),
            "audio_payload_b64": audio_b64,
            "audio_payload_size": len(audio_bytes),
            "payload_type": first_rtp.get("payload_type"),
            "audio_codec": first.get("audio_codec")
            or _codec_from_payload_type(first_rtp.get("payload_type")),
            "sample_rate": 8000,
            "packet_count": buffer.get("packet_count"),
            "started_at": buffer.get("started_at"),
            "ended_at": buffer.get("ended_at"),
            "packet": packet,
            "timing_ms": {
                "first_packet_at": _to_local_wall_time(buffer.get("started_at")),
                "last_packet_at": _to_local_wall_time(buffer.get("ended_at")),
                # 数值 epoch（UTC ms）：供 agent 侧做「首包→编码发送完成」墙钟差计算；
                # *_at 字符串仅供日志对读。
                "first_packet_epoch_ms": (
                    round(float(first_epoch) * 1000.0, 3) if first_epoch is not None else None
                ),
                "last_packet_epoch_ms": (
                    round(float(last_epoch) * 1000.0, 3) if last_epoch is not None else None
                ),
                "speech_duration_ms": speech_duration_ms,
                "silence_confirm_ms": silence_confirm_ms,
                "flushed_at_epoch_ms": round(flush_epoch * 1000.0, 3),
                "callback_build_ms": None,
            },
        }
        payload = self._drop_none(payload)
        if isinstance(payload.get("timing_ms"), dict):
            payload["timing_ms"]["callback_build_ms"] = round(
                (time.perf_counter() - build_started) * 1000.0, 3
            )
        logger.info(
            "voice_segment_closed first_packet_at=%s last_packet_at=%s packets=%s "
            "speech_duration_ms=%s silence_confirm_ms=%s audio_bytes=%d",
            _to_local_wall_time(payload.get("started_at")),
            _to_local_wall_time(payload.get("ended_at")),
            payload.get("packet_count"),
            speech_duration_ms,
            silence_confirm_ms,
            len(audio_bytes),
        )
        return payload

    def _merge_preview_locked(self) -> dict[str, Any] | None:
        buffer = self._merge_buffer
        if buffer is None:
            return None
        first = buffer.get("first_detail") or {}
        last = buffer.get("last_detail") or first
        return {
            "packet_count": buffer.get("packet_count"),
            "audio_bytes_total": buffer.get("audio_bytes_total"),
            "started_at": buffer.get("started_at"),
            "ended_at": buffer.get("ended_at"),
            "first_index": first.get("index"),
            "last_index": last.get("index"),
            "stream_key": buffer.get("stream_key"),
        }
