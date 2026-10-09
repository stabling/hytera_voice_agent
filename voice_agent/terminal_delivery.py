# =============================================================================
# 【维护注释】文件：voice_agent/terminal_delivery.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.routes, shared_contracts.security, shared_contracts.trace_context, shared_runtime.observability, shared_runtime.diagnostics, .utils, shared_runtime.logging_config
# 主要公开符号：deliver_mcp_result_to_terminal
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from mcp_wire import mcp_bridge
from mcp_wire.tool_registry import TOOL_NAME_TO_ID
from voice_agent.mcp_http_server.tool_catalog import ToolCatalog
from voice_agent.mcp_http_server.wire_compat import canonical_wire_invocation
from voice_agent.request_ids import normalize_request_id
from shared_contracts.routes import (
    DEFAULT_GATEWAY_BASE_URL,
    GATEWAY_PRIVATE_SMS_PATH,
    GATEWAY_PROTOBUF_RESULT_SMS_PATH,
)
from shared_contracts.security import outbound_headers
from shared_contracts.trace_context import inject_trace_headers
from shared_runtime.observability import trace_headers
from shared_runtime.diagnostics import bytes_snapshot, log_trace
from shared_runtime.latency import log_latency_tree

from .utils import perf_ms
from shared_runtime.logging_config import get_logger

logger = get_logger(__name__)
_WIRE_TOOL_CATALOG = ToolCatalog()


def _validated_wire_request(mcp_result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """严格执行 tools -> ToolCatalog -> mcp_bridge 前的 canonical 校验。"""
    if not isinstance(mcp_result, dict):
        raise ValueError("MCP request must be an object")
    params = mcp_result.get("params")
    if not isinstance(params, dict):
        raise ValueError("MCP request params must be an object")
    tool_name = str(params.get("name") or "").strip()
    arguments = params.get("arguments") or {}
    # 统一旧/新参数名（call_type/callee_id -> target_type/target_id 等），
    # 保证旧调用链在 v0.2 附件契约下仍能通过校验。
    tool_name, arguments = canonical_wire_invocation(tool_name, arguments)
    if not _WIRE_TOOL_CATALOG.contains(tool_name):
        raise ValueError(f"unknown tools catalog capability: {tool_name!r}")
    spec = _WIRE_TOOL_CATALOG.get(tool_name)
    if spec.executor_kind == "local":
        raise ValueError(f"local capability {tool_name!r} cannot enter mcp_bridge")
    if tool_name not in TOOL_NAME_TO_ID:
        raise ValueError(f"tool {tool_name!r} has no mcp_bridge wire ToolId")
    normalized = _WIRE_TOOL_CATALOG.validate_arguments(tool_name, dict(arguments))
    numeric_request_id = normalize_request_id(mcp_result.get("id"))
    wire_request = {
        "jsonrpc": "2.0",
        "id": numeric_request_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": normalized},
    }
    return wire_request, {
        "request_id": mcp_result.get("id"),
        "numeric_request_id": numeric_request_id,
        "tool_name": tool_name,
        "tool_id": int(TOOL_NAME_TO_ID[tool_name]),
        "arguments": normalized,
        "wire_format": "mcp_bridge_cbor_l2_be",
    }


# v0.2 线协议选项：reply=1 保留旧 qos=1 语义；mtu=0 关闭分片，
# 保证单次投递 body 恰好是一个完整帧（与旧单帧投递行为一致）。
_WIRE_ENCODE_OPTIONS = {"version": 0, "type": 0, "encoding": 0, "reply": 1, "mtu": 0}


def encode_tool_call(mcp_result: dict[str, Any]) -> bytes:
    wire_request, _ = _validated_wire_request(mcp_result)
    frames = mcp_bridge.encodeToolCall(wire_request, _WIRE_ENCODE_OPTIONS)
    if len(frames) != 1:
        raise ValueError(
            f"mcp_bridge expected one un-fragmented frame, got {len(frames)}"
        )
    return frames[0]


def inspect_tool_call_encoding(
    mcp_result: dict[str, Any],
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    wire_request, context = _validated_wire_request(mcp_result)
    frames = mcp_bridge.encodeToolCall(wire_request, _WIRE_ENCODE_OPTIONS)
    if len(frames) != 1:
        raise ValueError(
            f"mcp_bridge expected one un-fragmented frame, got {len(frames)}"
        )
    wire_bytes = frames[0]
    decoded = mcp_bridge.decodeToolRequest(wire_bytes)
    if len(decoded) != 1:
        raise ValueError(f"mcp_bridge decode expected one request, got {len(decoded)}")
    return wire_bytes, context, dict(decoded[0])


_ORIGINAL_ENCODE_TOOL_CALL = encode_tool_call

_ADK_RESULT_SMS_PATH = GATEWAY_PROTOBUF_RESULT_SMS_PATH
_SMS_PRIVATE_PATH = GATEWAY_PRIVATE_SMS_PATH
_FIXED_RESULT_SMS_TARGET_ID = "5"
_DYNAMIC_TARGET_HEADER = "X-Hytera-Target-Id"
_DYNAMIC_CHANNEL_HEADER = "X-Hytera-Channel-Id"
_DEFAULT_ADK_GATEWAY_URL = os.environ.get("HYTERA_ADK_BASE_URL", DEFAULT_GATEWAY_BASE_URL).rstrip("/")
_DEFAULT_ADK_RESULT_SMS_TIMEOUT_SECONDS = 10.0


def _validate_radio_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("target_id must be an integer")
    if not 1 <= value <= 16_777_215:
        raise ValueError("target_id must be 1..16777215")
    return value


def _validate_optional_channel_id(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("channel_id must be an integer")
    if not 1 <= value <= 0xFFFFFFFF:
        raise ValueError("channel_id must be 1..4294967295")
    return value


def _delivery_timing(
    *,
    total_started: float,
    protobuf_encode_ms: float = 0.0,
    http_ms: float = 0.0,
    response_decode_ms: float = 0.0,
    gateway_timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gateway = dict(gateway_timing or {})
    return {
        "protobuf_encode_ms": round(float(protobuf_encode_ms or 0.0), 3),
        "agent_to_gateway_http_ms": round(float(http_ms or 0.0), 3),
        "gateway_processing_ms": gateway.get("gateway_processing_ms"),
        "adk_send_command_ms": gateway.get("adk_send_command_ms"),
        "response_decode_ms": round(float(response_decode_ms or 0.0), 3),
        "total_ms": perf_ms(total_started),
    }


def _send_mcp_result_via_adk(
    mcp_result: dict[str, Any],
    *,
    target_id: int | None,
    channel_id: int | None,
    adk_base_url: str | None,
    timeout: float | None,
    encode: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    base_url = (adk_base_url or _DEFAULT_ADK_GATEWAY_URL).rstrip("/")
    timeout_value = float(
        timeout if timeout is not None else os.environ.get(
            "HYTERA_ADK_RESULT_SMS_TIMEOUT_SECONDS", _DEFAULT_ADK_RESULT_SMS_TIMEOUT_SECONDS
        )
    )
    expected_target_id = str(target_id) if target_id is not None else _FIXED_RESULT_SMS_TARGET_ID
    if not encode:
        return _send_notification_sms(
            mcp_result, base_url=base_url, target_id=target_id, channel_id=channel_id,
            expected_target_id=expected_target_id, timeout_value=timeout_value, t0=t0,
        )

    url = f"{base_url}{_ADK_RESULT_SMS_PATH}"
    headers = {"Content-Type": "application/octet-stream", "Accept": "application/json", **outbound_headers()}
    if target_id is not None:
        headers[_DYNAMIC_TARGET_HEADER] = str(target_id)
    if channel_id is not None:
        headers[_DYNAMIC_CHANNEL_HEADER] = str(channel_id)
    inject_trace_headers(headers)
    headers = trace_headers(headers)

    encode_started = time.perf_counter()
    if encode_tool_call is _ORIGINAL_ENCODE_TOOL_CALL:
        encode_wire_bytes, encode_context, decoded_request = inspect_tool_call_encoding(mcp_result)
    else:
        encode_wire_bytes = encode_tool_call(mcp_result)
        encode_context = {
            "request_id": mcp_result.get("id"),
            "tool_name": (mcp_result.get("params") or {}).get("name"),
            "compatibility_encoder_override": True,
        }
        decoded_request = {"status": "未执行回读：当前编码器已被测试或兼容代码替换"}
    protobuf_encode_ms = perf_ms(encode_started)
    log_trace(
        logger, "MCP到Protobuf编码链路", request_id=mcp_result.get("id"), terminal_id=target_id,
        channel_id=channel_id, tool_name=(mcp_result.get("params") or {}).get("name"),
        adk_http_path=_ADK_RESULT_SMS_PATH, adk_method="TMP.SendCommand", protobuf_before=mcp_result,
        protobuf_encode_context=encode_context, protobuf_after=bytes_snapshot(encode_wire_bytes),
        protobuf_decoded=decoded_request, protobuf_encode_ms=protobuf_encode_ms, delivery_status="sending",
    )
    try:
        http_started = time.perf_counter()
        from voice_agent.http_pool import pooled_post

        _status, raw = pooled_post(url, data=encode_wire_bytes, headers=headers, timeout=timeout_value)
        http_ms = perf_ms(http_started)
        decode_started = time.perf_counter()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        response_decode_ms = perf_ms(decode_started)
        if not isinstance(parsed, dict):
            parsed = {"response": parsed}
        gateway_timing = dict(parsed.get("timing_ms") or {})
        parsed.setdefault("ok", True)
        parsed.setdefault("target_type", "private")
        parsed["target_id"] = expected_target_id if target_id is not None else parsed.get("target_id", expected_target_id)
        parsed.setdefault("content", json.dumps(mcp_result, ensure_ascii=False, separators=(",", ":")))
        parsed["request_bytes"] = len(encode_wire_bytes)
        parsed["delivery_status"] = "binary_dispatched" if parsed.get("ok") else "failed"
        parsed["accepted"] = bool(parsed.get("ok"))
        parsed["dispatched"] = bool(parsed.get("ok"))
        parsed["device_confirmed"] = bool(parsed.get("device_confirmed") is True)
        parsed["confirmation_status"] = "confirmed" if parsed["device_confirmed"] else "not_returned"
        parsed["success_basis"] = "protobuf_binary_dispatched" if parsed.get("ok") else None
        parsed["timing_ms"] = _delivery_timing(
            total_started=t0, protobuf_encode_ms=protobuf_encode_ms, http_ms=http_ms,
            response_decode_ms=response_decode_ms, gateway_timing=gateway_timing,
        )
        log_latency_tree(logger, "TERMINAL_DELIVERY_TIME_TREE", parsed["timing_ms"],
                         request_id=mcp_result.get("id"), terminal_id=expected_target_id, channel_id=channel_id)
        log_trace(logger, "Protobuf控制短信网关返回", request_id=mcp_result.get("id"),
                  terminal_id=expected_target_id, channel_id=channel_id,
                  tool_name=(mcp_result.get("params") or {}).get("name"), adk_method="TMP.SendCommand",
                  gateway_response=parsed)
        return parsed
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return {"ok": False, "stage": "adk_send", "status_code": exc.code, "target_type": "private",
                "target_id": expected_target_id, "content": "", "request_bytes": len(encode_wire_bytes),
                "delivery_status": "failed", "error": f"HTTP {exc.code}: {exc.reason}; body={error_body[:500]}",
                "timing_ms": _delivery_timing(total_started=t0, protobuf_encode_ms=protobuf_encode_ms)}
    except urllib.error.URLError as exc:
        return {"ok": False, "stage": "adk_send", "status_code": None, "target_type": "private",
                "target_id": expected_target_id, "content": "", "request_bytes": len(encode_wire_bytes),
                "delivery_status": "failed", "error": f"connection error: {exc.reason}",
                "timing_ms": _delivery_timing(total_started=t0, protobuf_encode_ms=protobuf_encode_ms)}
    except Exception as exc:
        return {"ok": False, "stage": "adk_send", "status_code": None, "target_type": "private",
                "target_id": expected_target_id, "content": "", "request_bytes": len(encode_wire_bytes),
                "delivery_status": "failed", "error": f"{type(exc).__name__}: {exc}",
                "timing_ms": _delivery_timing(total_started=t0, protobuf_encode_ms=protobuf_encode_ms)}


def _send_notification_sms(
    mcp_result: dict[str, Any],
    *,
    base_url: str,
    target_id: int | None,
    channel_id: int | None,
    expected_target_id: str,
    timeout_value: float,
    t0: float,
) -> dict[str, Any]:
    """通知型短信：直接通过 /sms/private 发送原文，不经过 protobuf 转换。"""
    params = mcp_result.get("params") or {}
    args = params.get("arguments") or {}
    content = args.get("content")
    text = str(content) if content is not None else json.dumps(mcp_result, ensure_ascii=False, separators=(",", ":"))
    log_trace(logger, "通知型私人短信准备发送", request_id=mcp_result.get("id"), terminal_id=expected_target_id,
              channel_id=channel_id, tool_name=params.get("name"), adk_http_path=_SMS_PRIVATE_PATH,
              adk_method="TMP.SendCommand", hex_encode=False, text=text, delivery_status="sending")
    url = f"{base_url}{_SMS_PRIVATE_PATH}"
    sms_payload: dict[str, Any] = {"target_id": int(expected_target_id), "text": text, "hex_encode": False}
    if channel_id is not None:
        sms_payload["channel_id"] = channel_id
    encode_started = time.perf_counter()
    body = json.dumps(sms_payload, ensure_ascii=False).encode("utf-8")
    encode_ms = perf_ms(encode_started)
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", **outbound_headers()}
    inject_trace_headers(headers)
    headers = trace_headers(headers)
    try:
        http_started = time.perf_counter()
        from voice_agent.http_pool import pooled_post

        _status, raw = pooled_post(url, data=body, headers=headers, timeout=timeout_value)
        http_ms = perf_ms(http_started)
        decode_started = time.perf_counter()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        response_decode_ms = perf_ms(decode_started)
        if not isinstance(parsed, dict):
            parsed = {"response": parsed}
        ok = bool(parsed.get("ok"))
        gateway_timing = ((parsed.get("data") or {}).get("timing_ms") or {}) if isinstance(parsed.get("data"), dict) else {}
        result = {
            "ok": ok, "target_type": "private", "target_id": expected_target_id, "content": text,
            "request_bytes": len(body), "delivery_status": "submitted_to_adk" if ok else "failed",
            "accepted": ok, "dispatched": ok, "device_confirmed": False,
            "timing_ms": _delivery_timing(total_started=t0, protobuf_encode_ms=encode_ms, http_ms=http_ms,
                                           response_decode_ms=response_decode_ms, gateway_timing=gateway_timing),
            "adk_result": parsed.get("data"),
        }
        log_latency_tree(logger, "TERMINAL_DELIVERY_TIME_TREE", result["timing_ms"],
                         request_id=mcp_result.get("id"), terminal_id=expected_target_id, channel_id=channel_id)
        log_trace(logger, "通知型私人短信网关返回", request_id=mcp_result.get("id"), terminal_id=expected_target_id,
                  channel_id=channel_id, adk_method="TMP.SendCommand", gateway_response=parsed, delivery_result=result)
        return result
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        error = f"HTTP {exc.code}: {exc.reason}; body={error_body[:500]}"
        status_code = exc.code
    except urllib.error.URLError as exc:
        error, status_code = f"connection error: {exc.reason}", None
    except Exception as exc:
        error, status_code = f"{type(exc).__name__}: {exc}", None
    return {"ok": False, "stage": "adk_send", "status_code": status_code, "target_type": "private",
            "target_id": expected_target_id, "content": text, "request_bytes": len(body), "delivery_status": "failed",
            "error": error, "timing_ms": _delivery_timing(total_started=t0, protobuf_encode_ms=encode_ms)}


def deliver_mcp_result_to_terminal(
    mcp_result: dict[str, Any],
    *,
    target_id: int,
    channel_id: int | None = None,
    adk_base_url: str | None = None,
    timeout: float | None = None,
    encode: bool = True,
) -> dict[str, Any]:
    """把一个标准 MCP 请求封装发送到指定源终端。

    参数：
        mcp_result: 标准 ``tools/call`` JSON-RPC 对象。
        target_id: 目标终端 Radio ID。
        channel_id: 可选 ADK runtime channel_id。
        adk_base_url: 可选 ADK 网关地址。
        timeout: 可选发送超时时间。
        encode: 为 True 时进行 protobuf 编码；为 False 时按普通文本私信发送。

    返回：
        网关提交结果及设备确认状态。
    """
    normalized_target = _validate_radio_id(target_id)
    normalized_channel = _validate_optional_channel_id(channel_id)
    return _send_mcp_result_via_adk(
        mcp_result,
        target_id=normalized_target,
        channel_id=normalized_channel,
        adk_base_url=adk_base_url,
        timeout=timeout,
        encode=encode,
    )
