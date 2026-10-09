# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/voice.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..models, .shared, hytera_adk_gateway.logging_config
# 主要公开符号：VoiceMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import logging
import socket
import time
from pathlib import Path
from typing import Any

from ..models import CommandResult, VoiceSession
from .shared import AdkRuntimeError
from hytera_adk_gateway.logging_config import get_logger, log_print

logger = get_logger(__name__)

# Hytera DMR RTP 扩展头常量
_HYTERA_EXT_PROFILE = 0x0015
_HYTERA_EXT_LENGTH_WORDS = 3  # 12 bytes


class VoiceMixin:
    """内部能力分组；由 ``HyteraAdkBackend`` 组合使用。"""

    def attach_audio(self, channel_id: int | None = None) -> None:
        self._require()
        channel = self._channel(channel_id, allow_default=self._fallback_for_voice())
        if self.audio is None:
            raise AdkRuntimeError("Audio pipeline is unavailable.")
        self.audio.attach(channel)

    def audio_devices(self) -> dict[str, int]:
        self._require()
        return self.audio.device_counts() if self.audio is not None else {"input": 0, "output": 0}

    def start_voice_call(
        self, target_id: int, call_type: str, *, channel_id: int | None = None
    ) -> CommandResult:
        self._require()
        self._check_id(target_id)
        channel = self._channel(channel_id, allow_default=self._fallback_for_voice())
        if call_type not in {"private", "group"}:
            raise ValueError("call_type must be private or group")
        if self.voice is not None and self.voice.state not in {"stopped", "failed"}:
            raise AdkRuntimeError("A voice operation is already active; use voice-stop first.")
        self.attach_audio(channel)
        request = self._types["CallRequest"]()
        request._targetID = self._types["UInt32"](target_id)
        request._callType = self._call_type_enum(call_type)
        option = self._option_enum("RCP_CALL_REQUEST")
        log_print(logger, f"[VOICE-CALL] >>> 发送 CallRequest: target_id={target_id}, call_type={call_type}, channel={channel}, option={option}")
        request_id = int(
            self._services["RCP"].SendCommand(request, option, self._types["UInt32"](channel))
        )
        log_print(logger, f"[VOICE-CALL] <<< CallRequest 返回 request_id={request_id} ({'成功' if request_id >= 0 else '失败'})")
        self.voice = VoiceSession(
            target_id,
            call_type,
            channel,
            request_id,
            state="waiting_call_target_ack" if request_id >= 0 else "failed",
        )
        log_print(logger, f"[VOICE-CALL] 状态 → {self.voice.state}")
        return CommandResult(
            request_id >= 0,
            f"voice_{call_type}",
            self._option_name(option),
            target_id,
            channel,
            request_id,
            "Wait for RCP_CALL_REQUEST_ANSWER; the gateway presses PTT only after success.",
        )

    def stop_voice_call(self) -> CommandResult:
        self._require()
        if self.voice is None:
            raise AdkRuntimeError("No voice call is active.")
        channel = self.voice.channel_id
        log_print(logger, f"[VOICE-STOP] >>> 发送 PTT RELEASE: channel={channel}, target_id={self.voice.target_id}")
        request_id = self._send_ptt(channel, pressed=False)
        log_print(logger, f"[VOICE-STOP] <<< PTT RELEASE 返回 request_id={request_id}")
        self.voice.release_request_id = request_id
        self.voice.state = "waiting_ptt_release_ack" if request_id >= 0 else "failed"
        log_print(logger, f"[VOICE-STOP] 状态 → {self.voice.state}")
        return CommandResult(
            request_id >= 0,
            "voice_stop",
            self._option_name(self._option_enum("RCP_BUTTON_OPERATION_REQUEST")),
            self.voice.target_id,
            channel,
            request_id,
            "Wait for RCP_BUTTON_OPERATION_ANSWER.",
        )

    # ------------------------------------------------------------------
    # RTP 发送（ADK API 优先，UDP 直发回退）
    # ------------------------------------------------------------------

    _rtp_udp_socket: socket.socket | None = None
    _rtp_udp_target: tuple[str, int] | None = None
    _rtp_udp_fallback_logged: bool = False

    def _get_rtp_udp_target(self) -> tuple[str, int]:
        """推断中继台 RTP 接收地址（IP + 端口）。"""
        if self._rtp_udp_target is not None:
            return self._rtp_udp_target
        # 优先使用配置的中继台 IP（voice_capture_src_ip）
        repeater_ip = getattr(self.cfg, "voice_capture_src_ip", None) or ""
        if not repeater_ip:
            raise AdkRuntimeError(
                "无法确定中继台 IP：请在 gateway.ini [voice_capture] src_ip 中配置"
            )
        # RTP 端口使用配置中的 rtp_slot1
        rtp_port = self.cfg.ports.get("rtp_slot1") or 30012
        self._rtp_udp_target = (repeater_ip, int(rtp_port))
        return self._rtp_udp_target

    def _get_rtp_udp_socket(self) -> socket.socket:
        """获取或创建用于 RTP 直发的 UDP socket。"""
        if self._rtp_udp_socket is None or self._rtp_udp_socket.fileno() < 0:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 绑定到本机 ADK IP 的随机端口，确保中继台能看到正确源地址
            local_ip = getattr(self.cfg, "local_ip", "") or "0.0.0.0"
            try:
                sock.bind((local_ip, 0))
            except OSError:
                sock.bind(("0.0.0.0", 0))
            self._rtp_udp_socket = sock
        return self._rtp_udp_socket

    def _send_rtp_via_udp(self, packet: bytes) -> int:
        """通过 UDP 直接将 RTP 包发送到中继台（绕过 ADK CLR）。"""
        target = self._get_rtp_udp_target()
        sock = self._get_rtp_udp_socket()
        sock.sendto(packet, target)
        return len(packet)

    def send_rtp_packet(self, packet: bytes, *, channel_id: int | None = None) -> int:
        """Send one RTP packet via SoundController.SendRTPData, fallback to UDP.

        CLR 签名：void SoundController.SendRTPData(Byte[] buffer, UInt32 channelID)
        注意：与 C++ API 不同，CLR 版本只有 2 个参数（无 size）。
        """
        self._require()
        if not packet:
            raise ValueError("RTP packet cannot be empty")
        channel = self._channel(channel_id, allow_default=self._fallback_for_voice())

        # SoundController.SendRTPData（ADK 原生 RTP 发送）
        sound = getattr(self.audio, "_sound", None) if self.audio else None
        if sound is not None:
            try:
                array = self._types["ByteArray"](packet)
                sound.SendRTPData(array, self._types["UInt32"](channel))
                if not getattr(self, "_rtp_first_packet_logged", False):
                    self._rtp_first_packet_logged = True
                    log_print(logger, f"[RTP-SEND] 首包通过 SoundController.SendRTPData 发送成功: {len(packet)} bytes, channel={channel}")
                    log_print(logger, f"[RTP-SEND] 包头(hex): {packet[:16].hex()}")
                return len(packet)
            except Exception as exc:
                log_print(logger, f"[RTP-SEND] SoundController.SendRTPData 异常: {type(exc).__name__}: {exc}")
                logger.warning("SoundController.SendRTPData 失败，回退 UDP 直发: %s", exc)
        elif not self._rtp_udp_fallback_logged:
            self._rtp_udp_fallback_logged = True
            target = self._get_rtp_udp_target()
            log_print(logger, f"[RTP-SEND] SoundController 不可用，使用 UDP 直发 → {target[0]}:{target[1]}")
            logger.info(
                "SoundController 不可用，RTP 将通过 UDP 直发到中继台 %s:%s",
                target[0], target[1],
            )

        # 回退：UDP 直发到中继台
        sent = self._send_rtp_via_udp(packet)
        if not getattr(self, "_rtp_first_udp_logged", False):
            self._rtp_first_udp_logged = True
            target = self._get_rtp_udp_target()
            log_print(logger, f"[RTP-SEND] 首包通过 UDP 直发成功: {sent} bytes → {target[0]}:{target[1]}")
            log_print(logger, f"[RTP-SEND] 包头(hex): {packet[:16].hex()}")
        return sent

    def voice_capture_dirs(self) -> dict[str, str]:
        """Return gateway-owned directories used by the external TShark voice layer."""
        return {
            "capture_root": str(self.voice_capture_root),
            "send_root": str(self.voice_send_root),
        }

    def prepare_wav_voice_payload(
        self,
        wav_bytes: bytes,
        *,
        filename: str = "voice.wav",
        target_id: int | None = None,
        call_type: str = "private",
        frame_ms: int = 60,
        payload_type: int = 0,
        ssrc: int = 0x12345678,
        sequence_start: int = 1,
        timestamp_start: int = 0,
    ) -> dict[str, Any]:
        """Reserved WAV preparation API; implementation intentionally blank."""

        del (
            wav_bytes,
            filename,
            target_id,
            call_type,
            frame_ms,
            payload_type,
            ssrc,
            sequence_start,
            timestamp_start,
        )
        raise NotImplementedError(
            "send-wav payload preparation is reserved for future development"
        )

    def start_ptt_direct(
        self, target_id: int, *, channel_id: int | None = None
    ) -> CommandResult:
        """直接按 PTT 获取发射权限，无需呼叫请求/无需目标 ACK。

        流程：PTT PRESS → RCP_BUTTON_OPERATION_ANSWER → transmitting → 发 RTP → PTT RELEASE。
        """
        self._require()
        self._check_id(target_id)
        channel = self._channel(channel_id, allow_default=self._fallback_for_voice())
        if self.voice is not None and self.voice.state not in {"stopped", "failed"}:
            raise AdkRuntimeError("A voice operation is already active; use voice-stop first.")
        self.attach_audio(channel)
        log_print(logger, f"[PTT-DIRECT] >>> 直接 PTT 呼叫: target_id={target_id}, channel={channel}")
        request_id = self._send_ptt(channel, pressed=True)
        self.voice = VoiceSession(
            target_id,
            "private",
            channel,
            request_id,
            state="waiting_ptt_press_ack" if request_id >= 0 else "failed",
        )
        self.voice.ptt_request_id = request_id
        log_print(logger, f"[PTT-DIRECT] 状态 → {self.voice.state}, ptt_request_id={request_id}")
        return CommandResult(
            request_id >= 0,
            "voice_ptt_direct",
            self._option_name(self._option_enum("RCP_BUTTON_OPERATION_REQUEST")),
            target_id,
            channel,
            request_id,
            "PTT pressed directly; wait for BUTTON_OPERATION_ANSWER then transmit RTP.",
        )

    def wait_voice_ready(self, timeout: float = 10.0, *, force_ptt: bool = True) -> bool:
        """等待语音呼叫建立完成（state 变为 transmitting）。

        完整流程：CallRequest → CallReply → PressPTT → OperationReply → transmitting。
        如果 CallReply 超时且 force_ptt=True，则直接按 PTT 强制获取发射权限。
        """
        deadline = time.monotonic() + timeout
        forced = False
        last_state = None
        poll_count = 0
        log_print(logger, f"[VOICE-WAIT] 开始等待呼叫建立, timeout={timeout}s, force_ptt={force_ptt}, 当前 state={self.voice.state if self.voice else None}")
        while time.monotonic() < deadline:
            if self.voice is None:
                log_print(logger, "[VOICE-WAIT] voice 会话已丢失，返回 False")
                return False
            # 打印状态变化
            if self.voice.state != last_state:
                log_print(logger, f"[VOICE-WAIT] 状态变化: {last_state} → {self.voice.state} (poll #{poll_count})")
                last_state = self.voice.state
            if self.voice.state == "transmitting":
                log_print(logger, f"[VOICE-WAIT] ✓ 呼叫建立成功 (transmitting), 耗时 poll #{poll_count}")
                return True
            if self.voice.state in {"failed", "stopped"}:
                log_print(logger, f"[VOICE-WAIT] ✗ 呼叫失败/停止: state={self.voice.state}")
                return False
            # 超过一半等待时间仍未收到 CallReply → 强制 PTT
            if (
                force_ptt
                and not forced
                and self.voice.state == "waiting_call_target_ack"
                and time.monotonic() > deadline - timeout / 2
            ):
                forced = True
                log_print(logger, f"[VOICE-WAIT] CallReply 超时({timeout/2:.1f}s)，强制按 PTT (target={self.voice.target_id})")
                logger.info(
                    "CallReply 未到达，直接按 PTT 强制发射 (target=%s)",
                    self.voice.target_id,
                )
                request_id = self._send_ptt(self.voice.channel_id, pressed=True)
                log_print(logger, f"[VOICE-WAIT] 强制 PTT 返回 request_id={request_id}")
                self.voice.ptt_request_id = request_id
                self.voice.state = "waiting_ptt_press_ack" if request_id >= 0 else "failed"
            poll_count += 1
            time.sleep(0.1)
        log_print(logger, f"[VOICE-WAIT] ✗ 等待超时({timeout}s), 最终 state={self.voice.state if self.voice else None}, polls={poll_count}")
        return False

    def _send_ptt(self, channel_id: int, *, pressed: bool) -> int:
        action = "PRESS" if pressed else "RELEASE"
        log_print(logger, f"[PTT] >>> 发送 PTT {action}: channel={channel_id}")
        request = self._types["ButtonKeyboardOperationRequest"]()
        request._button = self._enum_member(self._types["Buttons"], "PTT", "AUDIO_PTT", "LOCAL_PTT")
        request._operation = self._enum_member(
            self._types["ButtonOperation"], "PRESS" if pressed else "RELEASE"
        )
        request_id = int(
            self._services["RCP"].SendCommand(
                request,
                self._option_enum("RCP_BUTTON_OPERATION_REQUEST"),
                self._types["UInt32"](channel_id),
            )
        )
        log_print(logger, f"[PTT] <<< PTT {action} 返回 request_id={request_id} ({'成功' if request_id >= 0 else '失败'})")
        return request_id
