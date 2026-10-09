# =============================================================================
# 【维护注释】文件：voice_agent/agent/workflow.py
# 所属层次：LangGraph 编排层
# 主要职责：负责状态图、节点、依赖绑定、检查点和工作流路由；业务能力通过注册表和 runtime 注入，不直接硬编码设备协议。
# 项目内直接依赖：.checkpointing, .dependencies, .framework, .nodes, .runtime_dependencies, .state, ..
# 主要公开符号：VoiceAgentProcessor
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""LangGraph assembly for the capability-driven voice agent."""
from __future__ import annotations

import time
import traceback
from typing import Callable

from .checkpointing import CheckpointerRuntime
from .dependencies import VoiceAgentDependencies
from .framework import Command, END, START, RunnableLambda, StateGraph
from .nodes import (
    approval_interrupt,
    build_context,
    convert_audio,
    deliver_result,
    execute_operation_plan,
    finalize_result,
    plan_request,
    route_after_transcribe,
    route_plan_gate,
    transcribe_audio,
)
from .runtime_dependencies import reset_dependencies, set_dependencies
from .state import VoiceAgentState
from .. import config


class VoiceAgentProcessor:
    def __init__(self, dependencies: Callable[[], VoiceAgentDependencies]) -> None:
        self._dependencies = dependencies
        self._checkpoint_runtime = CheckpointerRuntime(
            backend=config.WORKING_MEMORY_BACKEND,
            sqlite_path=config.WORKING_MEMORY_SQLITE_PATH,
            postgres_dsn=config.WORKING_MEMORY_POSTGRES_DSN,
        )
        self.graph = self._build_graph(self._checkpoint_runtime.checkpointer)

    @staticmethod
    def _build_graph(checkpointer=None):
        graph = StateGraph(VoiceAgentState)
        for name, func in (
            ("convert_audio", convert_audio),
            ("transcribe_audio", transcribe_audio),
            ("build_context", build_context),
            ("plan_request", plan_request),
            ("approval_interrupt", approval_interrupt),
            ("execute_operation_plan", execute_operation_plan),
            ("deliver_result", deliver_result),
            ("finalize_result", finalize_result),
        ):
            graph.add_node(name, RunnableLambda(func))
        graph.add_edge(START, "convert_audio")
        graph.add_edge("convert_audio", "transcribe_audio")
        # 需求 §10：仅 category=CONTROL 且 endpoint_complete=true 才进 Planner；
        # user 消息（含身份异常）只归档后直接 finalize。
        graph.add_conditional_edges(
            "transcribe_audio",
            route_after_transcribe,
            {"plan": "build_context", "finalize": "finalize_result"},
        )
        graph.add_edge("build_context", "plan_request")
        graph.add_conditional_edges(
            "plan_request",
            route_plan_gate,
            {"approval": "approval_interrupt", "execute": "execute_operation_plan", "deliver": "deliver_result"},
        )
        graph.add_conditional_edges(
            "approval_interrupt",
            route_plan_gate,
            {"approval": "approval_interrupt", "execute": "execute_operation_plan", "deliver": "deliver_result"},
        )
        graph.add_edge("execute_operation_plan", "deliver_result")
        graph.add_edge("deliver_result", "finalize_result")
        graph.add_edge("finalize_result", END)
        return graph.compile(checkpointer=checkpointer)

    def resume_approval(self, thread_id: str, payload: dict) -> dict | None:
        deps = self._dependencies()
        token = set_dependencies(deps)
        try:
            final_state = self.graph.invoke(
                Command(resume=payload),
                config={"configurable": {"thread_id": str(thread_id)}},
            )
            if isinstance(final_state, dict) and final_state.get("__interrupt__"):
                return {"approval_pending": True, "approval": final_state.get("approval")}
            return None if not isinstance(final_state, dict) else final_state.get("result")
        finally:
            reset_dependencies(token)

    def resume_wait(self, thread_id: str, payload: dict) -> dict | None:
        raise RuntimeError("wait resume is not used by the current capability graph")

    def process(self, task: dict, delivery_context=None) -> dict | None:
        deps = self._dependencies()
        task_id = task.get("task_id")
        request_id = task.get("request_id") or task_id
        data = task.get("data") or {}
        started_perf = time.perf_counter()
        worker_started_time_ms = int(task.get("worker_started_time_ms") or deps.now_ms())
        worker_dequeued_time_ms = int(task.get("worker_dequeued_time_ms") or worker_started_time_ms)
        queued_time_ms = task.get("queued_time_ms")
        try:
            agent_queue_wait_ms = max(0.0, float(worker_dequeued_time_ms) - float(queued_time_ms)) if queued_time_ms else 0.0
        except (TypeError, ValueError):
            agent_queue_wait_ms = 0.0
        command_id = None
        runtime = deps.operation_runtime
        if runtime is not None and getattr(runtime, "enabled", False):
            # Command persistence is an audit/idempotency layer.  A damaged or
            # temporarily unavailable SQLite state store must not prevent the
            # audio from reaching ASR.  The HTTP callback path is already
            # fail-open for the same shadow write; keep the worker path
            # consistent and let later planning persistence report its own
            # failure if the request actually needs a side effect.
            try:
                command = runtime.create_voice_command(task, delivery_context)
            except Exception as exc:
                command = None
                deps.logger.warning(
                    "command_persistence_failed_continue_asr | request_id=%s error_type=%s error=%s",
                    request_id,
                    type(exc).__name__,
                    str(exc)[:300],
                )
            command_id = None if command is None else command.command_id
            if command is not None and command.status == "CANCELLED":
                return {
                    "task_id": task_id,
                    "request_id": request_id,
                    "command_id": command.command_id,
                    "cancelled": True,
                    "cancel_reason": command.cancel_reason,
                }
        state: VoiceAgentState = {
            "task": task,
            "delivery_context": delivery_context,
            "command_id": command_id,
            "started_perf": started_perf,
            "task_id": task_id,
            "request_id": request_id,
            "data": data,
            "received_time_ms": task.get("received_time_ms"),
            "queued_time_ms": queued_time_ms,
            "worker_dequeued_time_ms": worker_dequeued_time_ms,
            "worker_started_time_ms": worker_started_time_ms,
            "timing_ms": {
                "agent_ingress_ms": dict(task.get("agent_ingress_timing_ms") or {}),
                "agent_queue_wait_ms": round(agent_queue_wait_ms, 3),
            },
            "routing_error": task.get("routing_error"),
        }
        token = set_dependencies(deps)
        try:
            final_state = self.graph.invoke(
                state,
                config={"configurable": {"thread_id": str(command_id or request_id or task_id or "anonymous")}},
            )
            if isinstance(final_state, dict) and final_state.get("__interrupt__"):
                return {
                    "task_id": task_id,
                    "request_id": request_id,
                    "command_id": command_id,
                    "approval_pending": True,
                    "approval": final_state.get("approval"),
                    "decision_id": (final_state.get("operation_plan") or {}).get("decision_id"),
                }
            return None if not isinstance(final_state, dict) else final_state.get("result")
        except Exception as exc:
            deps.logger.exception(
                "语音消息处理失败 | request_id=%s terminal_id=%s terminal_session_id=%s channel_id=%s",
                request_id,
                delivery_context.terminal_id if delivery_context else None,
                delivery_context.terminal_session_id if delivery_context else None,
                delivery_context.channel_id if delivery_context else None,
            )
            err = {
                "task_id": task_id,
                "request_id": request_id,
                "received_time_ms": task.get("received_time_ms"),
                "done_time_ms": deps.now_ms(),
                "error": f"worker exception: {exc}",
                "traceback": traceback.format_exc(),
                "timing_ms": {"worker_total_ms": deps.perf_ms(started_perf)},
            }
            deps.write_jsonl_async(deps.result_jsonl, err)
            deps.append_latest_result(err)
            deps.inc_stat("processed_total", 1)
            deps.inc_stat("failed_total", 1)
            deps.update_stats(last_error=err["error"], last_result=err)
            return None
        finally:
            reset_dependencies(token)
