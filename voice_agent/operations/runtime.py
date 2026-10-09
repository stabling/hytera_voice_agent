# =============================================================================
# 【维护注释】文件：voice_agent/operations/runtime.py
# 所属层次：原子操作执行层
# 主要职责：负责把计划步骤固化为可审计、可幂等、可重试的原子操作并记录执行结果。
# 项目内直接依赖：shared_contracts.trace_context, voice_agent.commands.models, voice_agent.commands.repository, .executor, .models, .repository
# 主要公开符号：OperationRuntime, build_operation_runtime
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Application facade for command creation, atomic execution, cancellation, and queries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from shared_contracts.trace_context import extract_trace_context

from voice_agent.commands.models import Command
from voice_agent.commands.repository import CommandRepository

from .executor import OperationExecutor
from .models import AtomicOperation, OperationResult, WaitCondition
from .repository import OperationRepository


class OperationRuntime:
    def __init__(
        self,
        path: str | Path,
        *,
        executor_mode: str = "legacy",
        default_timeout_seconds: float = 60.0,
        enabled: bool = True,
        audit_enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.audit_enabled = bool(audit_enabled)
        self.commands = CommandRepository(path)
        self.operations = OperationRepository(path)
        self.executor = OperationExecutor(
            self.operations,
            mode=executor_mode,
            default_timeout_seconds=default_timeout_seconds,
        )
        self.recovery = self.operations.recover_running()
        self._postgres_reads: tuple[Any, Any] | None = None

    def _read_repositories(self) -> tuple[Any, Any]:
        if os.environ.get("STORAGE_BACKEND", "sqlite").strip().lower() != "postgres":
            return self.operations, self.commands
        if self._postgres_reads is None:
            from voice_agent.storage.runtime import get_storage_runtime
            from voice_agent.storage.postgres.repositories import PostgresOperationRepository, PostgresCommandRepository
            pool = get_storage_runtime().postgres_pool
            if pool is None:
                raise RuntimeError("PostgreSQL operation read backend is unavailable")
            tenant = os.environ.get("CONTEXT_DEFAULT_TENANT_ID", "").strip() or "default"
            self._postgres_reads = (PostgresOperationRepository(pool, tenant_id=tenant), PostgresCommandRepository(pool, tenant_id=tenant))
        return self._postgres_reads

    @staticmethod
    def _redacted_payload(data: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "event_id", "source_id", "target_id", "group_id", "channel_id", "call_id",
            "session_id", "capture_session_id", "payload_hash", "payload_bytes", "created_at",
        }
        return {str(k): data[k] for k in sorted(data) if k in allowed and data[k] is not None}

    def create_voice_command(self, task: dict[str, Any], delivery_context: Any = None) -> Command | None:
        if not self.enabled:
            return None
        data = dict(task.get("data") or {})
        context = extract_trace_context(payload=data)
        actor = (
            getattr(delivery_context, "terminal_id", None)
            if delivery_context is not None else data.get("source_id")
        )
        source_ref = str(data.get("event_id") or task.get("spool_event_id") or task.get("request_id") or task.get("task_id") or "").strip() or None
        command = Command(
            source_type="voice",
            source_ref=source_ref,
            actor_id=None if actor is None else str(actor),
            payload_redacted=self._redacted_payload(data),
            trace_id=context.trace_id,
            correlation_id=context.correlation_id,
        )
        _, stored = self.commands.create_or_get(command)
        return stored

    def create_scheduled_command(
        self,
        *,
        source_ref: str,
        actor_id: str | None,
        parent_command_id: str,
        payload_redacted: dict[str, Any],
    ) -> Command:
        command = Command(
            source_type="scheduled",
            source_ref=source_ref,
            actor_id=actor_id,
            parent_command_id=parent_command_id,
            payload_redacted=payload_redacted,
        )
        _, stored = self.commands.create_or_get(command)
        return stored

    def update_command(self, command_id: str | None, **values: Any) -> None:
        if self.enabled and command_id:
            self.commands.update(command_id, **values)

    def execute_local(
        self,
        operation: AtomicOperation,
        callback: Callable[[AtomicOperation], dict[str, Any] | None],
    ) -> OperationResult:
        _, stored = self.operations.create_or_get(operation)
        self.commands.update(stored.origin_command_id, status="RUNNING")
        result = self.executor.execute(stored, callback)
        if result.status == "SUCCEEDED":
            self.commands.update(stored.origin_command_id, status="SUCCEEDED", completed=True)
        elif result.status in {"FAILED", "CANCELLED", "TIMED_OUT", "UNKNOWN"}:
            self.commands.update(stored.origin_command_id, status="FAILED", completed=True)
        return result

    def add_wait(
        self,
        *,
        operation_id: str,
        subject_type: str,
        subject_id: str,
        predicate: str,
        timeout_at: str | None = None,
    ) -> WaitCondition:
        condition = WaitCondition(
            operation_id=operation_id,
            subject_type=subject_type,
            subject_id=subject_id,
            predicate=predicate,
            timeout_at=timeout_at,
        )
        self.operations.add_wait(condition)
        return condition

    def cancel_source(self, source_ref: str, *, reason: str) -> int:
        command = self.commands.get_by_source("voice", source_ref)
        if command is None:
            return 0
        count = self.operations.cancel_for_command(command.command_id, reason=reason)
        self.commands.update(
            command.command_id, status="CANCELLED", cancel_reason=reason, completed=True
        )
        return count

    def query_operations(self, **filters: Any) -> list[dict[str, Any]]:
        operations, _ = self._read_repositories()
        return operations.query(**filters)

    def query_commands(self, **filters: Any) -> list[dict[str, Any]]:
        _, commands = self._read_repositories()
        return commands.query(**filters)


def build_operation_runtime(
    *,
    path: str | Path,
    executor_mode: str,
    default_timeout_seconds: float,
    enabled: bool,
    audit_enabled: bool,
) -> OperationRuntime:
    return OperationRuntime(
        path,
        executor_mode=executor_mode,
        default_timeout_seconds=default_timeout_seconds,
        enabled=enabled,
        audit_enabled=audit_enabled,
    )
