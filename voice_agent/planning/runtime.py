# =============================================================================
# 【维护注释】文件：voice_agent/planning/runtime.py
# 所属层次：计划治理层
# 主要职责：负责计划模型、校验、持久化、策略衔接、等待条件和计划执行前治理。
# 项目内直接依赖：voice_agent.approvals, voice_agent.capabilities, voice_agent.operations.models, voice_agent.operations.runtime, voice_agent.policy, voice_agent.reasoning, .executor, .models
# 主要公开符号：PlanningRuntime
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Validated capability planning, policy, approval and bounded execution."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable, Iterable

from voice_agent.approvals import ApprovalRepository, ApprovalService
from voice_agent.capabilities import CapabilityRegistry
from voice_agent.operations.models import AtomicOperation
from voice_agent.operations.runtime import OperationRuntime
from voice_agent.policy import PolicyEngine
from voice_agent.reasoning import SemanticPlanner

from .executor import BoundedPlanExecutor, PlanExecutionResult
from .models import OperationPlan, PlanLimits, PlanStep
from .planner import CapabilityPlanner
from .repository import PlanningRepository
from .validator import PlanValidator
from .waits import WaitRegistry

ToolExecutor = Callable[[str, dict[str, Any], float | None], dict[str, Any] | None]
LocalExecutor = Callable[[str, dict[str, Any], AtomicOperation], dict[str, Any] | None]


class PlanningRuntime:
    def __init__(
        self,
        *,
        operation_runtime: OperationRuntime,
        capability_registry: CapabilityRegistry,
        semantic_planner: SemanticPlanner | None,
        planning_store_path: str | Path,
        approval_store_path: str | Path,
        policy_mode: str,
        auto_execute_max_risk: str,
        r2_allowlist: Iterable[str],
        max_steps: int,
        max_replans: int,
        max_model_calls: int,
        total_timeout_seconds: float,
        approval_allowed_roles: Iterable[str] = ("approver", "admin"),
    ) -> None:
        self.registry = capability_registry
        self.limits = PlanLimits(
            max_steps=max_steps,
            max_replans=max_replans,
            max_model_calls=max_model_calls,
            timeout_seconds=total_timeout_seconds,
        )
        self.validator = PlanValidator(
            capability_registry,
            max_steps=max_steps,
            max_replans=max_replans,
            max_model_calls=max_model_calls,
            total_timeout_seconds=total_timeout_seconds,
        )
        self.planner = CapabilityPlanner(
            capability_registry,
            self.validator,
            semantic_planner=semantic_planner,
        )
        self.policy = PolicyEngine(
            capability_registry,
            mode=policy_mode,
            auto_execute_max_risk=auto_execute_max_risk,
            r2_allowlist=r2_allowlist,
        )
        self.repository = PlanningRepository(planning_store_path)
        self.approvals = ApprovalRepository(approval_store_path)
        self.approval_service = ApprovalService(
            self.approvals, self.validator, self.policy, allowed_roles=approval_allowed_roles
        )
        self.waits = WaitRegistry(operation_runtime.operations)
        self.executor = BoundedPlanExecutor(
            max_replans=max_replans, total_timeout_seconds=total_timeout_seconds
        )
        self.operation_runtime = operation_runtime

    def prepare(
        self,
        text: str,
        *,
        origin_command_id: str,
        actor_id: str | None,
        context: dict[str, Any] | None,
        context_snapshot_id: str | None,
        context_snapshot_hash: str | None,
    ) -> dict[str, Any]:
        prepare_started = time.perf_counter()
        planner_started = time.perf_counter()
        proposal, plan = self.planner.propose(
            text,
            origin_command_id=origin_command_id,
            context=context,
            context_snapshot_id=context_snapshot_id,
            context_snapshot_hash=context_snapshot_hash,
            limits=self.limits,
        )
        planner_total_ms = round((time.perf_counter() - planner_started) * 1000.0, 3)
        semantic = getattr(getattr(self.planner, "semantic_planner", None), "last_timing_ms", {}) or {}
        timing_ms: dict[str, Any] = {"planner_total_ms": planner_total_ms, "planner": dict(semantic)}
        if plan is None:
            timing_ms["prepare_total_ms"] = round((time.perf_counter() - prepare_started) * 1000.0, 3)
            return {"proposal": proposal, "plan": None, "policy": None, "approval": None, "timing_ms": timing_ms}
        policy_started = time.perf_counter()
        decision = self.policy.evaluate(plan, actor_id=actor_id)
        timing_ms["policy_ms"] = round((time.perf_counter() - policy_started) * 1000.0, 3)
        desired = decision.decision
        approval_required = desired == "APPROVAL_REQUIRED"
        plan = plan.model_copy(
            update={
                "policy_id": decision.policy_id,
                "risk_level": decision.risk_level,
                "approval_required": approval_required,
                "status": "APPROVAL_PENDING"
                if approval_required
                else ("REJECTED" if desired == "DENY" else "READY"),
            }
        )
        self.repository.save_plan(plan)
        self.repository.record_policy(
            plan.decision_id, decision.policy_id, decision.model_dump(mode="json")
        )
        approval = None
        approval_started = time.perf_counter()
        if approval_required:
            approval = self.approval_service.request(plan, thread_id=origin_command_id)
        timing_ms["approval_active_ms"] = round((time.perf_counter() - approval_started) * 1000.0, 3)
        timing_ms["prepare_total_ms"] = round((time.perf_counter() - prepare_started) * 1000.0, 3)
        return {"proposal": proposal, "plan": plan, "policy": decision, "approval": approval, "timing_ms": timing_ms}

    @staticmethod
    def _target_for_step(capability: str, arguments: dict[str, Any]) -> tuple[str | None, str | None]:
        if capability == "call_start":
            # v0.2 附件契约使用 target_type/target_id；旧计划可能仍携带
            # call_type/callee_id，两者都接受。
            call_type = str(
                arguments.get("target_type") or arguments.get("call_type") or ""
            )
            target_id = str(arguments.get("target_id") or arguments.get("callee_id") or "") or None
            return (
                "group" if call_type == "group" else "radio",
                target_id,
            )
        if arguments.get("target_id") is not None:
            return str(arguments.get("target_type") or "target"), str(arguments["target_id"])
        if arguments.get("call_id") is not None:
            return "call", str(arguments["call_id"])
        if arguments.get("reminder_id") is not None:
            return "reminder", str(arguments["reminder_id"])
        return None, None

    def _atomic_for_step(
        self,
        plan: OperationPlan,
        step: PlanStep,
        *,
        actor_id: str | None,
        arguments: dict[str, Any],
    ) -> AtomicOperation:
        spec = self.registry.get(step.capability)
        target_type, target_id = self._target_for_step(step.capability, arguments)
        return AtomicOperation(
            origin_command_id=plan.origin_command_id,
            operation_type=step.capability,
            step_id=step.step_id,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            arguments=dict(arguments),
            resolved_arguments=dict(arguments),
            executor_kind="local" if spec.executor_kind == "local" else "mcp",
            tool_name=spec.tool_name,
            risk_level=spec.risk_level,
            depends_on=list(step.depends_on),
            timeout_seconds=float(step.timeout_seconds or 60.0),
            side_effect=bool(spec.side_effect),
        )

    @staticmethod
    def _inject_server_arguments(
        arguments: dict[str, Any], *, spec: Any, actor_id: str | None, operation_id: str
    ) -> dict[str, Any]:
        values = dict(arguments)
        for key in spec.server_owned_arguments:
            if key == "actor_terminal_id":
                if not actor_id:
                    raise ValueError("actor terminal id is required by capability")
                values[key] = str(actor_id)
            elif key == "operation_id":
                values[key] = operation_id
        return values

    def execute_plan(
        self,
        plan: OperationPlan | dict[str, Any],
        *,
        actor_id: str | None,
        execute_tool: ToolExecutor,
        execute_local: LocalExecutor,
        resumed_wait: dict[str, Any] | None = None,
    ) -> PlanExecutionResult:
        plan_obj = self.validator.validate(OperationPlan.model_validate(plan))
        if plan_obj.status in {"REJECTED", "CANCELLED", "TIMED_OUT"}:
            raise PermissionError(f"plan is not executable in status {plan_obj.status}")
        self.operation_runtime.update_command(plan_obj.origin_command_id, status="RUNNING")
        running = plan_obj.model_copy(update={"status": "RUNNING"})
        self.repository.save_plan(running)

        def run_step(step: PlanStep, resolved_arguments: dict[str, Any]) -> dict[str, Any]:
            spec = self.registry.get(step.capability)
            if not spec.available:
                return {"ok": False, "error": spec.unavailable_reason or "capability unavailable", "recoverable": False}
            operation = self._atomic_for_step(
                running, step, actor_id=actor_id, arguments=resolved_arguments
            )
            _, stored = self.operation_runtime.operations.create_or_get(operation)
            execution_arguments = self._inject_server_arguments(
                resolved_arguments,
                spec=spec,
                actor_id=actor_id,
                operation_id=stored.operation_id,
            )

            def invoke(_operation: AtomicOperation) -> dict[str, Any] | None:
                if spec.executor_kind == "mcp":
                    return execute_tool(str(spec.tool_name or spec.id), execution_arguments, step.timeout_seconds)
                return execute_local(spec.id, execution_arguments, stored)

            result = self.operation_runtime.executor.execute(stored, invoke)
            output: dict[str, Any] = {
                "operation_id": stored.operation_id,
                "operation_status": result.status,
            }
            if isinstance(result.response, dict):
                response_output = result.response.get("result")
                if isinstance(response_output, dict):
                    output.update(response_output)
                else:
                    # HTTP MCP execution returns an MCPResult wrapper rather than
                    # placing the tool payload at response["result"].  Preserve the
                    # wrapper for audit/delivery compatibility, then additionally
                    # expose MCP structuredContent at the step-output root so plan
                    # references such as ${step_1.call_id} can resolve.
                    output.update(result.response)
                    mcp_response = result.response.get("mcp_response")
                    if isinstance(mcp_response, dict):
                        rpc_result = mcp_response.get("result")
                        if isinstance(rpc_result, dict):
                            structured = rpc_result.get("structuredContent")
                            if isinstance(structured, dict):
                                output.update(structured)
            if isinstance(result.final_result, dict):
                output.update(result.final_result)
            return {
                "ok": bool(result.ok),
                "output": output,
                "error": result.error,
                "recoverable": bool(
                    (not stored.side_effect) and result.status in {"FAILED", "TIMED_OUT"}
                ),
            }

        result = self.executor.execute(running, run_step)
        if result.status == "SUCCEEDED" and any(
            str(output.get("operation_status") or "") in {"ACCEPTED", "DISPATCHED"}
            for output in result.outputs.values()
        ):
            result = PlanExecutionResult(
                "DISPATCHED",
                outputs=result.outputs,
                completed_steps=result.completed_steps,
                replan_count=result.replan_count,
                timing_ms=result.timing_ms,
            )
        final_status = result.status if result.status in {
            "WAITING", "DISPATCHED", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"
        } else "FAILED"
        self.repository.save_plan(
            running.model_copy(update={"status": final_status, "replan_count": result.replan_count})
        )
        if result.status == "SUCCEEDED":
            self.operation_runtime.update_command(plan_obj.origin_command_id, status="SUCCEEDED", completed=True)
        elif result.status == "DISPATCHED":
            self.operation_runtime.update_command(plan_obj.origin_command_id, status="DISPATCHED")
        elif result.status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            self.operation_runtime.update_command(
                plan_obj.origin_command_id,
                status="FAILED" if result.status == "TIMED_OUT" else result.status,
                completed=True,
            )
        return result

    def dispatch_wait_event(
        self,
        *,
        subject_type: str,
        subject_id: str,
        predicate: str,
        event: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        matched = self.waits.dispatch(
            subject_type=subject_type,
            subject_id=subject_id,
            predicate=predicate,
            event=event or {},
        )
        out: list[dict[str, Any]] = []
        for item in matched:
            operation = self.operation_runtime.operations.get(str(item.get("operation_id") or ""))
            if operation is not None:
                data = dict(item)
                data["thread_id"] = operation.origin_command_id
                out.append(data)
        return out

    def approval_resume_payload(self, approval_id: str) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise KeyError("approval not found")
        if approval.status not in {"APPROVED", "EDITED", "REJECTED"}:
            raise ValueError("approval is not decided")
        return {
            "kind": "human_approval",
            "approval_id": approval.approval_id,
            "status": approval.status,
            "decision_id": approval.decision_id,
            "plan": approval.approved_payload,
            "reviewer_id": approval.reviewer_id,
            "reviewer_role": approval.reviewer_role,
        }
