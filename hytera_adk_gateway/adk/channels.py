# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/channels.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..models, .shared
# 主要公开符号：ChannelMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Any

from ..models import AdkEvent
from .shared import AdkRuntimeError


class ChannelMixin:
    """内部能力分组；由 ``HyteraAdkBackend`` 组合使用。"""

    def configured_channel_defaults(self) -> dict[str, Any]:
        defaults = {
            "default": self._cfg_default_channel_id(),
            "slot1": self._cfg_slot_default(1),
            "slot2": self._cfg_slot_default(2),
        }
        return {
            "mode": self.channel_mode,
            "runtime_override": self._channel_mode_override,
            "fallback_for_voice": self._fallback_for_voice(),
            "ids": {
                name: None if value is None else f"0x{value:08X}"
                for name, value in defaults.items()
            },
            "note": (
                "Configured defaults are fallback routing inputs. They are not inserted into channels and do not prove "
                "the repeater API path is online."
            ),
        }

    @property
    def channel_mode(self) -> str:
        return getattr(self, "_channel_mode_override", None) or str(
            getattr(getattr(self, "cfg", None), "channel_mode", "auto")
        )

    def set_channel_mode(self, mode: str) -> str:
        normalized = mode.strip().lower().replace("-", "_")
        if normalized not in {"auto", "default", "auto_then_default"}:
            raise ValueError("Channel mode must be auto, default, or auto_then_default")
        self._channel_mode_override = normalized
        self._emit("SYSTEM", "channel_mode_changed", "CHANNEL_MODE", metadata={"mode": normalized})
        return normalized

    def transport_state(self) -> dict[str, Any]:
        if self.channels:
            state = "channel_discovered"
            action = "Automatic discovery is active. Use routes/channels emitted by ADK; do not enter CPS aliases such as D3."
        elif self._fallback_available():
            state = "configured_channel_fallback_only"
            action = (
                "No ADK runtime channel has been observed. The configured default can be attempted, but it does not "
                "prove a live repeater transport. Update it from the vendor demo if ADK returns -11/-1."
            )
        elif any(self.event_counts.get(service, 0) for service in ("RRS", "TMP", "RCP")):
            state = "api_traffic_without_system_channel_report"
            action = "Run with --debug-fields and preserve the received RRS/TMP/RCP event JSON; callback channel IDs are used when present."
        else:
            state = "no_repeater_to_pc_api_traffic_observed"
            action = "Verify repeater CPS Forward to PC, Third Party Server IP, API ports, write/reboot, and UDP capture."
        return {
            "state": state,
            "selected_channel": None
            if self.selected_channel is None
            else f"0x{self.selected_channel:08X}",
            "selected_channel_source": self.selected_channel_source,
            "selected_channel_slot": self._channel_slot(self.selected_channel),
            "channels": self.channels,
            "radio_routes": self.radio_routes,
            "event_counts": self.event_counts,
            "channel_fallback": self.configured_channel_defaults(),
            "rtp_capture": {
                "mode": "external_tshark",
                "in_backend_capture_removed": True,
                "capture_root": str(self.voice_capture_root),
                "send_root": str(self.voice_send_root),
                "note": "RTP/media bytes are no longer captured by ADK callback, ADK receive-port, or Python raw socket in backend.py. Use voice_capture.py / voice_routes.py.",
            },
            "next_action": action,
        }

    def _emit(self, service: str, kind: str, option: str, **kwargs: Any) -> None:
        self._record_event(AdkEvent(service, kind, option, **kwargs))

    def _cfg_default_channel_id(self) -> int | None:
        value = getattr(getattr(self, "cfg", None), "default_channel_id", None)
        return int(value) if value is not None and int(value) > 0 else None

    def _cfg_slot_default(self, slot: int) -> int | None:
        name = "default_slot1_channel_id" if slot == 1 else "default_slot2_channel_id"
        value = getattr(getattr(self, "cfg", None), name, None)
        return int(value) if value is not None and int(value) > 0 else None

    def _fallback_for_voice(self) -> bool:
        return bool(getattr(getattr(self, "cfg", None), "fallback_for_voice", False))

    def _fallback_available(self) -> bool:
        return self._cfg_default_channel_id() is not None

    def _configured_default_channel(self, slot: int | None = None) -> int | None:
        if slot is not None:
            return self._cfg_slot_default(slot)
        return self._cfg_default_channel_id()

    def _is_configured_default(self, channel_id: int) -> bool:
        return channel_id in {
            value
            for value in (
                self._cfg_default_channel_id(),
                self._cfg_slot_default(1),
                self._cfg_slot_default(2),
            )
            if value is not None
        }

    def _channel_slot(self, channel_id: int | None) -> int | None:
        if channel_id is None:
            return None
        info = self.channels.get(int(channel_id))
        if not info:
            return None
        return self._asint(info.get("slot"))

    def _auto_select_channel(
        self, channel_id: int, *, slot: int | None = None, source: str = "adk_auto"
    ) -> None:
        """Automatically select an observed channel, preferring TDMA slot 1.

        Server mode should not let a later slot-2 report overwrite the slot-1
        channel. Manual selections and configured defaults remain operator-owned.
        """
        if not self.cfg.auto_channel:
            return
        if getattr(self, "selected_channel_source", None) in {
            "manual_runtime",
            "configured_default",
        }:
            return
        value = int(channel_id)
        slot_value = self._asint(slot)
        current = getattr(self, "selected_channel", None)
        current_slot = self._channel_slot(current)
        if current is None or current not in self.channels:
            self.selected_channel = value
            self.selected_channel_source = source
            return
        if current_slot == 1 and slot_value != 1:
            return
        if slot_value == 1 and current != value:
            self.selected_channel = value
            self.selected_channel_source = source
            return
        if current_slot is None and slot_value is not None and current != value:
            # Prefer a channel whose slot is known over a provisional unknown.
            self.selected_channel = value
            self.selected_channel_source = source

    def _preferred_auto_channel(self) -> int | None:
        if self.selected_channel is not None and int(self.selected_channel) in self.channels:
            return int(self.selected_channel)
        for channel_id, info in self.channels.items():
            if self._asint(info.get("slot")) == 1:
                return int(channel_id)
        if len(self.channels) == 1:
            return int(next(iter(self.channels)))
        if self.channels:
            return int(next(iter(self.channels)))
        return None

    def _channel_source(self, channel_id: int, *, target_id: int | None = None) -> str:
        if channel_id == 0:
            return "adk_auto_route"
        if (
            target_id is not None
            and self.radio_routes.get(target_id) == channel_id
            and channel_id in self.channels
        ):
            return "radio_route"
        if channel_id in self.channels:
            if (
                self.selected_channel == channel_id
                and getattr(self, "selected_channel_source", None) == "manual_runtime"
            ):
                return "manual_runtime"
            return "adk_discovered"
        if self._is_configured_default(channel_id):
            return "configured_default"
        return "manual_runtime"

    def _seed_configured_route(self, radio_id: int, channel_id: int) -> None:
        try:
            self._adk.ConfigurationRoute(
                self._types["UInt32"](radio_id), self._types["UInt32"](channel_id)
            )
            self.radio_routes[radio_id] = channel_id
            self._emit(
                "SYSTEM",
                "route_seeded_from_configured_default",
                "CONFIGURATION_ROUTE",
                channel_id=channel_id,
                source_id=radio_id,
                metadata={"channel": f"0x{channel_id:08X}", "reason": "configured_default"},
            )
        except Exception as exc:
            # ConfigurationRoute is advisory here. SendCommand still determines
            # whether this runtime accepts the configured fallback channel.
            self._emit(
                "SYSTEM",
                "configured_default_route_seed_failed",
                "CONFIGURATION_ROUTE",
                channel_id=channel_id,
                source_id=radio_id,
                metadata={"error": str(exc)},
            )

    def _private_text_channel(self, value: int | None, target_id: int) -> int | None:
        if value is not None:
            return self._validate_requested_channel(int(value), allow_default=True)
        # A deliberate ``channel default`` command overrides automatic selection.
        if (
            self.selected_channel is not None
            and getattr(self, "selected_channel_source", None) == "configured_default"
        ):
            return self.selected_channel
        # A per-terminal route is the strongest signal.
        route = self.radio_routes.get(target_id)
        if route is not None and int(route) in self.channels:
            return int(route)
        # Any currently selected runtime channel (manual or auto) is preferred over channel_id=0
        # because some V4 DLL builds reject SendCommand with channel_id=0 as
        # PACKET_ERROR_BUFFER_INVALID (-1). We always want a concrete ChannelID.
        preferred = self._preferred_auto_channel()
        if preferred is not None:
            return preferred

        mode = self.channel_mode
        default = self._configured_default_channel()
        if mode == "default":
            return default
        if mode == "auto_then_default":
            return default
        return None

    def _validate_requested_channel(self, value: int, *, allow_default: bool) -> int | None:
        if value in self.channels:
            return value
        if allow_default and self._is_configured_default(value):
            return value
        return None

    def _concrete_channel(
        self, value: int | None = None, *, allow_default: bool = True
    ) -> int | None:
        if value is not None:
            return self._validate_requested_channel(int(value), allow_default=allow_default)
        # Explicit operator choice is honoured before auto selection.
        if self.selected_channel is not None:
            if self.selected_channel in self.channels:
                return self.selected_channel
            if (
                allow_default
                and getattr(self, "selected_channel_source", None) == "configured_default"
                and self._is_configured_default(self.selected_channel)
            ):
                return self.selected_channel

        mode = self.channel_mode
        default = self._configured_default_channel() if allow_default else None
        if mode == "default":
            return default
        preferred = self._preferred_auto_channel()
        if preferred is not None:
            return preferred
        if mode == "auto_then_default":
            return default
        return None

    def select_channel(self, channel_id: int | None) -> int | None:
        if channel_id is None:
            self.selected_channel = None
            self.selected_channel_source = None
            return None
        value = int(channel_id)
        if value not in self.channels:
            raise AdkRuntimeError(
                f"Channel 0x{value:X} is not an ADK-discovered runtime channel. "
                "Use `channel default` for a configured fallback; D3 is a CPS alias, not an ADK channel ID."
            )
        self.selected_channel = value
        self.selected_channel_source = "manual_runtime"
        return value

    def select_default_channel(self, slot: int | None = None) -> int:
        if slot not in {None, 1, 2}:
            raise ValueError("Default slot must be 1 or 2")
        value = self._configured_default_channel(slot)
        if value is None:
            target = "default_channel_id" if slot is None else f"default_slot{slot}_channel_id"
            raise AdkRuntimeError(
                f"No configured {target}; edit [channel] in config/gateway.ini first."
            )
        self.selected_channel = value
        self.selected_channel_source = "configured_default"
        self._emit(
            "SYSTEM",
            "configured_default_selected",
            "CHANNEL_DEFAULT",
            channel_id=value,
            metadata={"slot": slot, "channel": f"0x{value:08X}"},
        )
        return value

    def _channel(self, value: int | None, *, allow_default: bool = False) -> int:
        """Resolve a concrete active channel required by Audio/PTT/RTP operations."""
        channel = self._concrete_channel(value, allow_default=allow_default)
        if channel is None:
            fallback_note = (
                " Configure channel.fallback_for_voice=true to intentionally allow a configured default ChannelID."
                if self._fallback_available() and not allow_default
                else ""
            )
            raise AdkRuntimeError(
                "No concrete ADK runtime channel is available for voice/RTP. Wait for repeater-to-PC API traffic "
                "or select a configured default. CPS alias D3 is not an ADK channel ID."
                + fallback_note
            )
        return channel
