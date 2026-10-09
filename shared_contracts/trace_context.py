# =============================================================================
# 【维护注释】文件：shared_contracts/trace_context.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：.identifiers
# 主要公开符号：TraceContext, propagation_enabled, parse_traceparent, make_traceparent, extract_trace_context, inject_trace_headers, current_trace_context, use_trace_context
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Dependency-free W3C Trace Context propagation helpers."""

from __future__ import annotations

import contextlib
import contextvars
import os
from dataclasses import dataclass
from typing import Iterator, Mapping, MutableMapping

from .identifiers import (
    new_span_id,
    new_trace_id,
    normalize_identifier,
    normalize_span_id,
    normalize_trace_id,
)

TRACEPARENT_HEADER = "traceparent"
HYTERA_TRACE_HEADER = "X-Hytera-Trace-ID"
CORRELATION_HEADER = "X-Correlation-ID"
CAUSATION_HEADER = "X-Hytera-Causation-ID"
COMMAND_HEADER = "X-Hytera-Command-ID"
OPERATION_HEADER = "X-Hytera-Operation-ID"
CALL_HEADER = "X-Hytera-Call-ID"


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    correlation_id: str | None = None
    causation_id: str | None = None
    command_id: str | None = None
    operation_id: str | None = None
    call_id: str | None = None


_CURRENT_TRACE_CONTEXT: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "hytera_trace_context", default=None
)


def propagation_enabled() -> bool:
    return os.environ.get("OTEL_PROPAGATION_ENABLE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def parse_traceparent(value: object) -> tuple[str, str] | None:
    text = str(value or "").strip().lower()
    parts = text.split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None
    trace_id = normalize_trace_id(parts[1])
    span_id = normalize_span_id(parts[2])
    if trace_id is None or span_id is None or len(parts[3]) != 2:
        return None
    return trace_id, span_id


def make_traceparent(trace_id: str, *, sampled: bool = True) -> str:
    normalized = normalize_trace_id(trace_id)
    if normalized is None:
        raise ValueError("trace_id must be 32 lowercase hex characters and non-zero")
    return f"00-{normalized}-{new_span_id()}-{'01' if sampled else '00'}"


def _header_value(headers: Mapping[str, object] | None, name: str) -> object | None:
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return value
    return None


def extract_trace_context(
    headers: Mapping[str, object] | None = None,
    payload: Mapping[str, object] | None = None,
    *,
    generate: bool = True,
) -> TraceContext:
    payload = payload or {}
    parsed = parse_traceparent(_header_value(headers, TRACEPARENT_HEADER))
    trace_id = (
        (parsed[0] if parsed else None)
        or normalize_trace_id(_header_value(headers, HYTERA_TRACE_HEADER))
        or normalize_trace_id(payload.get("trace_id"))
        or (new_trace_id() if generate else None)
    )
    if trace_id is None:
        raise ValueError("trace context is missing")
    correlation_id = normalize_identifier(
        _header_value(headers, CORRELATION_HEADER) or payload.get("correlation_id")
    )
    return TraceContext(
        trace_id=trace_id,
        correlation_id=correlation_id,
        causation_id=normalize_identifier(
            _header_value(headers, CAUSATION_HEADER) or payload.get("causation_id")
        ),
        command_id=normalize_identifier(
            _header_value(headers, COMMAND_HEADER) or payload.get("command_id")
        ),
        operation_id=normalize_identifier(
            _header_value(headers, OPERATION_HEADER) or payload.get("operation_id")
        ),
        call_id=normalize_identifier(_header_value(headers, CALL_HEADER) or payload.get("call_id")),
    )


def inject_trace_headers(
    headers: MutableMapping[str, str],
    context: TraceContext | None = None,
    *,
    enabled: bool | None = None,
) -> MutableMapping[str, str]:
    if enabled is None:
        enabled = propagation_enabled()
    if not enabled:
        return headers
    context = context or _CURRENT_TRACE_CONTEXT.get()
    if context is None:
        return headers
    headers[TRACEPARENT_HEADER] = make_traceparent(context.trace_id)
    headers[HYTERA_TRACE_HEADER] = context.trace_id
    if context.correlation_id:
        headers[CORRELATION_HEADER] = context.correlation_id
    if context.causation_id:
        headers[CAUSATION_HEADER] = context.causation_id
    if context.command_id:
        headers[COMMAND_HEADER] = context.command_id
    if context.operation_id:
        headers[OPERATION_HEADER] = context.operation_id
    if context.call_id:
        headers[CALL_HEADER] = context.call_id
    return headers


def current_trace_context() -> TraceContext | None:
    return _CURRENT_TRACE_CONTEXT.get()


@contextlib.contextmanager
def use_trace_context(context: TraceContext) -> Iterator[TraceContext]:
    token = _CURRENT_TRACE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_TRACE_CONTEXT.reset(token)
