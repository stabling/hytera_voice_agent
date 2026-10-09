# =============================================================================
# 【维护注释】文件：voice_agent/operations/executor.py
# 所属层次：原子操作执行层
# 主要职责：负责把计划步骤固化为可审计、可幂等、可重试的原子操作并记录执行结果。
# 项目内直接依赖：shared_contracts.models, shared_contracts.trace_context, .models, .repository
# 主要公开符号：OperationExecutor
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Unified executor that audits local, MCP, query, content, and delivery operations."""

from __future__ import annotations

import socket
from typing import Any, Callable

from shared_contracts.models import utc_now_iso
from shared_contracts.trace_context import TraceContext, current_trace_context, use_trace_context

from .models import AtomicOperation, OperationResult
from .repository import OperationRepository

ExecuteCallable = Callable[[AtomicOperation], dict[str, Any] | None]


def _nested_confirmation(value: Any) -> bool:
    """Accept device confirmation only when an explicit confirmation field exists."""
    if isinstance(value, dict):
        for key in ("device_confirmed", "accepted_by_device", "adk_confirmed"):
            if value.get(key) is True:
                return True
        return any(_nested_confirmation(item) for item in value.values())
    if isinstance(value, list):
        return any(_nested_confirmation(item) for item in value)
    return False


class OperationExecutor:
    def __init__(
        self,
        repository: OperationRepository,
        *,
        mode: str = "legacy",
        default_timeout_seconds: float = 60.0,
    ) -> None:
        normalized = str(mode).strip().lower()
        if normalized not in {"legacy", "shadow", "atomic"}:
            raise ValueError("operation executor mode must be legacy, shadow, or atomic")
        self.repository = repository
        self.mode = normalized
        self.default_timeout_seconds = max(0.1, float(default_timeout_seconds))

    def execute(
        self,
        operation: AtomicOperation,
        execute: ExecuteCallable,
    ) -> OperationResult:
        existing_result = self.repository.final_result(operation.operation_id)
        if existing_result is not None:
            return existing_result.model_copy(update={"cached": True})
        stored = self.repository.get(operation.operation_id)
        if stored is not None and stored.status in {"CANCELLED", "UNKNOWN", "FAILED", "TIMED_OUT"}:
            return OperationResult(
                operation_id=stored.operation_id,
                ok=False,
                status=stored.status,
                error_code=stored.status,
                error=f"operation is already {stored.status}",
                cached=True,
            )
        if not self.repository.claim(operation.operation_id):
            cached = self.repository.final_result(operation.operation_id)
            if cached is not None:
                return cached.model_copy(update={"cached": True})
            current = self.repository.get(operation.operation_id) or operation
            return OperationResult(
                operation_id=current.operation_id,
                ok=current.status in {"ACCEPTED", "DISPATCHED", "DEVICE_CONFIRMED", "SUCCEEDED"},
                status=current.status,
                error_code=None if current.status not in {"FAILED", "CANCELLED", "TIMED_OUT", "UNKNOWN"} else current.status,
                cached=True,
            )
        attempt = self.repository.start_attempt(operation.operation_id, self.mode)
        incoming = current_trace_context()
        context = TraceContext(
            trace_id=None if incoming is None else incoming.trace_id,
            correlation_id=None if incoming is None else incoming.correlation_id,
            causation_id=None if incoming is None else incoming.causation_id,
            command_id=operation.origin_command_id,
            operation_id=operation.operation_id,
            call_id=None if incoming is None else incoming.call_id,
        )
        response: dict[str, Any] | None = None
        try:
            with use_trace_context(context):
                raw = execute(operation)
            response = raw if isinstance(raw, dict) else {"ok": raw is not None, "result": raw}
            ok = bool(response.get("ok"))
            status_code = response.get("status_code")
            accepted = bool(ok or (isinstance(status_code, int) and 200 <= status_code < 300))
            dispatched = bool(accepted and (response.get("mcp_response") is not None or operation.executor_kind != "mcp"))
            device_confirmed = _nested_confirmation(response)
            if not ok:
                status = "FAILED"
            elif operation.side_effect:
                # Current terminal protocol has no execution confirmation.
                # A successfully dispatched protobuf request is the completion
                # boundary; explicit confirmation remains an optional upgrade.
                status = "SUCCEEDED" if (dispatched or device_confirmed) else "ACCEPTED"
            else:
                status = "SUCCEEDED"
            result = OperationResult(
                operation_id=operation.operation_id,
                ok=ok,
                status=status,
                accepted=accepted,
                dispatched=dispatched,
                device_confirmed=device_confirmed,
                final_result=response.get("result") if isinstance(response.get("result"), dict) else None,
                response=response,
                error_code=response.get("error_type"),
                error=response.get("error"),
                completed_at=utc_now_iso(),
            )
            self.repository.finish_attempt(
                attempt.attempt_id,
                accepted=accepted,
                dispatched=dispatched,
                device_confirmed=device_confirmed,
                response=response,
                error_code=result.error_code,
                error_message=result.error,
            )
            self.repository.transition(
                operation.operation_id,
                status,
                reason="execution_completed" if ok else "execution_failed",
                final_result=result.model_dump(mode="json"),
            )
            return result
        except (TimeoutError, socket.timeout) as exc:
            status = "UNKNOWN" if operation.side_effect else "TIMED_OUT"
            result = OperationResult(
                operation_id=operation.operation_id,
                ok=False,
                status=status,
                error_code="tool_timeout",
                error=str(exc),
            )
            self.repository.finish_attempt(
                attempt.attempt_id,
                accepted=None,
                dispatched=None,
                device_confirmed=None,
                response=response,
                error_code=result.error_code,
                error_message=result.error,
            )
            self.repository.transition(
                operation.operation_id, status, reason="execution_timeout",
                final_result=result.model_dump(mode="json"),
            )
            return result
        except Exception as exc:
            result = OperationResult(
                operation_id=operation.operation_id,
                ok=False,
                status="FAILED",
                error_code=type(exc).__name__,
                error=str(exc),
            )
            self.repository.finish_attempt(
                attempt.attempt_id,
                accepted=False,
                dispatched=False,
                device_confirmed=False,
                response=response,
                error_code=result.error_code,
                error_message=result.error,
            )
            self.repository.transition(
                operation.operation_id, "FAILED", reason="executor_exception",
                final_result=result.model_dump(mode="json"),
            )
            return result
