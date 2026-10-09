# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/mcp_executor.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.identifiers, shared_contracts.security, shared_contracts.trace_context, shared_runtime.observability, shared_runtime.diagnostics, shared_runtime.logging_config, ., .mcp_result
# 主要公开符号：execute_tool_via_http, execute_operation, execute_plan
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""经过校验的能力执行所使用的 MCP HTTP 客户端。"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

from shared_contracts.identifiers import new_call_id
from shared_contracts.security import outbound_headers
from shared_contracts.trace_context import current_trace_context, inject_trace_headers
from shared_runtime.observability import trace_headers
from shared_runtime.diagnostics import log_trace
from shared_runtime.latency import log_latency_tree
from shared_runtime.logging_config import get_logger

from . import config
from .mcp_result import MCPResult, build_mcp_result
from .utils import perf_ms

logger = get_logger(__name__)


def _tool_error_detail(tool_result: dict[str, Any]) -> str:
    """Extract the server-provided MCP tool error without hiding its cause."""

    structured = tool_result.get("structuredContent")
    if isinstance(structured, dict):
        for key in ("error", "error_code", "message", "code"):
            value = structured.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)
    content = tool_result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                for key in ("error", "error_code", "message", "code"):
                    value = parsed.get(key)
                    if value not in (None, "", 0, "0"):
                        return str(value)
            return text[:500]
    return "MCP tool returned isError=true"


def _plan_dict(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return result if isinstance(result, dict) else None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="json")
        return result if isinstance(result, dict) else None
    return None


def execute_tool_via_http(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    base_url: str | None = None,
    timeout: float | None = None,
    request_id: int | str | None = None,
    execution_plan: Any | None = None,
    actor_terminal_id: int | str | None = None,
    channel_id: int | str | None = None,
) -> dict[str, Any]:
    """调用 MCP tools/call，并分离客户端序列化、HTTP、服务端处理和响应解码耗时。"""
    started = time.perf_counter()
    plan_dict = _plan_dict(execution_plan)
    timing_ms: dict[str, Any] = {
        "request_serialize_ms": 0.0,
        "http_ms": 0.0,
        "response_decode_ms": 0.0,
        "mcp_server_processing_ms": None,
        "transport_overhead_ms": None,
        "total_ms": 0.0,
    }
    try:
        request_payload = build_mcp_result(tool_name, arguments, request_id=request_id)
        serialize_started = time.perf_counter()
        body = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timing_ms["request_serialize_ms"] = perf_ms(serialize_started)
    except Exception as exc:
        timing_ms["total_ms"] = perf_ms(started)
        return MCPResult(ok=False, mcp_result=None, status_code=None, mcp_response=None,
                         error=f"invalid MCP tool call: {type(exc).__name__}: {exc}", error_type="validation_error",
                         timing_ms=timing_ms, execution_plan=plan_dict).to_dict()

    endpoint = f"{(base_url or config.MCP_SERVER_URL).rstrip('/')}/mcp"
    timeout_value = float(timeout if timeout is not None else config.MCP_TOOL_TIMEOUT_SECONDS)
    allocated_call_id: str | None = None
    log_trace(logger, "MCP工具调用准备发送", request_id=request_payload.get("id"), terminal_id=actor_terminal_id,
              channel_id=channel_id, tool_name=tool_name, arguments=arguments, mcp_request=request_payload, endpoint=endpoint)
    try:
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                   "MCP-Protocol-Version": config.MCP_PROTOCOL_VERSION, **outbound_headers()}
        if actor_terminal_id is not None:
            headers["X-Hytera-Actor-Terminal-Id"] = str(actor_terminal_id)
        if channel_id is not None:
            headers["X-Hytera-Channel-Id"] = str(channel_id)
        trace_context = current_trace_context()
        if tool_name == "call_start" and trace_context is not None and not trace_context.call_id:
            allocated_call_id = new_call_id()
            trace_context = replace(trace_context, call_id=allocated_call_id)
        inject_trace_headers(headers, trace_context)
        headers = trace_headers(headers)
        http_started = time.perf_counter()
        from voice_agent.http_pool import pooled_post

        log_trace(logger, "MCP工具调用发送", request_id=request_payload.get("id"), terminal_id=actor_terminal_id,
              channel_id=channel_id, tool_name=tool_name, arguments=arguments, mcp_request=request_payload, endpoint=endpoint)
        status_code, raw_response = pooled_post(
            endpoint, data=body, headers=headers, timeout=timeout_value
        )
        timing_ms["http_ms"] = perf_ms(http_started)
        decode_started = time.perf_counter()
        response_body = raw_response.decode("utf-8", errors="replace")
        response_data = json.loads(response_body) if response_body else {}
        timing_ms["response_decode_ms"] = perf_ms(decode_started)
        if not isinstance(response_data, dict):
            raise ValueError("MCP server response must be a JSON object")

        rpc_error = response_data.get("error")
        tool_result = response_data.get("result")
        structured = tool_result.get("structuredContent") if isinstance(tool_result, dict) else None
        server_timing = structured.get("timing_ms") if isinstance(structured, dict) else None
        if isinstance(server_timing, dict):
            timing_ms["mcp_server_processing_ms"] = server_timing.get("mcp_server_processing_ms")
            try:
                timing_ms["transport_overhead_ms"] = round(max(0.0, float(timing_ms["http_ms"]) - float(timing_ms["mcp_server_processing_ms"])), 3)
            except (TypeError, ValueError):
                pass
        tool_is_error = bool(isinstance(tool_result, dict) and tool_result.get("isError"))
        ok = not rpc_error and not tool_is_error
        if rpc_error:
            error, error_type = json.dumps(rpc_error, ensure_ascii=False, separators=(",", ":")), "tool_rejected"
        elif tool_is_error:
            error, error_type = _tool_error_detail(tool_result), "tool_rejected"
        else:
            error = error_type = None
        timing_ms["total_ms"] = perf_ms(started)
        result = MCPResult(ok=ok, mcp_result=request_payload, status_code=status_code, mcp_response=response_data,
                           error=error, error_type=error_type, timing_ms=timing_ms, execution_plan=plan_dict).to_dict()
        log_latency_tree(logger, "MCP_CLIENT_TIME_TREE", timing_ms, request_id=request_payload.get("id"),
                         terminal_id=actor_terminal_id, channel_id=channel_id, tool_name=tool_name)
        log_trace(logger, "MCP工具调用结果", request_id=request_payload.get("id"), terminal_id=actor_terminal_id,
                  channel_id=channel_id, tool_name=tool_name, http_status=status_code, ok=ok,
                  mcp_response=response_data, error=error, error_type=error_type, timing_ms=timing_ms)
        if allocated_call_id is not None:
            result["call_id"] = allocated_call_id
        return result
    except urllib.error.HTTPError as exc:
        timing_ms["http_ms"] = timing_ms["http_ms"] or perf_ms(started)
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(error_body) if error_body else None
        except Exception:
            error_body, parsed = "", None
        error_type = "tool_timeout" if exc.code in {408, 504} else ("tool_rejected" if 400 <= exc.code < 500 else "transport_error")
        error = f"HTTP {exc.code}: {exc.reason}; body={error_body[:500]}"
        status_code, response_data = exc.code, parsed if isinstance(parsed, dict) else None
    except urllib.error.URLError as exc:
        reason = exc.reason
        error_type = "tool_timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "transport_error"
        error, status_code, response_data = f"connection error: {reason}", None, None
    except (TimeoutError, socket.timeout) as exc:
        error_type, error, status_code, response_data = "tool_timeout", f"tool timeout: {exc}", None, None
    except Exception as exc:
        error_type, error, status_code, response_data = "unexpected_error", f"{type(exc).__name__}: {exc}", None, None
    timing_ms["total_ms"] = perf_ms(started)
    failure = MCPResult(ok=False, mcp_result=request_payload, status_code=status_code, mcp_response=response_data,
                        error=error, error_type=error_type, timing_ms=timing_ms, execution_plan=plan_dict).to_dict()
    log_latency_tree(logger, "MCP_CLIENT_TIME_TREE", timing_ms, request_id=request_payload.get("id"),
                     terminal_id=actor_terminal_id, channel_id=channel_id, tool_name=tool_name)
    log_trace(logger, "MCP工具调用异常", request_id=request_payload.get("id"), terminal_id=actor_terminal_id,
              channel_id=channel_id, tool_name=tool_name, error=error, error_type=error_type, timing_ms=timing_ms)
    return failure


def execute_operation(
    operation: Any,
    *,
    base_url: str | None = None,
    timeout: float | None = None,
    request_id: int | str | None = None,
    actor_terminal_id: int | str | None = None,
    channel_id: int | str | None = None,
) -> dict[str, Any]:
    """执行一个已经完成校验、由 MCP 支撑的原子操作。"""

    tool_name = getattr(operation, "tool_name", None)
    arguments = getattr(operation, "resolved_arguments", None) or getattr(operation, "arguments", None)
    if not tool_name:
        raise ValueError("operation.tool_name is required for MCP execution")
    if not isinstance(arguments, dict):
        raise ValueError("operation arguments must be an object")
    return execute_tool_via_http(
        str(tool_name),
        dict(arguments),
        base_url=base_url,
        timeout=timeout,
        request_id=request_id,
        actor_terminal_id=actor_terminal_id,
        channel_id=channel_id,
    )


def execute_plan(
    operations: list[Any],
    *,
    base_url: str | None = None,
    timeout: float | None = None,
    request_id: int | str | None = None,
    actor_terminal_id: int | str | None = None,
    channel_id: int | str | None = None,
) -> list[dict[str, Any]]:
    """按顺序执行 MCP 原子操作列表，遇到首个失败即停止。"""

    results: list[dict[str, Any]] = []
    for operation in operations:
        result = execute_operation(
            operation,
            base_url=base_url,
            timeout=timeout,
            request_id=request_id,
            actor_terminal_id=actor_terminal_id,
            channel_id=channel_id,
        )
        results.append(result)
        if not result.get("ok"):
            break
    return results
