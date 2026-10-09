# =============================================================================
# 【维护注释】文件：voice_agent/agent/nodes.py
# 所属层次：LangGraph 编排层
# 主要职责：负责状态图、节点、依赖绑定、检查点和工作流路由；业务能力通过注册表和 runtime 注入，不直接硬编码设备协议。
# 项目内直接依赖：shared_contracts.event_models, shared_contracts.event_types, shared_contracts.identifiers, shared_runtime.diagnostics, .., .dependencies, .framework, .logging_fields
# 主要公开符号：convert_audio, transcribe_audio, build_context, plan_request, route_plan_gate, approval_interrupt, execute_operation_plan, deliver_result, finalize_result
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""基于能力注册表驱动的 LangGraph 节点。"""
from __future__ import annotations

import json
import time
from typing import Any

from shared_contracts.event_models import DomainEvent
from shared_contracts.event_types import ASR_COMPLETED, ASR_FAILED
from shared_contracts.identifiers import new_event_id
from shared_runtime.diagnostics import log_trace
from shared_runtime.latency import iso_to_epoch_ms, log_latency_tree

from .. import config
from ..delivery_policy import DEFAULT_USER_DELIVERY_POLICY
from ..zone_channel_directory import get_zone_channel_map
from .dependencies import VoiceAgentDependencies
from .framework import interrupt
from .logging_fields import delivery_log_fields
from .runtime_dependencies import get_dependencies
from .state import VoiceAgentState


def _deps(state: VoiceAgentState) -> VoiceAgentDependencies:
    value = state.get("dependencies") or get_dependencies()
    if value is None:
        raise RuntimeError("voice-agent dependencies are not bound to this invocation")
    return value


def _with_timing(state: VoiceAgentState, **entries: Any) -> dict[str, Any]:
    timing = dict(state.get("timing_ms") or {})
    timing.update(entries)
    return timing


def _timing_scalar_total(value: Any) -> float:
    """从 timing 字段取标量总耗时；dict 时按优先级查找 total 键。

    生产链路各节点写入的 timing 形态不一：audio_convert_ms 带 convert_total_ms、
    asr_total_ms 带 qwen_asr_total_ms、planner_total_ms 带 model_call_total_ms、
    plan_execute_ms 带 total_ms，其余为标量。找不到则返回 0。
    """
    if isinstance(value, dict):
        for key in (
            "total_ms",
            "convert_total_ms",
            "qwen_asr_total_ms",
            "model_call_total_ms",
            "planner_total_ms",
        ):
            candidate = value.get(key)
            if candidate is not None:
                try:
                    return float(candidate)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _no_send_timing(state: VoiceAgentState) -> dict[str, Any]:
    """无文本下发（如纯控制指令）时的 timing。

    终点回退到最后一步计划执行完成时刻（plan_execute_done_epoch_ms，
    含 MCP 编码下发至网关/ADK 接受），使「首包→最终编码发送」对控制类
    指令同样可统计；无执行记录时（如澄清/拒绝门控）不记终点。
    """
    plan_done = (state.get("timing_ms") or {}).get("plan_execute_done_epoch_ms")
    entries: dict[str, Any] = {"delivery_ms": {"total_ms": 0.0}}
    if plan_done is not None:
        entries["delivery_done_epoch_ms"] = int(plan_done)
        entries["delivery_done_source"] = "plan_execute"
    return _with_timing(state, **entries)


def _actor_terminal_id(state: VoiceAgentState) -> str | None:
    delivery = state.get("delivery_context")
    value = delivery.terminal_id if delivery is not None else (state.get("data") or {}).get("source_id")
    return str(value).strip() if value is not None and str(value).strip() else None


def _publish_event(
    state: VoiceAgentState,
    event_type: str,
    *,
    causation_id: str | None,
    data: dict[str, Any],
) -> str | None:
    deps = _deps(state)
    if deps.publish_event is None:
        return None
    event = DomainEvent.from_voice_event(
        state.get("data") or {},
        event_type=event_type,
        source_service="voice-agent",
        event_id=new_event_id(),
        causation_id=causation_id,
        data=data,
        provenance="observed",
    )
    deps.publish_event(event)
    # ``/text/input`` 与 ``/sms/callback`` 是命令注入/短信入口。它们仍发出
    # ASR 审计事件，但不得投影进 CallStore（否则每条文本命令都会变成
    # ``active`` 通话，遮蔽真正的通话历史）。
    is_direct_text = str((state.get("data") or {}).get("input_mode") or "").strip().lower() in {"text", "sms"}
    if deps.project_event is not None and not is_direct_text:
        deps.project_event(event)
    if (
        event_type in {ASR_COMPLETED, ASR_FAILED}
        and deps.collect_transcript is not None
        and not is_direct_text
    ):
        deps.collect_transcript(event)
    return event.event_id


def _direct_text(state: VoiceAgentState) -> str:
    data = state.get("data") or {}
    if str(data.get("input_mode") or "").strip().lower() not in {"text", "sms"}:
        return ""
    return str(data.get("text") or data.get("asr_text") or "").strip()


def convert_audio(state: VoiceAgentState) -> dict[str, Any]:
    text = _direct_text(state)
    if text:
        input_mode = str((state.get("data") or {}).get("input_mode") or "text").strip().lower()
        convert_result = {
            "ok": True, "skipped": True, "input_mode": input_mode or "text", "text_length": len(text),
            "timing": {"convert_total_ms": 0.0, "base64_decode_ms": 0.0, "audio_decode_ms": 0.0,
                       "trim_ms": 0.0, "normalize_ms": 0.0, "resample_ms": 0.0, "wav_build_ms": 0.0,
                       "data_url_encode_ms": 0.0, "debug_bundle_write_ms": 0.0},
        }
        return {"data_url": None, "convert_result": convert_result,
                "timing_ms": _with_timing(state, audio_convert_ms=convert_result["timing"])}
    data_url, result = _deps(state).callback_audio_to_data_url(
        state["data"], debug_id=str(state.get("task_id") or "unknown")
    )
    return {"data_url": data_url, "convert_result": result,
            "timing_ms": _with_timing(state, audio_convert_ms=dict(result.get("timing") or {}))}


def transcribe_audio(state: VoiceAgentState) -> dict[str, Any]:
    deps = _deps(state)
    direct_text = _direct_text(state)
    convert_result = state.get("convert_result") or {}
    if direct_text:
        input_mode = str((state.get("data") or {}).get("input_mode") or "text").strip().lower()
        asr_result = {
            "ok": True,
            "error": None,
            "raw_text": direct_text,
            "text": direct_text,
            "annotations": [],
            "status_code": 200,
            "model": "direct-sms" if input_mode == "sms" else "direct-text",
            "raw_response": None,
            "timing": {},
            "input_mode": input_mode or "text",
        }
    elif not state.get("data_url"):
        asr_result = {
            "ok": False,
            "error": convert_result.get("error"),
            "raw_text": "",
            "text": "",
            "annotations": [],
            "status_code": None,
            "raw_response": None,
            "timing": {},
        }
    else:
        asr_result = deps.call_asr(str(state["data_url"]))
    text = str(asr_result.get("text") or asr_result.get("raw_text") or "")
    runtime = deps.operation_runtime
    if runtime is not None:
        runtime.update_command(state.get("command_id"), raw_text=text)
    event_type = ASR_COMPLETED if asr_result.get("ok") else ASR_FAILED
    publish_started = time.perf_counter()
    event_id = _publish_event(
        state,
        event_type,
        causation_id=str((state.get("data") or {}).get("accepted_event_id") or (state.get("data") or {}).get("event_id") or "") or None,
        data={
            "ok": bool(asr_result.get("ok")),
            "status_code": asr_result.get("status_code"),
            "model": asr_result.get("model"),
            "transcript": text,
            "normalized_text": " ".join(text.split()),
            "text_length": len(text),
            "request_id": state.get("request_id"),
            "speaker_radio_id": (state.get("data") or {}).get("source_id"),
            "source_id": (state.get("data") or {}).get("source_id"),
            "target_id": (state.get("data") or {}).get("target_id"),
            "group_id": (state.get("data") or {}).get("group_id"),
            "channel_id": (state.get("data") or {}).get("channel_id"),
            "call_type": (state.get("data") or {}).get("call_type"),
            # 传递消息分类，供 TranscriptCollector 写入 call_turn.message_category；
            # 没有它，控制指令语音（control_*，如“上一通电话讲了什么”）会与
            # 真实通话混在同一可摘要口径里，查不到分类就无从排除。
            "message_category": (state.get("data") or {}).get("message_category"),
            "capture_session_id": (state.get("data") or {}).get("capture_session_id") or (state.get("data") or {}).get("session_id"),
        },
    )
    event_publish_ms = round((time.perf_counter() - publish_started) * 1000.0, 3)
    asr_timing = dict(asr_result.get("timing") or {})
    updates: dict[str, Any] = {
        "asr_result": asr_result, "asr_text": text, "asr_event_id": event_id,
        "timing_ms": _with_timing(state, asr_total_ms=asr_timing, event_publish_ms=event_publish_ms),
    }
    # 需求 §8/§10：控制消息身份不完整时不得进入 Planner，必须产生诊断标记。
    data = state.get("data") or {}
    message_category = str(data.get("message_category") or "").strip().lower()
    if message_category.startswith("control_") and not bool(data.get("endpoint_complete")):
        updates["planner_gate"] = "reject"
        updates["planner_block_error"] = "ENDPOINT_INCOMPLETE"
    return updates


def route_after_transcribe(state: VoiceAgentState) -> str:
    """需求 §10：仅 category=CONTROL 且 endpoint_complete=true 才进 Planner。

    - 分类未启用（无 message_category）：保持旧行为，进 Planner；
    - user 消息：只归档不规划，直接 finalize；
    - 控制消息身份不完整：拒绝规划，直接 finalize。
    """
    data = state.get("data") or {}
    category = str(data.get("message_category") or "").strip().lower()
    if not category:
        return "plan"
    if category.startswith("user_"):
        return "finalize"
    if not bool(data.get("endpoint_complete")):
        return "finalize"
    return "plan"


def build_context(state: VoiceAgentState) -> dict[str, Any]:
    deps = _deps(state)
    registry = deps.capability_registry
    base = {
        "capability_availability": registry.availability(),
        "capability_catalog_hash": registry.catalog_hash,
    }
    if config.CONTEXT_BUILDER_MODE == "off" or deps.context_builder is None:
        return {
            **base,
            "decision_context": None,
            "context_snapshot_id": None,
            "context_snapshot_hash": None,
            "context_shadow_only": True,
            "timing_ms": _with_timing(state, context_build_ms=0.0),
        }
    terminal_id = _actor_terminal_id(state)
    data = state.get("data") or {}
    try:
        context_started = time.perf_counter()
        context, snapshot = deps.context_builder.build(
            {
                "command_id": state.get("command_id"),
                "request_id": str(state.get("request_id") or "") or None,
                "actor_id": terminal_id,
                "terminal_id": terminal_id,
                "tenant_id": data.get("tenant_id") or config.CONTEXT_DEFAULT_TENANT_ID,
                "request_text": state.get("asr_text", ""),
                "terminal_state": {
                    key: data.get(key)
                    for key in ("channel_id", "group_id", "target_id", "call_id", "session_id")
                    if data.get(key) is not None
                },
                # Snapshot the same authoritative directory used by the local
                # resolver so the planner can classify names before creating
                # cross-step zone_id/channel_id references.
                "authoritative_directory": get_zone_channel_map(),
                "available_capabilities": registry.availability(),
                "policy_constraints": [
                    {
                        "policy_id": "authority-first",
                        "rule": "verified device state and directories outrank model inference",
                    },
                    {
                        "policy_id": "capability-boundary",
                        "rule": "only registered and currently available capabilities may execute",
                    },
                    {
                        "policy_id": "history-no-trigger",
                        "rule": "history and memory may inform the current plan but never create commands themselves",
                    },
                ],
                "max_items": config.CONTEXT_MAX_ITEMS,
                "max_tokens": config.CONTEXT_MAX_TOKENS,
                "recent_call_days": config.CONTEXT_RECENT_CALL_DAYS,
                "semantic_query": state.get("asr_text") if config.VECTOR_SEARCH_ENABLE else None,
                "mode": "active" if config.CONTEXT_BUILDER_MODE == "active" else "shadow",
            }
        )
        context_build_ms = round((time.perf_counter() - context_started) * 1000.0, 3)
        deps.log_event(
            deps.logger,
            "context_snapshot_created",
            request_id=state.get("request_id"),
            command_id=state.get("command_id"),
            snapshot_id=snapshot.snapshot_id,
            content_hash=snapshot.content_hash,
            item_count=context.used_items,
            token_estimate=context.used_tokens,
            truncated=context.truncated,
            context_build_ms=context_build_ms,
        )
        return {
            **base,
            # shadow 只观察：快照照常持久化（snapshot_id/hash 可审计），
            # 但 planner_payload 不得进入 Planner 输入；active 是唯一允许
            # 把决策上下文交给模型的模式，否则未验证的 shadow 数据会
            # 静默改变规划行为。
            "decision_context": (
                context.planner_payload()
                if config.CONTEXT_BUILDER_MODE == "active"
                else None
            ),
            "context_snapshot_id": snapshot.snapshot_id,
            "context_snapshot_hash": snapshot.content_hash,
            "context_shadow_only": config.CONTEXT_BUILDER_MODE != "active",
            "timing_ms": _with_timing(state, context_build_ms=context_build_ms),
        }
    except Exception as exc:
        deps.log_event(
            deps.logger,
            "context_build_failed",
            request_id=state.get("request_id"),
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        return {
            **base,
            "decision_context": None,
            "context_snapshot_id": None,
            "context_snapshot_hash": None,
            "context_shadow_only": True,
            "timing_ms": _with_timing(state, context_build_ms=0.0),
        }


def plan_request(state: VoiceAgentState) -> dict[str, Any]:
    deps = _deps(state)
    text = str(state.get("asr_text") or "").strip()
    asr = state.get("asr_result") or {}
    if not asr.get("ok") or not text:
        return {
            "plan_proposal": {"decision": "NO_CAPABILITY", "goal": "", "steps": [], "clarification": None},
            "operation_plan": None,
            "policy_decision": None,
            "approval": None,
            "planner_gate": "reject",
            "planner_block_error": "ASR_FAILED_OR_EMPTY",
        }
    command_id = str(state.get("command_id") or state.get("request_id") or "").strip()
    if not command_id:
        return {"planner_gate": "reject", "planner_block_error": "COMMAND_ID_REQUIRED"}
    try:
        planning_started = time.perf_counter()
        prepared = deps.planning_runtime.prepare(
            text,
            origin_command_id=command_id,
            actor_id=_actor_terminal_id(state),
            context=state.get("decision_context"),
            context_snapshot_id=state.get("context_snapshot_id"),
            context_snapshot_hash=state.get("context_snapshot_hash"),
        )
        planning_prepare_ms = round((time.perf_counter() - planning_started) * 1000.0, 3)
        proposal = prepared["proposal"]
        plan = prepared["plan"]
        policy = prepared["policy"]
        approval = prepared["approval"]
        if proposal.decision == "NEEDS_CLARIFICATION":
            gate = "clarification"
        elif proposal.decision == "NO_CAPABILITY":
            gate = "no_capability"
        elif plan is None or policy is None:
            gate = "reject"
        else:
            # 审计模式只记录理想策略结论，真正的执行门控必须使用策略引擎返回的实际决策。
            desired = policy.decision
            gate = "approval" if desired == "APPROVAL_REQUIRED" else ("reject" if desired == "DENY" else "authorize")

        log_trace(
            deps.logger,
            "意图识别与能力规划完成",
            request_id=state.get("request_id"),
            terminal_id=_actor_terminal_id(state),
            user_text=text,
            decision=proposal.decision,
            goal=proposal.goal,
            intent_steps=[step.model_dump(mode="json") for step in proposal.steps],
            operation_plan=None if plan is None else plan.model_dump(mode="json"),
            policy=None if policy is None else policy.model_dump(mode="json"),
            approval=None if approval is None else approval.model_dump(mode="json"),
            planner_gate=gate,
            planning_prepare_ms=planning_prepare_ms,
        )
        planning_timing = dict(prepared.get("timing_ms") or {})
        planning_timing.setdefault("prepare_total_ms", planning_prepare_ms)
        updates_timing = _with_timing(
            state,
            planner_total_ms=planning_timing.get("planner", planning_timing),
            policy_ms=planning_timing.get("policy_ms", 0.0),
            approval_active_ms=planning_timing.get("approval_active_ms", 0.0),
            planning_prepare_ms=planning_timing.get("prepare_total_ms", planning_prepare_ms),
        )
        approval_wait_started_time_ms = deps.now_ms() if gate == "approval" else None
        return {
            "plan_proposal": proposal.model_dump(mode="json"),
            "operation_plan": None if plan is None else plan.model_dump(mode="json"),
            "policy_decision": None if policy is None else policy.model_dump(mode="json"),
            "approval": None if approval is None else approval.model_dump(mode="json"),
            "planner_gate": gate,
            "planner_block_error": None if gate not in {"reject"} else (None if policy is None else policy.reason),
            "approval_wait_started_time_ms": approval_wait_started_time_ms,
            "timing_ms": updates_timing,
        }
    except Exception as exc:
        deps.log_event(
            deps.logger,
            "capability_plan_failed",
            request_id=state.get("request_id"),
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        return {
            "plan_proposal": None,
            "operation_plan": None,
            "policy_decision": None,
            "approval": None,
            "planner_gate": "reject",
            "planner_block_error": f"PLAN_FAILED:{type(exc).__name__}:{exc}",
        }


def route_plan_gate(state: VoiceAgentState) -> str:
    gate = str(state.get("planner_gate") or "reject")
    if gate == "approval":
        return "approval"
    if gate == "authorize":
        return "execute"
    return "deliver"


def approval_interrupt(state: VoiceAgentState) -> dict[str, Any]:
    active_started = time.perf_counter()
    approval = state.get("approval") or {}
    if not approval.get("approval_id"):
        return {"planner_gate": "reject", "planner_block_error": "missing approval record"}
    resumed = interrupt(
        {
            "kind": "human_approval",
            "approval_id": approval.get("approval_id"),
            "decision_id": approval.get("decision_id"),
            "origin_command_id": approval.get("origin_command_id"),
            "risk_level": approval.get("risk_level"),
            "plan_hash": approval.get("plan_hash"),
            "plan": state.get("operation_plan") or {},
        }
    )
    approval_active_ms = round((time.perf_counter() - active_started) * 1000.0, 3)
    wait_started = state.get("approval_wait_started_time_ms")
    approval_wait_ms = max(0.0, float(_deps(state).now_ms()) - float(wait_started)) if wait_started else 0.0
    timing = _with_timing(state, approval_active_ms=approval_active_ms, approval_wait_ms=round(approval_wait_ms, 3))
    if not isinstance(resumed, dict) or resumed.get("approval_id") != approval.get("approval_id"):
        return {"planner_gate": "reject", "planner_block_error": "approval resume payload mismatch", "timing_ms": timing}
    status = str(resumed.get("status") or "").upper()
    if status not in {"APPROVED", "EDITED"}:
        return {"planner_gate": "reject", "planner_block_error": "human approval rejected the plan", "approval_resume": resumed, "timing_ms": timing}
    plan = resumed.get("plan")
    return {
        "planner_gate": "authorize",
        "operation_plan": plan if status == "EDITED" and isinstance(plan, dict) else state.get("operation_plan"),
        "approval_resume": resumed,
        "timing_ms": timing,
    }


def execute_operation_plan(state: VoiceAgentState) -> dict[str, Any]:
    deps = _deps(state)
    plan = state.get("operation_plan")
    if not plan:
        return {"planner_block_error": "operation plan unavailable"}
    delivery = state.get("delivery_context")
    actor = _actor_terminal_id(state)
    request_id = state.get("request_id")

    # 这是语音进入系统时观察到的通道，只用于上下文/审计。
    capture_channel_id = (
        None if delivery is None else delivery.channel_id
    )


    def execute_tool(
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float | None,
    ) -> dict[str, Any]:

        return deps.execute_tool_direct(
            tool_name,
            dict(arguments),
            request_id=request_id,
            timeout=timeout or config.OPERATION_DEFAULT_TIMEOUT_SECONDS,
            actor_terminal_id=actor,

            # 重要：
            # 不把语音采集通道强制作为 Protobuf 控制消息下行通道。
            # target radio 的实际 TMP route 由 Gateway 的
            # radio_routes[target_id] / ADK 自动选择。
            channel_id=None,
        )

    def execute_local(capability_id: str, arguments: dict[str, Any], operation: Any) -> dict[str, Any]:
        return deps.local_capability_executor.execute(
            capability_id,
            dict(arguments),
            operation,
            terminal_id=None if actor is None else int(actor),
            channel_id=capture_channel_id,
            request_id=request_id,
        )

    execution_started = time.perf_counter()
    execution = deps.planning_runtime.execute_plan(
        plan,
        actor_id=actor,
        execute_tool=execute_tool,
        execute_local=execute_local,
    )
    deps.log_event(
        deps.logger,
        "plan_execution_timing",
        request_id=request_id,
        command_id=state.get("command_id"),
        plan_execute_ms=round((time.perf_counter() - execution_started) * 1000.0, 3),
        status=execution.status,
    )
    outputs = execution.outputs
    last_output = outputs.get(execution.completed_steps[-1], {}) if execution.completed_steps else {}
    mcp_result = last_output.get("mcp_result") if isinstance(last_output, dict) else None
    operation_id = last_output.get("operation_id") if isinstance(last_output, dict) else None
    execution_dict = execution.as_dict()
    exec_timing = dict(execution_dict.get("timing_ms") or {})
    return {
        "plan_execution": execution_dict,
        "mcp_result": mcp_result if isinstance(mcp_result, dict) else None,
        "operation_id": operation_id,
        "operation_result": last_output if isinstance(last_output, dict) else None,
        "planner_block_error": execution.error if execution.status in {"FAILED", "CANCELLED", "TIMED_OUT"} else None,
        "timing_ms": _with_timing(
            state,
            plan_execute_ms=exec_timing,
            # 控制类指令的最终编码发送发生在计划步骤内（MCP 下发）；
            # 记录执行完成墙钟时刻，供无文本下发时回退作为链路终点。
            plan_execute_done_epoch_ms=int(time.time() * 1000.0),
        ),
    }


def _find_text(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("content", "summary", "translation"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key in ("structuredContent", "result", "mcp_response", "response"):
            candidate = _find_text(value.get(key))
            if candidate:
                return candidate
    if isinstance(value, list):
        for item in value:
            candidate = _find_text(item)
            if candidate:
                return candidate
    return None


def _last_capability(state: VoiceAgentState) -> str | None:
    plan = state.get("operation_plan") or {}
    steps = plan.get("steps") or []
    if not steps:
        return None
    return str((steps[-1] or {}).get("capability") or (steps[-1] or {}).get("operation_type") or "") or None


# 通话内容类能力：结果走固定“自动概要 + 源终端 TTS 播报”流程。
_CALL_CONTENT_CAPABILITIES = {"call_get_transcript", "call_summarize", "call_translate"}


def _call_content_announcement(state: VoiceAgentState) -> str | None:
    """固定流程（源终端播报）：通话内容结果自动做出概要。

    通话内容类能力（call_get_transcript/call_summarize/call_translate）
    执行成功后取最后一个成功步骤：
    - call_summarize/call_translate 已产出概要/译文 content，直接使用；
    - call_get_transcript 只有 turns，由摘要生成器自动压缩成概要
      （纯转写不适合短信下发也不适合 TTS 播报）；
    无通话内容步骤或均未成功时返回 None，维持原有投递逻辑。
    """
    plan = state.get("operation_plan") or {}
    outputs = (state.get("plan_execution") or {}).get("outputs") or {}
    chosen: tuple[str, dict[str, Any]] | None = None
    for step in plan.get("steps") or []:
        capability = str((step or {}).get("capability") or "")
        if capability not in _CALL_CONTENT_CAPABILITIES:
            continue
        # 生产计划序列化为 step_id（PlanStep 模型字段），测试夹具可能用 id。
        step_key = str(
            (step or {}).get("step_id") or (step or {}).get("id") or ""
        )
        output = outputs.get(step_key)
        if isinstance(output, dict) and output.get("ok"):
            chosen = (capability, output)
    if chosen is None:
        return None
    capability, output = chosen
    content = str(output.get("content") or "").strip()
    if content:
        return content
    turns = output.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    # 固定流程：自动做岀概要（延迟导入避免编排层静态依赖通话领域层）。
    from ..calls.summary_generator import generate_call_summary

    summary = generate_call_summary(
        turns, call_context={"call_id": output.get("call_id")}
    )
    return str(summary.get("summary") or "").strip() or None


def _speak_announcement_on_terminal(
    deps: VoiceAgentDependencies,
    state: VoiceAgentState,
    announcement: str,
) -> dict[str, Any]:
    """固定流程：通话概要不经短信下发，直接经 sms_speak 在源终端 TTS 播报。

    仅通话内容类结果走该固定流程，其他结果仍维持“短信送达、不播报”。
    播报失败时 user_delivery.ok=False 并透传 playback 细节，不影响
    计划执行结果本身。
    """
    delivery = state.get("delivery_context")
    target = delivery.terminal_id if delivery is not None else None
    if target is None:
        return {
            "ok": False,
            "error": "source_terminal_unavailable",
            "content": announcement,
        }
    speaker = getattr(deps, "speak_announcement_on_terminal", None)
    if speaker is None:
        return {
            "ok": False,
            "error": "speak_announcement_on_terminal unavailable",
            "content": announcement,
        }
    try:
        playback = speaker(int(target), announcement)
    except Exception as exc:
        playback = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(playback, dict):
        playback = {"ok": False, "error": "invalid playback result"}
    log_trace(
        deps.logger,
        "通话概要sms_speak播报结果",
        request_id=state.get("request_id"),
        terminal_id=int(target),
        tool_name="sms_speak",
        playback=playback,
    )
    return {
        "ok": bool(playback.get("ok")),
        "content": announcement,
        "playback": playback,
    }


def deliver_result(state: VoiceAgentState) -> dict[str, Any]:
    """仅发送面向用户的文本结果，设备控制指令不会在此处重复下发。"""
    deps = _deps(state)
    gate = str(state.get("planner_gate") or "")
    proposal = state.get("plan_proposal") or {}
    execution = state.get("plan_execution") or {}
    # 固定流程：通话内容类结果自动做出概要，直接经 sms_speak 在源终端
    # TTS 播报（概要不再短信下发，见 _call_content_announcement）。
    announcement = _call_content_announcement(state)
    text: str | None = None
    if gate == "clarification":
        text = str(proposal.get("clarification") or "请补充必要信息。")
    elif gate == "no_capability":
        text = ""
    elif gate == "reject":
        text = ""
    else:
        text = announcement or _find_text(execution.get("outputs"))
        if not text and execution.get("status") == "FAILED":
            text = "执行失败：" + str(execution.get("error") or "未知错误")
    # 设备控制指令已经通过 MCP -> DeviceExecutor 下发，此处只处理可读文本，避免重复执行。
    if announcement and gate not in {"clarification", "no_capability", "reject"}:
        # 固定流程：概要不发短信，直接把概要文本经 sms_speak 下发源终端播报。
        playback_result = _speak_announcement_on_terminal(deps, state, announcement)
        # 透传发送链路真实 timing（protobuf 编码/HTTP/网关处理/ADK 提交），
        # 供时间树闭合；缺失时回退 0。
        playback_timing = dict((playback_result.get("playback") or {}).get("timing_ms") or {})
        if not playback_timing:
            playback_timing = {"total_ms": 0.0}
        return {
            "user_delivery": playback_result,
            "timing_ms": _with_timing(
                state,
                delivery_ms=playback_timing,
                delivery_done_epoch_ms=int(time.time() * 1000.0),
                delivery_done_source="text_delivery",
            ),
        }
    if not text:
        return {"user_delivery": None, "timing_ms": _no_send_timing(state)}

    # 错误提示只写日志，不再通过私人短信通知终端。成功结果、正常摘要、澄清等
    # 仍沿用原有发送链路。策略以依赖注入形式保留可替换边界，但默认行为固定为
    # “错误日志化、正常结果照常投递”。
    policy = getattr(deps, "user_delivery_policy", None) or DEFAULT_USER_DELIVERY_POLICY
    decision = policy.evaluate(state, text)
    if not decision.deliver:
        execution_error = str(execution.get("error") or state.get("planner_block_error") or "未知错误")
        deps.logger.error(
            "用户错误通知已抑制，仅记录日志 | request_id=%s reason=%s status=%s error=%s",
            state.get("request_id"),
            decision.reason,
            execution.get("status"),
            execution_error,
        )
        return {"user_delivery": None, "timing_ms": _no_send_timing(state)}

    delivery = state.get("delivery_context")
    target = delivery.terminal_id if delivery is not None else None
    if target is None:
        return {"user_delivery": {"ok": False, "error": "source_terminal_unavailable", "content": text},
                "timing_ms": _no_send_timing(state)}
    try:
        log_trace(
            deps.logger,
            "用户可读私信准备发送",
            request_id=state.get("request_id"),
            terminal_id=int(target),
            channel_id=None if delivery is None else delivery.channel_id,
            adk_method="TMP.SendCommand",
            require_ack=False,
            hex_encode=False,
            content=text,
        )
        delivery_started = time.perf_counter()
        response = deps.send_text_to_terminal(
            int(target),
            text,
            channel_id=None if delivery is None else delivery.channel_id,
            require_ack=False,
            hex_encode=False,
        )
        delivery_send_ms = round((time.perf_counter() - delivery_started) * 1000.0, 3)
        log_trace(
            deps.logger,
            "用户可读私信发送结果",
            request_id=state.get("request_id"),
            terminal_id=int(target),
            channel_id=None if delivery is None else delivery.channel_id,
            gateway_result=response,
            delivery_send_ms=delivery_send_ms,
        )
        response_timing = dict(response.get("timing_ms") or {})
        response_timing.setdefault("total_ms", delivery_send_ms)
        return {"user_delivery": {"ok": bool(response.get("ok")), "content": text, "gateway": response},
                "timing_ms": _with_timing(
                    state,
                    delivery_ms=response_timing,
                    delivery_done_epoch_ms=int(time.time() * 1000.0),
                    delivery_done_source="text_delivery",
                )}
    except Exception as exc:
        log_trace(
            deps.logger,
            "用户可读私信发送异常",
            request_id=state.get("request_id"),
            terminal_id=int(target),
            channel_id=None if delivery is None else delivery.channel_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return {
            "user_delivery": {"ok": False, "content": text, "error": f"{type(exc).__name__}: {exc}"},
            "timing_ms": _with_timing(
                state,
                delivery_ms={"total_ms": round((time.perf_counter() - delivery_started) * 1000.0, 3)},
                delivery_done_epoch_ms=int(time.time() * 1000.0),
                delivery_done_source="text_delivery",
            ),
        }


def _archive_message(state: VoiceAgentState) -> float:
    """需求 §11-§15：把消息异步归档到四库之一（Planner 不等待 SQLite 写入）。

    归档失败只记日志，不影响主流程结果。
    分类结果在 runtime.process_asr_task 阶段已由 classify_and_annotate
    标注到 data（message_category 等字段），这里据此重建 ClassificationResult，
    而不是把 finalize 的 result dict 误当分类对象传入。
    """
    deps = _deps(state)
    archive_started = time.perf_counter()
    try:
        from ..messages import MEDIUM_SMS, get_message_archive_service
        from ..messages.classification import ClassificationResult

        service = get_message_archive_service()
        if not service.enabled:
            return round((time.perf_counter() - archive_started) * 1000.0, 3)
        data = state.get("data") or {}
        medium = str(data.get("message_medium") or "").strip().lower()
        if not medium:
            return round((time.perf_counter() - archive_started) * 1000.0, 3)
        classification = ClassificationResult(
            category=str(data.get("message_category") or ""),
            source_id=data.get("source_id"),
            target_id=data.get("target_id"),
            group_id=data.get("group_id"),
            endpoint_complete=bool(data.get("endpoint_complete")),
            classification_reason=str(data.get("classification_reason") or ""),
            origin=data.get("origin"),
            medium=medium,
        )
        request_id = str(state.get("request_id") or "") or None
        # 网关回调未携带 call_id 时，归档阶段回绑 CallStore 会话：spool
        # 物化期（runtime._materialize_spooled_task）的解析发生在 ASR
        # 事件投影进 CallStore 之前，必然落空；归档在 finalize 阶段执行，
        # call_event_link 已建立，按 asr_event_id 反查即可绑定真实通话
        # （2026-09-01 真机复现：归档行 call_id 全 NULL，“上一通电话”
        # 查询拿到空绑定）。direct-text/sms 是命令注入而非通话
        # （wiki 08 §4），不绑定；网关已携带的 call_id 优先保留。
        if (
            not str(data.get("call_id") or "").strip()
            and str(data.get("input_mode") or "").strip().lower() not in {"text", "sms"}
        ):
            resolver = getattr(deps, "resolve_call_id", None)
            if callable(resolver):
                for candidate in (
                    state.get("asr_event_id"),
                    data.get("accepted_event_id"),
                    data.get("event_id"),
                ):
                    event_ref = str(candidate or "").strip()
                    if not event_ref:
                        continue
                    try:
                        resolved = str(resolver(event_ref) or "").strip()
                    except Exception:
                        resolved = ""
                    if resolved:
                        data = {**data, "call_id": resolved}
                        break
        if medium == MEDIUM_SMS:
            asr = state.get("asr_result") or {}
            text = str(
                data.get("text")
                or asr.get("text")
                or state.get("asr_text")
                or ""
            )
            service.submit_sms_event(
                classification,
                data,
                text=text,
                request_id=request_id,
            )
        else:
            service.submit_voice_result(
                classification, data, state.get("asr_result") or {}, request_id=request_id,
            )
        return round((time.perf_counter() - archive_started) * 1000.0, 3)
    except Exception:
        deps.logger.exception("消息归档提交失败 request_id=%s", state.get("request_id"))
        return round((time.perf_counter() - archive_started) * 1000.0, 3)


def finalize_result(state: VoiceAgentState) -> dict[str, Any]:
    deps = _deps(state)
    asr = state.get("asr_result") or {}
    execution = state.get("plan_execution") or {}
    proposal = state.get("plan_proposal") or {}
    delivery = state.get("delivery_context")
    received = state.get("received_time_ms")
    done = deps.now_ms()
    plan = state.get("operation_plan") or {}
    capabilities = [
        str((item or {}).get("capability") or (item or {}).get("operation_type") or "")
        for item in plan.get("steps") or []
    ]
    timing = dict(state.get("timing_ms") or {})
    queue_wait_ms = round(float(timing.get("agent_queue_wait_ms") or 0.0), 3)
    approval_wait_ms = round(float(timing.get("approval_wait_ms") or 0.0), 3)
    worker_wall_ms = deps.perf_ms(state["started_perf"])
    worker_active_total_ms = round(max(0.0, worker_wall_ms - approval_wait_ms), 3)
    capture_ms = iso_to_epoch_ms((state.get("data") or {}).get("occurred_at") or (state.get("data") or {}).get("captured_at"))
    e2e_total_ms = round(max(0.0, float(done) - float(capture_ms)), 3) if capture_ms is not None else (round(float(done) - float(received), 3) if received else worker_wall_ms)
    result = {
        "task_id": state.get("task_id"),
        "request_id": state.get("request_id"),
        "command_id": state.get("command_id"),
        "operation_id": state.get("operation_id"),
        "received_time_ms": received,
        "done_time_ms": done,
        "terminal_session_id": None if delivery is None else delivery.terminal_session_id,
        "channel_id": (state.get("data") or {}).get("channel_id"),
        "source_id": (state.get("data") or {}).get("source_id"),
        "message": {
            "category": (state.get("data") or {}).get("message_category"),
            "medium": (state.get("data") or {}).get("message_medium"),
            "endpoint_complete": (state.get("data") or {}).get("endpoint_complete"),
            "classification_reason": (state.get("data") or {}).get("classification_reason"),
        },
        "asr": {
            "ok": asr.get("ok"),
            "text": state.get("asr_text", ""),
            "error": asr.get("error"),
            "status_code": asr.get("status_code"),
            "model": asr.get("model"),
        },
        "plan": {
            "decision": proposal.get("decision"),
            "goal": proposal.get("goal"),
            "capabilities": capabilities,
            "catalog_hash": state.get("capability_catalog_hash"),
            "policy": state.get("policy_decision"),
            "approval": state.get("approval"),
            "error": state.get("planner_block_error"),
        },
        "execution": execution or None,
        "operation_result": state.get("operation_result"),
        "user_delivery": state.get("user_delivery"),
        "context_snapshot_id": state.get("context_snapshot_id"),
        "context_snapshot_hash": state.get("context_snapshot_hash"),
        "timing_ms": {
            "e2e_total_ms": e2e_total_ms,
            "agent_queue_wait_ms": queue_wait_ms,
            "queue_wait_ms": queue_wait_ms,
            "worker_active_ms": worker_active_total_ms,
            "worker_total_ms": worker_wall_ms,
            "approval_wait_ms": approval_wait_ms,
        },
    }
    runtime = deps.operation_runtime
    if runtime is not None and state.get("command_id"):
        status = str(execution.get("status") or "")
        if status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            runtime.update_command(
                str(state["command_id"]),
                status="FAILED" if status == "TIMED_OUT" else status,
                completed=True,
            )
        elif status == "DISPATCHED":
            runtime.update_command(str(state["command_id"]), status="DISPATCHED")
    # 需求 §11-§15：这里只统计归档入队；真正 SQLite write 在 writer 线程单独打印 archive_write_ms。
    archive_enqueue_ms = _archive_message(state)
    background_ms = {
        "archive_enqueue_ms": archive_enqueue_ms,
        "archive_write_ms": None,
        "event_publish_ms": timing.get("event_publish_ms", 0.0),
    }
    # latency closure: keep unknown gaps visible instead of hiding them inside totals.
    ingress = dict(timing.get("agent_ingress_ms") or {})
    # ACK 前移模式下，ingress.total 止于 ACK；后台持久化链（spool 持久化/
    # shadow 写入/真入队）耗时单独计 persist_to_enqueue_ms，位于 ingress
    # 结束与 queue_wait 起点之间。同步模式下为 None。
    persist_to_enqueue_ms = ingress.get("persist_to_enqueue_ms")
    ingress_known = sum(float(ingress.get(k) or 0.0) for k in (
        "auth_ms", "read_body_ms", "json_validate_ms", "persist_ms", "enqueue_ms", "ack_write_ms"
    ))
    ingress["unattributed_ms"] = round(max(0.0, float(ingress.get("total_ms") or 0.0) - ingress_known), 3)
    worker_children = dict(timing.get("worker_children_ms") or {})
    if worker_children:
        worker_known = sum(_timing_scalar_total(v) for v in worker_children.values())
    else:
        # 生产链路各节点直接把分段 timing 写到顶层（audio_convert_ms/asr_total_ms/
        # planner_total_ms/plan_execute_ms 等），而非 worker_children_ms；
        # 从实际字段取总耗时闭合，避免 unattributed 虚高为 total。
        worker_known = sum(
            _timing_scalar_total(timing.get(key))
            for key in (
                "audio_convert_ms",
                "asr_total_ms",
                "context_build_ms",
                "planner_total_ms",
                "policy_ms",
                "approval_active_ms",
                "plan_execute_ms",
            )
        )
    worker_gap = round(max(0.0, worker_active_total_ms - worker_known), 3)
    delivery_timing = dict(timing.get("delivery_ms") or {"total_ms": 0.0})
    # 网关侧分段随 payload.timing_ms 透传（首末包时间戳/说话时长/静音确认）。
    gateway_timing = dict((state.get("data") or {}).get("timing_ms") or {})
    gateway_speech_ms = gateway_timing.get("speech_duration_ms")
    gateway_silence_ms = gateway_timing.get("silence_confirm_ms")
    flushed_at_ms = gateway_timing.get("flushed_at_epoch_ms")
    # flush→agent 收到回调：覆盖网关侧 build/enqueue/queue_wait/HTTP 传输。
    gateway_flush_to_received_ms = (
        round(max(0.0, float(received) - float(flushed_at_ms)), 3)
        if flushed_at_ms is not None and received is not None
        else None
    )
    latency_tree_ms = {
        "e2e_total_ms": e2e_total_ms,
        "gateway_capture_to_callback_ms": {
            "first_packet_at": gateway_timing.get("first_packet_at"),
            "last_packet_at": gateway_timing.get("last_packet_at"),
            "speech_duration_ms": gateway_speech_ms,
            "silence_confirm_ms": gateway_silence_ms,
            "callback_build_ms": gateway_timing.get("callback_build_ms"),
            "flush_to_agent_received_ms": gateway_flush_to_received_ms,
            "outbox_enqueue_ms": None, "outbox_queue_wait_ms": None,
            "callback_http_ms": {"connect_ms": None, "write_ms": None, "read_ms": None, "total_ms": None},
            "note": "completed callback HTTP timing is printed by gateway after Agent ACK",
        },
        "agent_ingress_ms": ingress,
        "agent_persist_to_enqueue_ms": persist_to_enqueue_ms,
        "agent_queue_wait_ms": queue_wait_ms,
        "worker_active_ms": {
            "total_ms": worker_active_total_ms,
            "unattributed_ms": worker_gap,
            "audio_convert_ms": dict(timing.get("audio_convert_ms") or {}),
            "asr_total_ms": dict(timing.get("asr_total_ms") or {}),
            "context_build_ms": timing.get("context_build_ms", 0.0),
            "planner_total_ms": timing.get("planner_total_ms", {}),
            "policy_ms": timing.get("policy_ms", 0.0),
            "approval_active_ms": timing.get("approval_active_ms", 0.0),
            "approval_wait_ms": approval_wait_ms,
            "plan_execute_ms": timing.get("plan_execute_ms", {}),
            "plan_runtime_total_ms": timing.get("plan_runtime_total_ms", timing.get("plan_execute_ms", {}).get("total_ms", 0.0)),
            "delivery_ms": delivery_timing,
        },
        "background_ms": background_ms,
    }
    # 首包→编码发送完成（sms_speak 播报 / 私信下发至 ADK 接受提交）总耗时及闭合。
    # 仅在真实发生过发送（deliver_result 记录了 delivery_done_epoch_ms）时输出。
    delivery_done_epoch_ms = timing.get("delivery_done_epoch_ms")
    first_packet_epoch_ms = gateway_timing.get("first_packet_epoch_ms")
    first_packet_to_delivery_ms = (
        round(float(delivery_done_epoch_ms) - float(first_packet_epoch_ms), 3)
        if delivery_done_epoch_ms is not None and first_packet_epoch_ms is not None
        else None
    )
    # worker 开始→发送完成：worker_total 减去「发送完成→finalize 起点」的尾部。
    worker_to_delivery_ms = (
        round(worker_wall_ms - max(0.0, float(done) - float(delivery_done_epoch_ms)), 3)
        if delivery_done_epoch_ms is not None
        else None
    )
    speech_to_delivery = {
        "total_ms": first_packet_to_delivery_ms,
        "delivery_done_source": timing.get("delivery_done_source"),
        "speech_duration_ms": gateway_speech_ms,
        "silence_confirm_ms": gateway_silence_ms,
        "flush_to_agent_received_ms": gateway_flush_to_received_ms,
        "agent_ingress_ms": ingress.get("total_ms"),
        "agent_persist_to_enqueue_ms": persist_to_enqueue_ms,
        "agent_queue_wait_ms": queue_wait_ms,
        "worker_to_delivery_ms": worker_to_delivery_ms,
        "delivery_total_ms": delivery_timing.get("total_ms"),
        "note": (
            "first RTP packet -> encoded send accepted by ADK; "
            "text_delivery=sms_speak/private SMS, plan_execute=last MCP dispatch; "
            "total = speech + silence + flush_to_received + ingress + persist_to_enqueue "
            "+ queue_wait + worker_to_delivery + unattributed"
        ),
    }
    if first_packet_to_delivery_ms is not None:
        speech_known_parts = (
            gateway_speech_ms,
            gateway_silence_ms,
            gateway_flush_to_received_ms,
            ingress.get("total_ms"),
            persist_to_enqueue_ms,
            queue_wait_ms,
            worker_to_delivery_ms,
        )
        speech_to_delivery["unattributed_ms"] = round(
            float(first_packet_to_delivery_ms)
            - sum(float(v) for v in speech_known_parts if v is not None),
            3,
        )
    latency_tree_ms["speech_to_delivery_ms"] = speech_to_delivery
    # e2e 闭合：e2e_total ≈ speech + silence + flush_to_received
    # + ingress + persist_to_enqueue + queue_wait + worker_total + e2e_unattributed。
    # 异步持久化（VOICE_INGRESS_ASYNC_PERSIST）与队列等待并行时存在重叠，
    # e2e_unattributed 可能为负值，反映并行重叠而非缺失计时。
    if capture_ms is not None:
        e2e_known_parts = (
            gateway_speech_ms,
            gateway_silence_ms,
            gateway_flush_to_received_ms,
            ingress.get("total_ms"),
            persist_to_enqueue_ms,
            queue_wait_ms,
            worker_wall_ms,
        )
        latency_tree_ms["e2e_unattributed_ms"] = round(
            float(e2e_total_ms) - sum(float(v) for v in e2e_known_parts if v is not None),
            3,
        )
    result["latency_tree_ms"] = latency_tree_ms
    log_latency_tree(
        deps.logger, "E2E_TIME_TREE_AGENT", latency_tree_ms, request_id=state.get("request_id"),
        command_id=state.get("command_id"), correlation_id=(state.get("data") or {}).get("correlation_id"),
    )
    deps.write_jsonl_async(deps.result_jsonl, result)
    deps.append_latest_result(result)
    deps.inc_stat("processed_total", 1)
    execution_status = str(execution.get("status") or "")
    category = str(((state.get("data") or {}).get("message_category") or "")).strip().lower()
    if category.startswith("user_"):
        # 需求 §10：user 消息只归档不规划；成败只取决于 ASR。
        deps.inc_stat("user_message_total", 1)
        success = bool(asr.get("ok"))
    else:
        success = bool(asr.get("ok")) and execution_status == "SUCCEEDED"
    if success:
        deps.inc_stat("success_total", 1)
    elif execution_status in {"DISPATCHED", "WAITING"}:
        deps.inc_stat("pending_total", 1)
    else:
        deps.inc_stat("failed_total", 1)
    deps.update_stats(last_result=result, last_error=state.get("planner_block_error") or asr.get("error"))
    deps.log_event(
        deps.logger,
        "语音消息处理完成",
        **delivery_log_fields(
            delivery,
            request_id=state.get("request_id"),
            tool_name=_last_capability(state),
            delivery_status=None if state.get("user_delivery") is None else ("sent" if (state.get("user_delivery") or {}).get("ok") else "failed"),
        ),
        asr_ok=asr.get("ok"),
        asr_text=state.get("asr_text", ""),
        plan_decision=proposal.get("decision"),
        goal=proposal.get("goal"),
        capabilities=capabilities,
        execution_status=execution.get("status"),
        operation_status=(state.get("operation_result") or {}).get("operation_status"),
        elapsed_ms=result["timing_ms"]["worker_total_ms"],
    )
    return {"result": result}
