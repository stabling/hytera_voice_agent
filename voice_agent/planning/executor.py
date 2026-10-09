# =============================================================================
# 【维护注释】文件：voice_agent/planning/executor.py
# 所属层次：计划治理层
# 主要职责：负责计划模型、校验、持久化、策略衔接、等待条件和计划执行前治理。
# 项目内直接依赖：.models
# 主要公开符号：PlanExecutionResult, BoundedPlanExecutor
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Bounded stable execution loop for validated/authorized plans."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import OperationPlan, PlanStep

StepExecutor = Callable[[PlanStep, dict[str, Any]], dict[str, Any]]
ReplanCallable = Callable[[OperationPlan, PlanStep, dict[str, Any]], OperationPlan]


def _parallel_steps_enabled() -> bool:
    # 无依赖步骤并发执行开关：设备调用是 IO 密集（MCP HTTP），并发可把
    # 多步无依赖计划的总耗时从串行叠加压缩到单步耗时。设
    # PLAN_PARALLEL_STEPS=false 可回退到严格串行。
    value = str(os.environ.get("PLAN_PARALLEL_STEPS", "true")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _step_references_pending(step: PlanStep, completed: set[str]) -> bool:
    """步骤参数是否引用了尚未完成的步骤输出（如 ${step_1.call_id}）。

    depends_on 未声明但存在输出引用时，并发执行会因引用缺失而失败；
    此类步骤不进入并发批次，退回串行顺序保证。
    """

    def _contains(value: Any) -> bool:
        if isinstance(value, dict):
            return any(_contains(v) for v in value.values())
        if isinstance(value, list):
            return any(_contains(v) for v in value)
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            ref_step = value[2:value.find(".")] if "." in value[2:-1] else value[2:-1]
            return ref_step not in completed
        return False

    return _contains(step.arguments)


@dataclass
class PlanExecutionResult:
    status: str
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    replan_count: int = 0
    wait: dict[str, Any] | None = None
    timing_ms: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "status": self.status, "outputs": self.outputs,
            "completed_steps": self.completed_steps, "failed_step": self.failed_step,
            "error": self.error, "replan_count": self.replan_count, "wait": self.wait,
            "timing_ms": self.timing_ms,
        }


def _resolve(value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        return {k: _resolve(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, outputs) for v in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        expr = value[2:-1]
        step_id, _, path = expr.partition(".")
        current: Any = outputs.get(step_id, {})
        for part in path.split(".") if path else ():
            if not isinstance(current, dict) or part not in current:
                raise KeyError(f"unresolved plan reference: {value}")
            current = current[part]
        return current
    return value


# Some tool contracts define fields as alternatives rather than as a pair of
# unconditional outputs.  The resolver for a named radio target is one such
# case: it can legitimately return only ``zone_id`` or only ``channel_id``.
# A planner may still project both possible fields into the downstream action.
# Keep reference resolution strict everywhere else, but for these explicit
# alternative groups omit a missing projection when another member resolves.
_OPTIONAL_REFERENCE_GROUPS: dict[str, tuple[frozenset[str], ...]] = {
    "zone_channel_active_set": (frozenset({"zone_id", "channel_id"}),),
}


def _resolve_step_arguments(
    step: PlanStep, outputs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    groups = _OPTIONAL_REFERENCE_GROUPS.get(step.capability, ())
    if not groups:
        return _resolve(step.arguments, outputs)

    grouped_fields = set().union(*groups) if groups else set()
    resolved: dict[str, Any] = {}
    missing: dict[str, KeyError] = {}

    for key, value in step.arguments.items():
        if key not in grouped_fields:
            resolved[key] = _resolve(value, outputs)
            continue
        try:
            resolved[key] = _resolve(value, outputs)
        except KeyError as exc:
            missing[key] = exc

    for group in groups:
        supplied = [key for key in group if key in step.arguments]
        if not supplied:
            continue
        resolved_members = [key for key in supplied if key in resolved]
        missing_members = [key for key in supplied if key in missing]
        if missing_members and not resolved_members:
            # The alternative contract still requires at least one member.
            # Preserve the original diagnostic when none can be projected.
            raise missing[missing_members[0]]

    return resolved


class BoundedPlanExecutor:
    def __init__(self, *, max_replans: int = 2, total_timeout_seconds: float = 120.0) -> None:
        self.max_replans = max(0, int(max_replans))
        self.total_timeout_seconds = max(0.1, float(total_timeout_seconds))
        self.parallel_steps = _parallel_steps_enabled()

    def execute(self, plan: OperationPlan, execute_step: StepExecutor, *, replan: ReplanCallable | None = None) -> PlanExecutionResult:
        started = time.monotonic()
        started_perf = time.perf_counter()
        outputs: dict[str, dict[str, Any]] = {}
        completed: list[str] = []
        completed_set: set[str] = set()
        replans = 0
        step_timings: dict[str, Any] = {}
        current = plan
        index = 0

        def finish(status: str, *, failed_step: str | None = None, error: str | None = None,
                   wait: dict[str, Any] | None = None) -> PlanExecutionResult:
            return PlanExecutionResult(
                status, outputs, completed, failed_step=failed_step, error=error,
                replan_count=replans, wait=wait,
                timing_ms={"total_ms": round((time.perf_counter() - started_perf) * 1000.0, 3), "steps": step_timings},
            )

        def _run_one(step: PlanStep) -> tuple[PlanStep, dict[str, Any], float, float, float]:
            """执行单个步骤并返回 (step, result, resolve_ms, execute_ms, total_ms)。"""
            step_started = time.perf_counter()
            resolve_started = time.perf_counter()
            try:
                resolved_arguments = _resolve_step_arguments(step, outputs)
                resolve_ms = round((time.perf_counter() - resolve_started) * 1000.0, 3)
                execute_started = time.perf_counter()
                result = execute_step(step, resolved_arguments)
                execute_ms = round((time.perf_counter() - execute_started) * 1000.0, 3)
            except Exception as exc:
                resolve_ms = round((time.perf_counter() - resolve_started) * 1000.0, 3)
                execute_ms = 0.0
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "recoverable": False}
            total_ms = round((time.perf_counter() - step_started) * 1000.0, 3)
            return step, result, resolve_ms, execute_ms, total_ms

        def _record_timing(step: PlanStep, resolve_ms: float, execute_ms: float, total_ms: float,
                           *, parallel: bool = False) -> None:
            step_timings[step.step_id] = {
                "capability": step.capability, "resolve_ms": resolve_ms, "execute_ms": execute_ms,
                "total_ms": total_ms, "parallel": parallel,
            }

        while index < len(current.steps):
            if time.monotonic() - started >= min(self.total_timeout_seconds, current.limits.timeout_seconds):
                return finish("TIMED_OUT", error="plan total timeout exceeded")

            # 收集本批可执行步骤：依赖已完成且不引用未完成步骤输出。
            batch: list[PlanStep] = []
            scan = index
            while scan < len(current.steps):
                step = current.steps[scan]
                if any(dep not in completed_set for dep in step.depends_on):
                    break
                if _step_references_pending(step, completed_set):
                    break
                batch.append(step)
                scan += 1
                if not self.parallel_steps:
                    break

            if not batch:
                # 依赖未完成且无法推进（与串行实现的 FAILED 判定一致）。
                blocked = current.steps[index]
                return finish("FAILED", failed_step=blocked.step_id, error="dependency not completed")

            first = batch[0]
            if any(dep not in completed_set for dep in first.depends_on):
                return finish("FAILED", failed_step=first.step_id, error="dependency not completed")

            if len(batch) > 1:
                # 并发执行无依赖批次；resolve 在各自线程内独立进行。
                batch_started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=min(len(batch), 4)) as pool:
                    futures = [pool.submit(_run_one, step) for step in batch]
                    batch_results = [future.result() for future in futures]
                parallel_total_ms = round((time.perf_counter() - batch_started) * 1000.0, 3)
                for step, result, resolve_ms, execute_ms, _total_ms in batch_results:
                    _record_timing(step, resolve_ms, execute_ms, parallel_total_ms, parallel=True)
            else:
                batch_results = [_run_one(first)]
                step, result, resolve_ms, execute_ms, total_ms = batch_results[0]
                _record_timing(step, resolve_ms, execute_ms, total_ms)

            # 按步骤顺序处理结果，保持串行实现的失败/replan/wait 语义。
            aborted = False
            for step, result, _resolve_ms, _execute_ms, _total_ms in batch_results:
                if result.get("wait"):
                    wait_data = result.get("wait_condition")
                    return finish("WAITING", failed_step=step.step_id,
                                  wait=(dict(wait_data) if isinstance(wait_data, dict) else None))
                if bool(result.get("ok")):
                    outputs[step.step_id] = dict(result.get("output") or result)
                    completed.append(step.step_id)
                    completed_set.add(step.step_id)
                    index += 1
                    continue
                recoverable = bool(result.get("recoverable"))
                if step.failure_strategy == "continue":
                    outputs[step.step_id] = dict(result)
                    completed.append(step.step_id)
                    completed_set.add(step.step_id)
                    index += 1
                    continue
                if step.failure_strategy == "replan" and recoverable and replan is not None and replans < min(self.max_replans, current.limits.max_replans):
                    replans += 1
                    current = replan(current, step, result)
                    if current.replan_count != replans:
                        current = current.model_copy(update={"replan_count": replans})
                    index = 0
                    outputs = {}
                    completed = []
                    completed_set = set()
                    aborted = True
                    break
                return finish("FAILED", failed_step=step.step_id, error=str(result.get("error") or "step failed"))
            if aborted:
                continue
        return finish("SUCCEEDED")

