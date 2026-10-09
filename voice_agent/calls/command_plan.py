# =============================================================================
# 【维护注释】文件：voice_agent/calls/command_plan.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：.content_service
# 主要公开符号：ExactCallContentPlan, ExactCallPlanExecutor
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Exact-call content plans used by explicit compound commands.

A plan is always bound to a concrete call_id returned by the same call.start
operation.  It never represents an unscoped future-call subscription.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .content_service import CallContentService


@dataclass(frozen=True)
class ExactCallContentPlan:
    operation_id: str
    call_id: str
    actor_terminal_id: str
    operation_type: str
    target_language: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


class ExactCallPlanExecutor:
    def __init__(self, service: CallContentService, *, timeout_seconds: float = 300.0) -> None:
        self.service = service
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._lock = threading.RLock()
        self._results: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, threading.Thread] = {}

    def schedule(self, plan: ExactCallContentPlan) -> dict[str, Any]:
        if not plan.call_id:
            raise ValueError("exact call_id is required")
        with self._lock:
            if plan.operation_id in self._results or plan.operation_id in self._threads:
                return {"ok": True, "duplicate": True, "operation_id": plan.operation_id}
            thread = threading.Thread(
                target=self._run,
                args=(plan,),
                name=f"exact-call-content-{plan.operation_id[-16:]}",
                daemon=True,
            )
            self._threads[plan.operation_id] = thread
            thread.start()
        return {
            "ok": True,
            "duplicate": False,
            "operation_id": plan.operation_id,
            "call_id": plan.call_id,
        }

    def result(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._results.get(str(operation_id))
            return None if value is None else dict(value)

    def _run(self, plan: ExactCallContentPlan) -> None:
        try:
            completed = self.service.wait_until_complete(
                plan.call_id, timeout_seconds=self.timeout_seconds
            )
            if not completed:
                result = {
                    "ok": False,
                    "operation_id": plan.operation_id,
                    "call_id": plan.call_id,
                    "error": "CALL_WAIT_TIMEOUT",
                }
            elif plan.operation_type == "call.translate":
                result = self.service.translate(
                    call_id=plan.call_id,
                    actor_terminal_id=plan.actor_terminal_id,
                    operation_id=plan.operation_id,
                    target_language=str(plan.target_language or "en"),
                    options=plan.options,
                )
            else:
                result = self.service.summarize(
                    call_id=plan.call_id,
                    actor_terminal_id=plan.actor_terminal_id,
                    operation_id=plan.operation_id,
                    options=plan.options,
                )
        except Exception as exc:  # fail-open background boundary
            result = {
                "ok": False,
                "operation_id": plan.operation_id,
                "call_id": plan.call_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        with self._lock:
            self._results[plan.operation_id] = dict(result)
            self._threads.pop(plan.operation_id, None)
