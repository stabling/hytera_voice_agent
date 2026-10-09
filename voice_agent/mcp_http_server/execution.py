# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/execution.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：shared_contracts.event_models, shared_contracts.event_types, shared_contracts.identifiers, shared_contracts.models, shared_contracts.trace_context, voice_agent.calls.runtime, voice_agent.context.event_publisher, .
# 主要公开符号：result_is_error, execute_tool_with_events
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from shared_contracts.event_models import DomainEvent
from shared_contracts.event_types import (
    CALL_ENDED,
    CALL_FAILED,
    CALL_STARTED,
    OPERATION_COMPLETED,
    OPERATION_FAILED,
    OPERATION_REQUESTED,
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_REQUESTED,
)
from shared_contracts.identifiers import new_call_id, new_command_id, new_event_id, new_operation_id
from shared_contracts.models import utc_now_iso
from shared_contracts.trace_context import TraceContext, use_trace_context

from voice_agent.calls.runtime import build_call_store_runtime
from voice_agent.context.event_publisher import build_event_publisher

from . import config
from .tool_service import RadioToolService

EVENT_PUBLISHER = build_event_publisher(source_service="mcp-server", default_path="data/state.sqlite3")
CALL_STORE_RUNTIME = build_call_store_runtime(default_path="data/state.sqlite3")
_ACTIVE_CALL_IDS: set[str] = set()
_ACTIVE_CALL_IDS_LOCK = threading.RLock()


def result_is_error(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False:
        return True
    return result.get("code") not in (None, 0, "0")


def _call_data(tool_name: str, arguments: dict[str, Any], result: object, request_id: object | None) -> dict[str, Any]:
    call_type = str(arguments.get("call_type") or "unknown").strip().lower()
    target = str(arguments.get("callee_id") or arguments.get("target_id") or "").strip() or None
    result_request_id = None
    if isinstance(result, dict):
        result_request_id = result.get("request_id") or result.get("call_request_id")
    data: dict[str, Any] = {
        "tool_name": tool_name,
        "direction": "outbound",
        "call_type": call_type if call_type in {"private", "group", "standby", "broadcast"} else "unknown",
        "request_id": str(result_request_id or request_id or "").strip() or None,
        "channel_id": arguments.get("channel_id"),
        "talk_mode": arguments.get("talk_mode"),
    }
    data["group_id" if call_type == "group" else "observed_target_id"] = target
    if isinstance(result, dict):
        data["result_code"] = result.get("code")
        data["accepted"] = result.get("accepted")
    return data


def _is_side_effect(service: RadioToolService, tool_name: str) -> bool:
    spec = service.catalog.get(tool_name)
    if bool((spec.annotations or {}).get("readOnlyHint")):
        return False
    return str(spec.permission).upper() == "WRITE"


def execute_tool_with_events(
    service: RadioToolService,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    trace_context: TraceContext,
    request_id: object | None = None,
) -> dict[str, Any]:
    """Execute one MCP tool while keeping operation facts semantically precise.

    A device-side write is only marked completed when the device explicitly
    confirms it. Gateway acceptance alone remains a dispatched operation.
    """
    command_id = trace_context.command_id or new_command_id()
    operation_id = trace_context.operation_id or new_operation_id()
    call_id = trace_context.call_id
    if tool_name == "call_start" and not call_id:
        call_id = new_call_id()
    elif tool_name == "call_end" and not call_id:
        with _ACTIVE_CALL_IDS_LOCK:
            if len(_ACTIVE_CALL_IDS) == 1:
                call_id = next(iter(_ACTIVE_CALL_IDS))

    requested = DomainEvent(
        event_id=new_event_id(),
        event_type=TOOL_REQUESTED,
        occurred_at=utc_now_iso(),
        source_service="mcp-server",
        subject=tool_name,
        trace_id=trace_context.trace_id,
        correlation_id=trace_context.correlation_id,
        causation_id=trace_context.causation_id,
        command_id=command_id,
        operation_id=operation_id,
        call_id=call_id,
        data={
            "tool_name": tool_name,
            "argument_keys": sorted(str(key) for key in arguments),
            "request_id": str(request_id) if request_id is not None else None,
            "executor_mode": config.DEVICE_EXECUTOR_MODE,
        },
    )
    EVENT_PUBLISHER.publish(requested)
    operation_requested = DomainEvent(
        event_type=OPERATION_REQUESTED,
        source_service="mcp-server",
        subject=tool_name,
        trace_id=trace_context.trace_id,
        correlation_id=trace_context.correlation_id,
        causation_id=requested.event_id,
        command_id=command_id,
        operation_id=operation_id,
        call_id=call_id,
        data={"tool_name": tool_name, "attempt_status": "dispatched"},
    )
    EVENT_PUBLISHER.publish(operation_requested)
    execution_context = replace(
        trace_context,
        causation_id=operation_requested.event_id,
        command_id=command_id,
        operation_id=operation_id,
        call_id=call_id,
    )

    try:
        with use_trace_context(execution_context):
            result = service.execute_tool(tool_name, arguments)
    except Exception as exc:
        failed = DomainEvent(
            event_type=OPERATION_FAILED,
            source_service="mcp-server",
            subject=tool_name,
            trace_id=trace_context.trace_id,
            correlation_id=trace_context.correlation_id,
            causation_id=operation_requested.event_id,
            command_id=command_id,
            operation_id=operation_id,
            call_id=call_id,
            data={"tool_name": tool_name, "error_type": type(exc).__name__},
        )
        EVENT_PUBLISHER.publish(failed)
        EVENT_PUBLISHER.publish(DomainEvent(
            event_type=TOOL_FAILED,
            source_service="mcp-server",
            subject=tool_name,
            trace_id=trace_context.trace_id,
            correlation_id=trace_context.correlation_id,
            causation_id=failed.event_id,
            command_id=command_id,
            operation_id=operation_id,
            call_id=call_id,
            data={"tool_name": tool_name, "error_type": type(exc).__name__},
        ))
        if tool_name in {"call_start", "call_end"}:
            fact = DomainEvent(
                event_type=CALL_FAILED,
                source_service="mcp-server",
                subject=tool_name,
                trace_id=trace_context.trace_id,
                correlation_id=trace_context.correlation_id,
                causation_id=failed.event_id,
                command_id=command_id,
                operation_id=operation_id,
                call_id=call_id,
                data={**_call_data(tool_name, arguments, {}, request_id), "error_type": type(exc).__name__},
            )
            EVENT_PUBLISHER.publish(fact)
            CALL_STORE_RUNTIME.consume(fact)
        raise

    failed = result_is_error(result)
    side_effect = _is_side_effect(service, tool_name)
    device_confirmed = bool(isinstance(result, dict) and result.get("device_confirmed") is True)
    dispatched = bool(isinstance(result, dict) and result.get("dispatched") is True)
    # Current terminals have no execution-confirmation path. For side effects,
    # successful protobuf dispatch is considered complete. Future confirmation
    # can still be supplied through device_confirmed without changing this API.
    completed = not failed and (not side_effect or dispatched or device_confirmed)
    operation_status = "FAILED" if failed else ("SUCCEEDED" if completed else "ACCEPTED")

    operation_fact: DomainEvent | None = None
    if failed or completed:
        operation_fact = DomainEvent(
            event_type=OPERATION_FAILED if failed else OPERATION_COMPLETED,
            source_service="mcp-server",
            subject=tool_name,
            trace_id=trace_context.trace_id,
            correlation_id=trace_context.correlation_id,
            causation_id=operation_requested.event_id,
            command_id=command_id,
            operation_id=operation_id,
            call_id=call_id,
            data={
                "tool_name": tool_name,
                "operation_status": operation_status,
                "device_confirmed": device_confirmed,
                "dispatched": dispatched,
                "success_basis": (
                    "device_confirmation" if device_confirmed else
                    "protobuf_binary_dispatched" if dispatched else None
                ),
                "result_code": result.get("code") if isinstance(result, dict) else None,
            },
        )
        EVENT_PUBLISHER.publish(operation_fact)

    tool_fact = DomainEvent(
        event_type=TOOL_FAILED if failed else TOOL_COMPLETED,
        source_service="mcp-server",
        subject=tool_name,
        trace_id=trace_context.trace_id,
        correlation_id=trace_context.correlation_id,
        causation_id=(operation_fact.event_id if operation_fact else operation_requested.event_id),
        command_id=command_id,
        operation_id=operation_id,
        call_id=call_id,
        data={
            "tool_name": tool_name,
            "operation_status": operation_status,
            "device_confirmed": device_confirmed,
            "dispatched": dispatched,
            "success_basis": (
                "device_confirmation" if device_confirmed else
                "protobuf_binary_dispatched" if dispatched else None
            ),
            "result_code": result.get("code") if isinstance(result, dict) else None,
        },
    )
    EVENT_PUBLISHER.publish(tool_fact)

    if tool_name in {"call_start", "call_end"} and (failed or completed):
        event_type = CALL_FAILED if failed else (CALL_STARTED if tool_name == "call_start" else CALL_ENDED)
        fact = DomainEvent(
            event_type=event_type,
            source_service="mcp-server",
            subject=tool_name,
            trace_id=trace_context.trace_id,
            correlation_id=trace_context.correlation_id,
            causation_id=tool_fact.event_id,
            command_id=command_id,
            operation_id=operation_id,
            call_id=call_id,
            data=_call_data(tool_name, arguments, result, request_id),
        )
        EVENT_PUBLISHER.publish(fact)
        CALL_STORE_RUNTIME.consume(fact)
        if not failed and call_id:
            with _ACTIVE_CALL_IDS_LOCK:
                if tool_name == "call_start":
                    _ACTIVE_CALL_IDS.add(call_id)
                else:
                    _ACTIVE_CALL_IDS.discard(call_id)

    return result
