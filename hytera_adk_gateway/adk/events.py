# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/events.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..models, .shared, hytera_adk_gateway.logging_config
# 主要公开符号：EventMixin, adk_event_to_domain_event
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Any

from ..models import AdkEvent
from .shared import ADK_SEND_ERRORS, AdkRuntimeError
from hytera_adk_gateway.logging_config import get_logger, log_print

logger = get_logger(__name__)


class EventMixin:
    """内部能力分组；由 ``HyteraAdkBackend`` 组合使用。"""

    def _on_system(self, clr_event: Any) -> None:
        event = self._decode("SYSTEM", clr_event)
        if event.option == "SYSTEM_CHANNEL_CHANGED_REPORT":
            payload = getattr(clr_event, "_eventData", None)
            channel_id = self._asint(self._first_attr(payload, "_channelID", "_channelId"))
            deleting = bool(self._first_attr(payload, "_isDelete", "_delete"))
            if channel_id is not None:
                if deleting:
                    self.channels.pop(channel_id, None)
                    for radio_id, route in list(self.radio_routes.items()):
                        if route == channel_id:
                            self.radio_routes.pop(radio_id, None)
                    self._emit(
                        "SYSTEM",
                        "channel_offline",
                        event.option,
                        channel_id=channel_id,
                        metadata=event.metadata,
                    )
                else:
                    slot = self._asint(
                        self._first_attr(payload, "_soltID", "_slotID", "_slot", "_Slot")
                    )
                    self.channels[channel_id] = {
                        "slot": slot,
                        "ip": self._asint(self._first_attr(payload, "_channelIP", "_channelIp")),
                        "device_id": self._asint(
                            self._first_attr(payload, "_deviceID", "_deviceId")
                        ),
                        "discovered_by": "SYSTEM_CHANNEL_CHANGED_REPORT",
                        "provisional": False,
                    }
                    self._auto_select_channel(channel_id, slot=slot, source="adk_auto")
                    self._emit(
                        "SYSTEM",
                        "channel_online",
                        event.option,
                        channel_id=channel_id,
                        metadata=self.channels[channel_id],
                    )
                    if self.cfg.auto_attach_audio:
                        try:
                            if self.audio is not None:
                                self.audio.attach(channel_id)
                        except Exception as exc:
                            self._emit(
                                "AUDIO",
                                "audio_attach_failed",
                                "PLAY_CHANNEL_SOUND",
                                channel_id=channel_id,
                                metadata={"error": str(exc)},
                            )
            return

        if event.option == "SYSTEM_ROUTE_CHANGED_REPORT":
            payload = getattr(clr_event, "_eventData", None)
            radio_id = self._asint(self._first_attr(payload, "_radioID", "_radioId"))
            channel_id = self._asint(self._first_attr(payload, "_channelID", "_channelId"))
            old_channel_id = self._asint(
                self._first_attr(payload, "_oldChannelID", "_oldChannelId")
            )
            if radio_id is not None and channel_id is not None:
                self._remember_observed_channel(
                    event, channel_id, discovered_by="SYSTEM_ROUTE_CHANGED_REPORT"
                )
                self.radio_routes[radio_id] = channel_id
                self._emit(
                    "SYSTEM",
                    "route_changed",
                    event.option,
                    channel_id=channel_id,
                    source_id=radio_id,
                    metadata={"old_channel_id": old_channel_id},
                )
                return
        self._record_event(event)

    def _on_tmp(self, clr_event: Any) -> None:
        event = self._decode("TMP", clr_event)
        self._remember_observed_channel(event)
        self._record_event(event)

    def _on_rrs(self, clr_event: Any) -> None:
        event = self._decode("RRS", clr_event)
        self._remember_observed_channel(event)
        self._record_event(event)

        # 详细日志：调试 RRS 注册和应答逻辑
        radio_id = event.source_id or event.target_id
        channel_id = event.channel_id
        self._emit(
            "RRS",
            "debug_rrs_event",
            "RRS_EVENT_RECEIVED",
            source_id=radio_id,
            channel_id=channel_id,
            metadata={
                "option": event.option,
                "option_raw": getattr(clr_event, "_optioncode", None),
                "source_id_raw": event.source_id,
                "target_id_raw": event.target_id,
                "channel_id_raw": event.channel_id,
                "rrs_auto_ack": self.cfg.rrs_auto_ack,
            },
        )

        if event.option != "RRS_REGIST":
            self._emit(
                "RRS",
                "debug_rrs_skip",
                "RRS_SKIP",
                metadata={"reason": f"option is {event.option}, not RRS_REGIST"},
            )
            return

        # The supplied V4 C# document states that incoming ServiceEvent carries
        # _srcID/_destID. Some CLR revisions expose those directly, others only
        # expose a subset. When the IDs are available, seed the ADK route table
        # immediately instead of waiting solely for SYSTEM_ROUTE_CHANGED_REPORT.
        if radio_id is None or channel_id is None or channel_id <= 0:
            self._emit(
                "RRS",
                "registration_ack_skipped",
                event.option,
                channel_id=channel_id,
                source_id=radio_id,
                target_id=event.target_id,
                metadata={
                    "reason": f"radio_id={radio_id}, channel_id={channel_id}, need both > 0",
                    "hint": "Check CLR event field names for _srcID/_channelID",
                },
            )
            return

        if not self.cfg.rrs_auto_ack:
            self._emit(
                "RRS",
                "registration_ack_disabled",
                event.option,
                metadata={"reason": "rrs_auto_ack=false"},
            )
            return

        # Seed route table from RRS registration
        if radio_id is not None and channel_id is not None and channel_id > 0:
            try:
                self._adk.ConfigurationRoute(
                    self._types["UInt32"](radio_id), self._types["UInt32"](channel_id)
                )
                self.radio_routes[radio_id] = channel_id
                self._emit(
                    "SYSTEM",
                    "route_seeded_from_rrs",
                    "CONFIGURATION_ROUTE",
                    channel_id=channel_id,
                    source_id=radio_id,
                    metadata={"reason": "RRS_REGIST"},
                )
            except Exception as exc:
                self._emit(
                    "SYSTEM",
                    "route_seed_failed",
                    "CONFIGURATION_ROUTE",
                    channel_id=channel_id,
                    source_id=radio_id,
                    metadata={"error": str(exc)},
                )
        else:
            self._emit(
                "RRS",
                "registration_route_unavailable",
                event.option,
                channel_id=channel_id,
                source_id=radio_id,
                target_id=event.target_id,
                metadata={
                    "reason": "The CLR event did not expose both radio ID and channel ID; no route was invented.",
                    "hint": "Run interactive with --debug-fields and keep the event JSON.",
                },
            )

        try:
            answer = self._types["OnlineAnswerRequest"]()
            answer._targetID = self._types["UInt32"](radio_id)
            answer._result = self._enum_member(self._types["RadioOnlineResult"], "SUCCESS")
            answer._validTime = self._types["UInt32"](self.cfg.rrs_valid_time)
            request_id = int(
                self._services["RRS"].SendCommand(
                    answer, self._option_enum("RRS_REGIST_ACK"), self._types["UInt32"](channel_id)
                )
            )
            self._emit(
                "RRS",
                "registration_ack_submitted" if request_id >= 0 else "registration_ack_failed",
                "RRS_REGIST_ACK",
                channel_id=channel_id,
                request_id=request_id,
                target_id=radio_id,
                result_code=0 if request_id >= 0 else request_id,
                metadata={
                    "valid_time": self.cfg.rrs_valid_time,
                    "error_name": None
                    if request_id >= 0
                    else ADK_SEND_ERRORS.get(request_id, "UNKNOWN_ADK_SEND_ERROR"),
                },
            )
        except Exception as exc:
            self._emit(
                "RRS",
                "registration_ack_failed",
                "RRS_REGIST_ACK",
                channel_id=channel_id,
                target_id=radio_id,
                metadata={"error": str(exc)},
            )

    # 呼叫建立相关 opcode（用于日志和状态判断）
    _VOICE_CALL_OPTIONS = {
        "RCP_CALL_REQUEST",                       # 0x0841 呼叫请求
        "RCP_CALL_REQUEST_ANSWER",                 # 0x8841 呼叫应答
        "RCP_BUTTON_OPERATION_REQUEST",            # PTT 请求
        "RCP_BUTTON_OPERATION_ANSWER",             # PTT 应答
        "RCP_ANALOG_BROADCASTSTATUS_CONFIGURE",    # 0x10C9 广播状态配置请求
        "RCP_ANALOG_BROADCASTSTATUS_CONFIGURE_REPLY",  # 0x80C9 广播状态配置应答
        "RCP_ANALOG_BROADCASTVOICESTATUS",         # 0xB843 广播发送状态通知
        "RCP_BROADCAST_STATUS_REPORT",             # 广播状态报告
        "RCP_XPTBroadcastStatusConfigureRequest",  # XPT 广播配置请求
        "RCP_XPTBroadcastStatusConfigureACK",      # XPT 广播配置应答
        "RCP_XPTFreeRepeaterBroadcastReport",      # XPT 空闲中继台广播报告
    }

    def _on_rcp(self, clr_event: Any) -> None:
        event = self._decode("RCP", clr_event)
        self._remember_observed_channel(event)
        self._record_event(event)

        # 记录所有呼叫/广播相关 RCP 事件的 opcode，便于确认呼叫连接流程
        if event.option in self._VOICE_CALL_OPTIONS:
            payload = getattr(clr_event, "_eventData", None)
            process = self._safe_repr(getattr(payload, "_process", None), limit=50)
            source = self._safe_repr(getattr(payload, "_source", None), limit=50)
            # 详细打印原始 CLR 事件的所有可用字段
            raw_fields = {}
            for attr in dir(clr_event):
                if attr.startswith("_") and not attr.startswith("__"):
                    try:
                        val = getattr(clr_event, attr, None)
                        if val is not None and not callable(val):
                            raw_fields[attr] = repr(val)[:80]
                    except Exception:
                        pass
            payload_fields = {}
            if payload is not None:
                for attr in dir(payload):
                    if attr.startswith("_") and not attr.startswith("__"):
                        try:
                            val = getattr(payload, attr, None)
                            if val is not None and not callable(val):
                                payload_fields[attr] = repr(val)[:80]
                        except Exception:
                            pass
            log_print(logger, f"\n[RCP-EVENT] ====== 收到呼叫相关事件 ======")
            log_print(logger, f"[RCP-EVENT] option={event.option}")
            log_print(logger, f"[RCP-EVENT] request_id={event.request_id}, result_code={event.result_code}")
            log_print(logger, f"[RCP-EVENT] source_id={event.source_id}, target_id={event.target_id}, channel_id={event.channel_id}")
            log_print(logger, f"[RCP-EVENT] process={process}, source={source}")
            log_print(logger, f"[RCP-EVENT] clr_event 字段: {raw_fields}")
            log_print(logger, f"[RCP-EVENT] eventData 字段: {payload_fields}")
            log_print(logger, f"[RCP-EVENT] 当前 voice.state={self.voice.state if self.voice else None}")
            log_print(logger, f"[RCP-EVENT] voice.call_request_id={self.voice.call_request_id if self.voice else None}, voice.ptt_request_id={self.voice.ptt_request_id if self.voice else None}")
            log_print(logger, f"[RCP-EVENT] ====================================\n")
            logger.info(
                "RCP 呼叫事件: option=%s request_id=%s result=%s process=%s source=%s",
                event.option, event.request_id, event.result_code, process, source,
            )

        if self.voice is None:
            return

        # --- 0x8841 CallReply：呼叫应答 ---
        if (
            event.option == "RCP_CALL_REQUEST_ANSWER"
            and event.request_id == self.voice.call_request_id
        ):
            log_print(logger, f"[RCP-CALL] 收到 CallReply: result_code={event.result_code} ({'ACK成功' if event.result_code == 0 else '拒绝'})")
            if event.result_code == 0:
                log_print(logger, f"[RCP-CALL] CallReply ACK 成功，自动发起 PTT PRESS...")
                request_id = self._send_ptt(self.voice.channel_id, pressed=True)
                self.voice.ptt_request_id = request_id
                self.voice.state = "waiting_ptt_press_ack" if request_id >= 0 else "failed"
                log_print(logger, f"[RCP-CALL] PTT PRESS 已发送, request_id={request_id}, 状态 → {self.voice.state}")
                self._emit(
                    "VOICE",
                    "ptt_press_submitted",
                    "RCP_BUTTON_OPERATION_REQUEST",
                    channel_id=self.voice.channel_id,
                    request_id=request_id,
                    target_id=self.voice.target_id,
                )
            else:
                self.voice.state = "failed"
                log_print(logger, f"[RCP-CALL] ✗ CallReply 拒绝: result_code={event.result_code}, 状态 → failed")
                self._emit(
                    "VOICE",
                    "call_target_rejected",
                    event.option,
                    channel_id=self.voice.channel_id,
                    request_id=event.request_id,
                    target_id=self.voice.target_id,
                    result_code=event.result_code,
                )

        # --- PTT OperationReply：按钮操作应答 ---
        elif event.option == "RCP_BUTTON_OPERATION_ANSWER":
            log_print(logger, f"[RCP-PTT-ACK] 收到 PTT 应答: request_id={event.request_id}, result_code={event.result_code}, "
                  f"ptt_request_id={self.voice.ptt_request_id}, release_request_id={self.voice.release_request_id}")
            if event.request_id == self.voice.ptt_request_id:
                success = event.result_code == 0
                self.voice.state = "transmitting" if success else "failed"
                log_print(logger, f"[RCP-PTT-ACK] PTT PRESS 确认: {'✓ 成功 → transmitting' if success else '✗ 失败 → failed'} (result_code={event.result_code})")
                self._emit(
                    "VOICE",
                    "ptt_held" if success else "ptt_press_failed",
                    event.option,
                    channel_id=self.voice.channel_id,
                    request_id=event.request_id,
                    target_id=self.voice.target_id,
                    result_code=event.result_code,
                )
            elif event.request_id == self.voice.release_request_id:
                success = event.result_code == 0
                self.voice.state = "stopped" if success else "failed"
                log_print(logger, f"[RCP-PTT-ACK] PTT RELEASE 确认: {'✓ 成功 → stopped' if success else '✗ 失败 → failed'} (result_code={event.result_code})")
                self._emit(
                    "VOICE",
                    "ptt_released" if success else "ptt_release_failed",
                    event.option,
                    channel_id=self.voice.channel_id,
                    request_id=event.request_id,
                    target_id=self.voice.target_id,
                    result_code=event.result_code,
                )

        # --- 0xB843 Broadcast Transmit Status：广播发送状态通知 ---
        elif event.option in {
            "RCP_ANALOG_BROADCASTVOICESTATUS",
            "RCP_BROADCAST_STATUS_REPORT",
            "RCP_XPTFreeRepeaterBroadcastReport",
        }:
            payload = getattr(clr_event, "_eventData", None)
            process = str(getattr(payload, "_process", ""))
            # Voice_TX 确认发射已建立；Voice_CallFailed / Voice_CallEnd 表示失败/结束
            if "Voice_TX" in process and self.voice.state != "transmitting":
                self.voice.state = "transmitting"
                self._emit(
                    "VOICE",
                    "broadcast_tx_confirmed",
                    event.option,
                    channel_id=self.voice.channel_id,
                    target_id=self.voice.target_id,
                )
            elif "Voice_CallFailed" in process:
                if self.voice.state not in {"stopped", "failed"}:
                    self.voice.state = "failed"
                self._emit(
                    "VOICE",
                    "broadcast_call_failed",
                    event.option,
                    channel_id=self.voice.channel_id,
                    target_id=self.voice.target_id,
                )
            elif "Voice_CallEnd" in process:
                if self.voice.state not in {"stopped", "failed"}:
                    self.voice.state = "stopped"
                self._emit(
                    "VOICE",
                    "broadcast_call_end",
                    event.option,
                    channel_id=self.voice.channel_id,
                    target_id=self.voice.target_id,
                )

    def _remember_observed_channel(
        self, event: AdkEvent, channel_id: int | None = None, *, discovered_by: str | None = None
    ) -> None:
        """Retain a channel seen in an inbound service callback.

        This is a recovery path for V4 installations that deliver RRS/TMP/RCP
        data before or without a separate SYSTEM_CHANNEL_CHANGED_REPORT. It does
        not create a fictional channel: a positive channel ID must already be in
        the native callback.
        """
        value = event.channel_id if channel_id is None else channel_id
        if value is None or value <= 0:
            return
        slot = self._asint(event.metadata.get("slot")) if event.metadata else None
        if value not in self.channels:
            self.channels[value] = {
                "slot": slot,
                "ip": None,
                "device_id": None,
                "discovered_by": discovered_by or f"{event.service}:{event.option}",
                "provisional": True,
            }
            self._auto_select_channel(value, slot=slot, source="adk_auto")
            self._emit(
                "SYSTEM",
                "channel_observed",
                "ADK_SERVICE_EVENT",
                channel_id=value,
                metadata=dict(self.channels[value]),
            )
        else:
            if slot is not None and self.channels[value].get("slot") is None:
                self.channels[value]["slot"] = slot
            self._auto_select_channel(
                value, slot=self.channels[value].get("slot"), source="adk_auto"
            )

    def _register(self, name: str, service_type: Any, handler: Any) -> None:
        service = self._adk.GetService(service_type)
        if service is None:
            raise AdkRuntimeError(f"ADK service {name} is unavailable")
        callback = self._types["ADKEventHandler"](handler)
        self._emit("SYSTEM", "register_attempt", "ADK_SERVICE_REGISTER", metadata={"service": name})
        try:
            service.Register(callback)
        except Exception as exc:
            raise AdkRuntimeError(
                f"ADK service Register failed for {name}: {exc}\n"
                f"bootstrap={self.cfg.bootstrap}; listener_ip={self.cfg.listener_ip}; "
                f"active_ports={dict(self.cfg.ports)}\n{self._adk_log_tail()}"
            ) from exc
        self._services[name] = service
        self._callbacks[name] = callback
        self._emit("SYSTEM", "register_success", "ADK_SERVICE_REGISTER", metadata={"service": name})


def adk_event_to_domain_event(event: AdkEvent):
    """Normalize observable ADK call facts without inventing missing identities."""

    from shared_contracts.event_models import DomainEvent
    from shared_contracts.event_types import (
        CALL_ADK_OBSERVED,
        CALL_ANSWERED,
        CALL_AUDIO_ACTIVITY,
        CALL_ENDED,
        CALL_FAILED,
        CALL_REJECTED,
        CALL_REQUESTED,
        CALL_RINGING,
        CALL_TIMEOUT,
    )

    option = str(event.option or "").upper()
    kind = str(event.kind or "").lower()
    call_related = event.service == "RCP" and (
        "CALL" in option
        or "BROADCAST" in option
        or "BUTTON_OPERATION" in option
        or any(token in kind for token in ("call", "voice", "ptt", "broadcast"))
    )
    if not call_related:
        return None

    lifecycle_state = "observed"
    event_type = CALL_ADK_OBSERVED
    if "TIMEOUT" in option or "timeout" in kind:
        lifecycle_state, event_type = "timeout", CALL_TIMEOUT
    elif "REJECT" in option or "reject" in kind:
        lifecycle_state, event_type = "rejected", CALL_REJECTED
    elif any(token in option for token in ("RELEASE", "END", "HANGUP")) or "ended" in kind:
        lifecycle_state, event_type = "ended", CALL_ENDED
    elif event.result_code not in (None, 0) or "failed" in kind:
        lifecycle_state, event_type = "failed", CALL_FAILED
    elif "CALL_REQUEST_ANSWER" in option or "ANSWER" in kind:
        lifecycle_state, event_type = "answered", CALL_ANSWERED
    elif "CALL_REQUEST" in option:
        lifecycle_state, event_type = "requested", CALL_REQUESTED
    elif "RING" in option or "ring" in kind:
        lifecycle_state, event_type = "ringing", CALL_RINGING
    elif "BUTTON_OPERATION" in option or "VOICE" in option or "PTT" in kind:
        lifecycle_state, event_type = "active", CALL_AUDIO_ACTIVITY

    metadata = dict(event.metadata or {})
    call_type = str(metadata.get("call_type") or "").strip().lower() or None
    target_id = event.target_id
    data = {
        "lifecycle_state": lifecycle_state,
        "service": event.service,
        "kind": event.kind,
        "option": event.option,
        "request_id": event.request_id,
        "observed_source_id": event.source_id,
        "channel_id": event.channel_id,
        "result_code": event.result_code,
        "call_type": call_type,
        "direction": metadata.get("direction"),
        "vendor_metadata_keys": sorted(str(key) for key in metadata),
    }
    if call_type == "group" or "GROUP" in option:
        data["group_id"] = target_id
    else:
        data["observed_target_id"] = target_id
    return DomainEvent(
        event_type=event_type,
        occurred_at=event.at.isoformat(),
        source_service="adk-gateway",
        subject=str(event.channel_id) if event.channel_id is not None else None,
        actor_id=str(event.source_id) if event.source_id is not None else None,
        data=data,
        provenance="observed",
        confidence=1.0 if event.request_id is not None else 0.8,
    )
