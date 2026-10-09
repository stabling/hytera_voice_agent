# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/reflection.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..models, .shared
# 主要公开符号：ReflectionMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import base64
from typing import Any

from ..models import AdkEvent
from .shared import AdkRuntimeError, ClrReflectionMethod


class ReflectionMixin:
    """内部能力分组；由 ``HyteraAdkBackend`` 组合使用。"""

    def _decode(self, service: str, clr_event: Any) -> AdkEvent:
        payload = getattr(clr_event, "_eventData", None)
        option = self._option_name(getattr(clr_event, "_optioncode", "UNKNOWN"))
        text = self._first_attr(payload, "_content", "_msg", "_text")
        if text is not None:
            text = str(text)
        # V4 documentation describes _srcID/_destID on ServiceEvent. Prefer
        # those direct fields; fall back to payload naming used by other ADK builds.
        # 需求 §7：先 ADK/CLR 明确字段（source/srcID），再兼容 sender/caller/calling/from
        # 与 receiver/target/destination/callee/called/to 命名变体。
        source_id = self._asint(
            self._first_attr(
                clr_event,
                "_srcID",
                "_sourceID",
                "_sourceId",
                "_senderID",
                "_senderId",
                "_srcId",
                "_callerID",
                "_callerId",
                "_callingID",
                "_callingId",
                "_fromID",
                "_fromId",
            )
        )
        if source_id is None:
            source_id = self._asint(
                self._first_attr(
                    payload,
                    "_sourceID",
                    "_sourceId",
                    "_senderID",
                    "_senderId",
                    "_srcID",
                    "_srcId",
                    "_radioID",
                    "_radioId",
                    "_callerID",
                    "_callerId",
                    "_callingID",
                    "_callingId",
                    "_fromID",
                    "_fromId",
                )
            )
        target_id = self._asint(
            self._first_attr(
                clr_event,
                "_destID",
                "_DestID",
                "_targetID",
                "_targetId",
                # Hytera CLR spells the target field "_targatID" on RCP
                # BroadcaseRepeaterStateReport payloads; without these
                # candidates the inbound private-call receiver (the repeater)
                # would be lost and later re-invented from the caller ID.
                "_targatID",
                "_targatId",
                "_TargatID",
                "_receiverID",
                "_receiverId",
                "_destinationID",
                "_destinationId",
                "_calleeID",
                "_calleeId",
                "_calledID",
                "_calledId",
                "_toID",
                "_toId",
            )
        )
        if target_id is None:
            target_id = self._asint(
                self._first_attr(
                    payload,
                    "_targetID",
                    "_targetId",
                    # See the clr_event branch: Hytera CLR exposes the private
                    # call receiver as "_targatID" (vendor spelling).
                    "_targatID",
                    "_targatId",
                    "_TargatID",
                    "_destID",
                    "_DestID",
                    "_receiverID",
                    "_receiverId",
                    "_destinationID",
                    "_destinationId",
                    "_calleeID",
                    "_calleeId",
                    "_calledID",
                    "_calledId",
                    "_toID",
                    "_toId",
                )
            )
        result = self._asint(self._first_attr(payload, "_result", "_resultCode", "_Result"))
        request_id = self._asint(self._first_attr(clr_event, "_requestID", "_requestId"))
        channel_id = self._asint(self._first_attr(clr_event, "_channelID", "_channelId"))
        if channel_id is None:
            channel_id = self._asint(self._first_attr(payload, "_channelID", "_channelId"))
        metadata: dict[str, Any] = {
            "remote_ip": self._asint(self._first_attr(clr_event, "_remoteIP", "_remoteIp")),
            "local_ip": self._asint(self._first_attr(clr_event, "_localIP", "_localIp")),
            "slot": self._asint(self._first_attr(payload, "_soltID", "_slotID", "_slot", "_Slot")),
            "payload_type": type(payload).__name__ if payload is not None else None,
        }
        binary_payload = self._first_bytes(
            payload,
            clr_event,
            "_data",
            "_payload",
            "_packet",
            "_bytes",
            "_audioData",
            "_voiceData",
            "_rtpData",
            "_RTPData",
            "_pcm",
            "_pcmData",
            "_contentBytes",
        )
        if binary_payload:
            metadata["binary_payload_b64"] = base64.b64encode(binary_payload).decode("ascii")
            metadata["binary_payload_size"] = len(binary_payload)

        # RCP carries call/PTT/repeater state reports used by voice control.
        # Do not hide its vendor CLR payload behind --debug-fields: /voice/info
        # must expose the actual received RCP object content for field work.
        if service == "RCP":
            metadata["rcp_actual"] = {
                "option": option,
                "option_code_raw": self._safe_scalar(getattr(clr_event, "_optioncode", None)),
                "event_type": self._clr_type_name(clr_event),
                "payload_type": self._clr_type_name(payload),
                "event": self._object_snapshot(clr_event, depth=2),
                "payload": self._object_snapshot(payload, depth=3),
            }
            metadata["rcp_event_fields"] = self._fields(clr_event)
            metadata["rcp_payload_fields"] = self._fields(payload)

        if self.debug_fields:
            metadata["payload_fields"] = self._fields(payload)
            metadata["event_fields"] = self._fields(clr_event)
        incoming_options = {
            "TMP_PRIVATE_NEED_ACK_REQUEST",
            "TMP_PRIVATE_NO_NEED_ACK_REQUEST",
            "TMP_GROUP_REQUEST",
            "TMP_GROUP_NO_ACK_NEED",
        }
        result_options = {"TMP_PRIVATE_ANSWER", "TMP_GROUP_ANSWER"}
        kind = (
            "message_received"
            if service == "TMP" and option in incoming_options
            else "send_result"
            if service == "TMP" and option in result_options
            else "service_event"
        )
        return AdkEvent(
            service,
            kind,
            option,
            channel_id=channel_id,
            request_id=request_id,
            source_id=source_id,
            target_id=target_id,
            text=text,
            result_code=result,
            metadata=metadata,
        )

    def _service_enum(self, *names: str) -> Any:
        return self._enum_member(self._types["ServiceType"], *names)

    def _option_enum(self, *names: str) -> Any:
        return self._enum_member(self._types["OptionCode"], *names)

    def _call_type_enum(self, call_type: str) -> Any:
        if call_type == "private":
            return self._enum_member(
                self._types["CallType"], "PRIVATE", "PRIVATE_CALL", "CallType_PRIVITE_CALL"
            )
        return self._enum_member(
            self._types["CallType"], "GROUP", "GROUP_CALL", "CallType_GROUP_CALL"
        )

    @staticmethod
    def _enum_member(enum_type: Any, *names: str) -> Any:
        for name in names:
            try:
                return getattr(enum_type, name)
            except AttributeError:
                continue
        raise AdkRuntimeError(
            f"Vendor CLR enum {getattr(enum_type, '__name__', enum_type)} has none of {names}"
        )

    def _option_name(self, value: Any) -> str:
        try:
            numeric = int(value)
        except Exception:
            numeric = None
        names = getattr(self, "_option_names", {})
        if numeric is not None and numeric in names:
            return names[numeric]
        return self._enum_name(value)

    @staticmethod
    def _enum_name(value: Any) -> str:
        try:
            name = str(value)
        except Exception:
            return "UNKNOWN"
        return name.rsplit(".", 1)[-1]

    @staticmethod
    def _first_attr(obj: Any, *names: str) -> Any:
        if obj is None:
            return None
        for name in names:
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if value is not None:
                return value
        return None

    @staticmethod
    def _safe_repr(value: Any, limit: int = 200) -> str:
        try:
            text = repr(value)
        except Exception:
            text = f"<{type(value).__name__}>"
        return text if len(text) <= limit else text[:limit] + "..."

    @staticmethod
    def _coerce_bytes(value: Any) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, memoryview):
            return value.tobytes()
        try:
            if hasattr(value, "ToArray"):
                return bytes(int(item) & 0xFF for item in value.ToArray())
        except Exception:
            pass
        try:
            if hasattr(value, "Length") and str(type(value)).find("Byte") >= 0:
                return bytes(int(item) & 0xFF for item in value)
        except Exception:
            return None
        return None

    @classmethod
    def _first_bytes(cls, *objects_and_names: Any) -> bytes | None:
        if len(objects_and_names) < 2:
            return None
        *objects, names = objects_and_names
        if not isinstance(names, tuple):
            # Called as _first_bytes(obj1, obj2, "_data", "_payload", ...).
            objects = list(objects_and_names[:2])
            names = tuple(objects_and_names[2:])
        for obj in objects:
            if obj is None:
                continue
            for name in names:
                try:
                    raw = getattr(obj, name)
                except Exception:
                    continue
                data = cls._coerce_bytes(raw)
                if data:
                    return data
        return None

    def _emit_audio(self, kind: str, channel_id: int, metadata: dict[str, Any]) -> None:
        self._emit("AUDIO", kind, "PLAY_CHANNEL_SOUND", channel_id=channel_id, metadata=metadata)

    def _record_event(self, event: AdkEvent) -> None:
        self.last_event = event
        self._event_history.append(event)
        self.event_counts[event.service] = self.event_counts.get(event.service, 0) + 1
        self._events.put_nowait(event)

    def recent_events(self, limit: int = 30) -> list[AdkEvent]:
        if limit < 1:
            return []
        return list(self._event_history)[-limit:]

    def _require(self) -> None:
        if not self._started:
            raise AdkRuntimeError("ADK backend is not started")

    @staticmethod
    def _check_id(value: int) -> None:
        if not isinstance(value, int) or not 1 <= value <= 16_777_215:
            raise ValueError("DMR ID must be an integer from 1 to 16777215")

    @staticmethod
    def _check_text(text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text cannot be empty")

    @staticmethod
    def _asint(value: Any) -> int | None:
        try:
            return None if value is None else int(value)
        except Exception:
            return None

    @classmethod
    def _fields(cls, obj: Any) -> dict[str, str]:
        if obj is None:
            return {}
        try:
            return {
                str(field.Name): cls._safe_str(field.GetValue(obj))
                for field in cls._clr_fields(obj)
            }
        except Exception:
            return {}

    @staticmethod
    def _safe_str(value: Any) -> str:
        try:
            return str(value)
        except Exception:
            return "<unprintable>"

    @staticmethod
    def _safe_scalar(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        try:
            return int(value)
        except Exception:
            return str(value)

    @staticmethod
    def _clr_type_name(obj: Any) -> str | None:
        if obj is None:
            return None
        try:
            typ = obj.GetType()
            return str(getattr(typ, "FullName", None) or getattr(typ, "Name", None) or typ)
        except Exception:
            return type(obj).__name__

    @staticmethod
    def _clr_fields(obj: Any) -> list[Any]:
        if obj is None:
            return []
        try:
            import System  # type: ignore

            flags = (
                System.Reflection.BindingFlags.Instance
                | System.Reflection.BindingFlags.Public
                | System.Reflection.BindingFlags.NonPublic
                | System.Reflection.BindingFlags.FlattenHierarchy
            )
            return list(obj.GetType().GetFields(flags))
        except Exception:
            try:
                return list(obj.GetType().GetFields())
            except Exception:
                return []

    @classmethod
    def _object_snapshot(cls, obj: Any, *, depth: int = 2, max_fields: int = 120) -> dict[str, Any]:
        if obj is None:
            return {"present": False}
        snapshot: dict[str, Any] = {
            "present": True,
            "python_type": type(obj).__name__,
            "clr_type": cls._clr_type_name(obj),
            "string": cls._safe_str(obj),
            "fields": {},
        }
        fields: dict[str, Any] = {}
        for index, field in enumerate(cls._clr_fields(obj)):
            if index >= max_fields:
                fields["__truncated__"] = f"more than {max_fields} fields"
                break
            name = str(getattr(field, "Name", f"field_{index}"))
            try:
                raw = field.GetValue(obj)
            except Exception as exc:
                fields[name] = {"error": f"GetValue failed: {type(exc).__name__}: {exc}"}
                continue
            fields[name] = cls._jsonable_value(raw, depth=depth - 1)
        snapshot["fields"] = fields
        return snapshot

    @classmethod
    def _jsonable_value(cls, value: Any, *, depth: int = 1) -> Any:
        if value is None or isinstance(value, (str, bool, int, float)):
            return value

        data = cls._coerce_bytes(value)
        if data is not None:
            return {
                "type": "bytes",
                "size": len(data),
                "base64": base64.b64encode(data).decode("ascii"),
            }

        if isinstance(value, dict):
            return {
                str(k): cls._jsonable_value(v, depth=depth - 1) for k, v in list(value.items())[:80]
            }
        if isinstance(value, (list, tuple)):
            return [cls._jsonable_value(item, depth=depth - 1) for item in list(value)[:80]]

        # Most CLR scalar values and enums can be represented both as int and string.
        try:
            numeric = int(value)
        except Exception:
            numeric = None
        if numeric is not None and not hasattr(value, "GetType"):
            return numeric

        if depth > 0 and hasattr(value, "GetType") and cls._clr_fields(value):
            return cls._object_snapshot(value, depth=depth, max_fields=80)

        result: dict[str, Any] = {
            "type": type(value).__name__,
            "clr_type": cls._clr_type_name(value),
            "string": cls._safe_str(value),
        }
        if numeric is not None:
            result["int"] = numeric
        return result

    def _first_method(self, target: Any, *names: str) -> Any | None:
        """优先取 pythonnet 暴露的方法，必要时回退到 CLR 反射。"""
        if target is None:
            return None
        for name in names:
            candidate = getattr(target, name, None)
            if callable(candidate):
                return candidate
        try:
            methods = target.GetType().GetMethods()
        except Exception:
            return None
        wanted = {name.lower() for name in names}
        for method in methods:
            method_name = str(getattr(method, "Name", ""))
            if method_name.lower() in wanted:
                return ClrReflectionMethod(target, method, method_name)
        return None

    def _available_rtp_methods(self) -> list[str]:
        """列出 ADKFramework 上可能的 RTP 发送方法，便于定位 CLR 版本差异。"""
        if self._adk is None:
            return []
        names: set[str] = set()
        for name in dir(self._adk):
            if "rtp" in name.lower() and callable(getattr(self._adk, name, None)):
                names.add(name)
        try:
            for method in self._adk.GetType().GetMethods():
                name = str(getattr(method, "Name", ""))
                if "rtp" in name.lower():
                    names.add(name)
        except Exception:
            pass
        return sorted(names)
