# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/messaging.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..models, .shared, shared_runtime.diagnostics
# 主要公开符号：MessagingMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import logging
import time

from typing import Any

from ..models import CommandResult
from .shared import ADK_SEND_ERRORS
from shared_runtime.diagnostics import bytes_snapshot, log_trace

logger = logging.getLogger(__name__)


class MessagingMixin:
    """内部能力分组；由 ``HyteraAdkBackend`` 组合使用。"""

    def send_private_text(
        self, target_id: int, text: str, *, require_ack: bool = False, channel_id: int | None = None,
        hex_encode: bool = True,
    ) -> CommandResult:
        self._require()
        self._check_id(target_id)
        # 此处允许 bytes 作为 protobuf 控制载荷；文本长度限制由上层接口负责。
        channel = self._private_text_channel(channel_id, target_id)
        option = self._option_enum(
            "TMP_PRIVATE_NEED_ACK_REQUEST" if require_ack else "TMP_PRIVATE_NO_NEED_ACK_REQUEST",
            "TMP_PRIVATE_NO_ACK_REQUEST" if not require_ack else "TMP_PRIVATE_NEED_ACK_REQUEST",
        )
        if channel is None:
            return self._blocked_text_result(
                "private_text",
                option,
                target_id,
                "No usable ADK channel is available under the current channel.mode. "
                "Use channel.mode=auto after SYSTEM/RRS discovery, or configure [channel] "
                "default_channel_id and select default/auto_then_default deliberately.",
            )
        channel_source = self._channel_source(channel, target_id=target_id)
        # 使用配置回退通道时，先通过 ConfigurationRoute 建立 Radio ID 到通道的映射，
        # 再把明确的 ChannelID 交给 ADK；最终是否接受仍由 ADK SendCommand 返回值决定。
        if channel_source == "configured_default":
            self._seed_configured_route(target_id, channel)
        request = self._types["TextMessageRequest"]()  
        request._targetID = self._types["UInt32"](target_id)
        if hex_encode:
            # 控制型：protobuf 编码后的隐藏短信，需要转 hex 再发
            request._msg = text.hex()
            logger.debug(
                "私信正文已编码为十六进制 | target_id=%s bytes=%s",
                target_id,
                len(text.hex()),
            )
        else:
            # 通知型：直接发送原文，不转 hex
            request._msg = str(text)
            logger.debug(
                "私信正文原文发送（通知型） | target_id=%s len=%s",
                target_id,
                len(text),
            )
        log_trace(
            logger,
            "底层ADK私人短信调用前",
            adk_service="TMP",
            adk_method="SendCommand",
            adk_option=self._option_name(option),
            target_id=target_id,
            requested_channel_id=channel_id,
            channel_id=channel,
            channel_source=channel_source,
            require_ack=require_ack,
            hex_encode=hex_encode,
            original_payload=bytes_snapshot(text) if isinstance(text, (bytes, bytearray, memoryview)) else {"text": str(text)},
            adk_message=str(request._msg),
        )
        adk_started = time.perf_counter()
        request_id = int(
            self._services["TMP"].SendCommand(request, option, self._types["UInt32"](channel))
        )
        adk_send_command_ms = round((time.perf_counter() - adk_started) * 1000.0, 3)
        result = self._text_result(
            "private_text",
            option,
            target_id,
            channel,
            request_id,
            require_ack=require_ack,
            channel_source=channel_source,
            timing_ms={"adk_send_command_ms": adk_send_command_ms},
        )
        log_trace(
            logger,
            "底层ADK私人短信调用结果",
            adk_service="TMP",
            adk_method="SendCommand",
            adk_option=self._option_name(option),
            target_id=target_id,
            channel_id=channel,
            request_id=request_id,
            adk_send_command_ms=adk_send_command_ms,
            result=result.to_dict(),
        )
        return result

    def send_group_text(
        self, group_id: int, text: str, *, channel_id: int | None = None
    ) -> CommandResult:
        self._require()
        self._check_id(group_id)
        option = self._option_enum("TMP_GROUP_REQUEST")
        channel = self._concrete_channel(channel_id, allow_default=True)
        if channel is None:
            return self._blocked_text_result(
                "group_text",
                option,
                group_id,
                "No concrete ADK channel is available. Group TMP cannot use channel_id=0; "
                "wait for automatic discovery or configure/select a default ChannelID.",
            )
        channel_source = self._channel_source(channel)
        request = self._types["TextMessageRequest"]()
        request._targetID = self._types["UInt32"](group_id)
        request._msg = text
        adk_started = time.perf_counter()
        request_id = int(
            self._services["TMP"].SendCommand(request, option, self._types["UInt32"](channel))
        )
        adk_send_command_ms = round((time.perf_counter() - adk_started) * 1000.0, 3)
        return self._text_result(
            "group_text",
            option,
            group_id,
            channel,
            request_id,
            require_ack=False,
            channel_source=channel_source,
            timing_ms={"adk_send_command_ms": adk_send_command_ms},
        )

    def _blocked_text_result(
        self, operation: str, option: Any, target_id: int, note: str
    ) -> CommandResult:
        # -11 表示当前没有可用通道；这是网关侧前置条件失败，此时尚未调用 CLR SendCommand。
        return CommandResult(
            False,
            operation,
            self._option_name(option),
            target_id,
            0,
            -11,
            note,
            error_code=-11,
            error_name="COMMAND_ERROR_CHANNEL_NOT_EXISTS",
            timing_ms={"adk_send_command_ms": 0.0},
        )

    def _text_result(
        self,
        operation: str,
        option: Any,
        target_id: int,
        channel: int,
        request_id: int,
        *,
        require_ack: bool,
        channel_source: str,
        timing_ms: dict[str, Any] | None = None,
    ) -> CommandResult:
        source_note = {
            "adk_auto_route": " ADK automatic route selection is in use (channel_id=0).",
            "radio_route": " A verified Radio ID -> runtime ChannelID route was used.",
            "adk_discovered": " An ADK-discovered runtime ChannelID was used.",
            "manual_runtime": " The manually selected ADK runtime ChannelID was used.",
            "configured_default": (
                f" Configured fallback ChannelID 0x{channel:08X} was used and ConfigurationRoute "
                "was seeded for this Radio ID."
            ),
        }.get(channel_source, "")
        if request_id >= 0:
            note = (
                "Submitted to ADK; wait for TMP_PRIVATE_ANSWER."
                if require_ack
                else "Submitted to ADK without terminal ACK requirement."
            ) + source_note
            return CommandResult(
                True, operation, self._option_name(option), target_id, channel, request_id, note,
                timing_ms=dict(timing_ms or {}),
            )

        error_name = ADK_SEND_ERRORS.get(request_id, "UNKNOWN_ADK_SEND_ERROR")
        fallback_hint = (
            " The configured fallback ID is not an active ADK transport in this session; "
            "switch to automatic discovery or update [channel] defaults from a current vendor-demo channel list."
            if channel_source == "configured_default" and request_id in {-1, -5, -11, -17}
            else ""
        )
        return CommandResult(
            False,
            operation,
            self._option_name(option),
            target_id,
            channel,
            request_id,
            f"ADK rejected this request locally: {error_name}. No over-the-air send and no terminal ACK can occur."
            + source_note
            + fallback_hint,
            error_code=request_id,
            error_name=error_name,
            timing_ms=dict(timing_ms or {}),
        )
