# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/device_executor.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：shared_contracts.security, shared_contracts.trace_context, shared_runtime.observability, shared_runtime.diagnostics, ., .execution_context, .tool_catalog
# 主要公开符号：DeviceExecutor, PrintStubExecutor, HttpForwardExecutor, GatewayTerminalExecutor, build_executor
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""MCP 工具调用与真实设备执行之间的边界层。"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict

from shared_contracts.security import outbound_headers
from shared_contracts.trace_context import inject_trace_headers
from shared_runtime.observability import trace_headers
from shared_runtime.diagnostics import log_trace

from . import config
from .execution_context import current_execution_context
from .tool_catalog import ToolSpec

logger = logging.getLogger(__name__)


class DeviceExecutor(ABC):
    @abstractmethod
    def execute(self, tool_name: str, arguments: Dict[str, Any], spec: ToolSpec) -> Dict[str, Any]:
        raise NotImplementedError


class PrintStubExecutor(DeviceExecutor):
    """开发阶段桩执行器，仅打印调用信息，不代表真实设备已经执行。"""

    def execute(self, tool_name: str, arguments: Dict[str, Any], spec: ToolSpec) -> Dict[str, Any]:
        event = {
            "ts_ms": int(time.time() * 1000),
            "executor": "print_stub",
            "tool_name": tool_name,
            "arguments": arguments,
            "resource": spec.resource,
            "permission": spec.permission,
        }
        logger.info("DEVICE_TOOL_STUB | %s", json.dumps(event, ensure_ascii=False, sort_keys=True))
        return {
            "ok": False,
            "code": -501,
            "stub": True,
            "tool_name": tool_name,
            "arguments": arguments,
            "error": "device_execution_disabled",
            "message": "printed_only_not_executed",
            "accepted": False,
            "dispatched": False,
            "device_confirmed": False,
        }


class HttpForwardExecutor(DeviceExecutor):
    """把工具调用转发给设备侧 HTTP 实现。"""

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or config.DEVICE_HTTP_BASE_URL).rstrip("/")
        if not self.base_url:
            raise ValueError("DEVICE_HTTP_BASE_URL is required for http_forward mode")
        self.timeout = timeout or config.DEVICE_HTTP_TIMEOUT

    def execute(self, tool_name: str, arguments: Dict[str, Any], spec: ToolSpec) -> Dict[str, Any]:
        url = f"{self.base_url}/tools/{tool_name}"
        body = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **outbound_headers()}
        inject_trace_headers(headers)
        headers = trace_headers(headers)
        try:
            from voice_agent.http_pool import pooled_post

            _status, resp_raw = pooled_post(url, data=body, headers=headers, timeout=self.timeout)
            resp_body = resp_raw.decode("utf-8", errors="replace")
            value = json.loads(resp_body) if resp_body else {"code": 0}
            return value if isinstance(value, dict) else {"ok": True, "result": value}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "code": exc.code, "error": f"device http error: {exc.code}", "tool_name": tool_name}
        except Exception as exc:
            return {"ok": False, "code": -1, "error": f"device request failed: {exc}", "tool_name": tool_name}


class GatewayTerminalExecutor(DeviceExecutor):
    """通过网关把 protobuf 工具请求一次性下发到终端。

    当前终端没有返回独立的“设备已执行”确认，因此二进制请求成功下发
    作为本层完成边界；后续若终端返回明确确认，则继续原样向上透传。
    """

    def execute(self, tool_name: str, arguments: Dict[str, Any], spec: ToolSpec) -> Dict[str, Any]:
        from voice_agent.terminal_delivery import deliver_mcp_result_to_terminal
        from voice_agent.request_ids import normalize_request_id

        context = current_execution_context()
        actor = str(context.get("actor_terminal_id") or "").strip()
        if not actor.isdigit() or int(actor) <= 0:
            return {"ok": False, "code": -400, "error": "actor_terminal_id_required", "device_confirmed": False}
        channel_raw = str(context.get("channel_id") or "").strip()
        channel_id = int(channel_raw) if channel_raw.isdigit() and int(channel_raw) > 0 else None
        envelope = {
            "jsonrpc": "2.0",
            "id": normalize_request_id(context.get("request_id")),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        log_trace(
            logger,
            "设备执行边界准备下发",
            request_id=envelope["id"],
            terminal_id=int(actor),
            channel_id=channel_id,
            tool_name=tool_name,
            arguments=arguments,
            mcp_envelope=envelope,
            delivery_type="protobuf_private_sms",
            adk_method="TMP.SendCommand",
        )
        delivery = deliver_mcp_result_to_terminal(
            envelope,
            target_id=int(actor),
            channel_id=channel_id,
            encode=True,
        )
        accepted = bool(delivery.get("ok"))
        log_trace(
            logger,
            "设备执行边界下发结果",
            request_id=envelope["id"],
            terminal_id=int(actor),
            channel_id=channel_id,
            tool_name=tool_name,
            accepted=accepted,
            delivery=delivery,
        )
        return {
            "ok": accepted,
            "code": 0 if accepted else -1,
            "tool_name": tool_name,
            "arguments": arguments,
            "accepted": accepted,
            "dispatched": accepted,
            "device_confirmed": bool(delivery.get("device_confirmed") is True),
            "confirmation_status": delivery.get("confirmation_status"),
            "success_basis": "protobuf_binary_dispatched" if accepted else None,
            "delivery": delivery,
            "timing_ms": dict(delivery.get("timing_ms") or {}),
            "message": "protobuf_binary_dispatched" if accepted else "terminal_dispatch_failed",
        }


def build_executor() -> DeviceExecutor:
    mode = config.DEVICE_EXECUTOR_MODE.strip().lower()
    if mode == "print_stub":
        return PrintStubExecutor()
    if mode == "http_forward":
        return HttpForwardExecutor()
    if mode == "gateway_terminal":
        return GatewayTerminalExecutor()
    raise ValueError(f"Unsupported DEVICE_EXECUTOR_MODE={config.DEVICE_EXECUTOR_MODE!r}")
