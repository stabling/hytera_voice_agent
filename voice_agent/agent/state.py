# =============================================================================
# 【维护注释】文件：voice_agent/agent/state.py
# 所属层次：LangGraph 编排层
# 主要职责：负责状态图、节点、依赖绑定、检查点和工作流路由；业务能力通过注册表和 runtime 注入，不直接硬编码设备协议。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：VoiceAgentState
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Typed shared state for the capability-driven voice-agent graph."""
from __future__ import annotations

from typing import Any, TypedDict


class VoiceAgentState(TypedDict, total=False):
    task: dict[str, Any]
    delivery_context: Any
    dependencies: Any
    started_perf: float
    task_id: Any
    request_id: Any
    data: dict[str, Any]
    received_time_ms: int | None
    queued_time_ms: int | None
    worker_dequeued_time_ms: int | None
    worker_started_time_ms: int | None
    approval_wait_started_time_ms: int | None
    timing_ms: dict[str, Any]
    routing_error: str | None
    command_id: str | None
    data_url: str | None
    convert_result: dict[str, Any]
    asr_result: dict[str, Any]
    asr_text: str
    asr_event_id: str | None
    decision_context: dict[str, Any] | None
    context_snapshot_id: str | None
    context_snapshot_hash: str | None
    context_shadow_only: bool
    capability_availability: list[dict[str, Any]]
    capability_catalog_hash: str | None
    plan_proposal: dict[str, Any] | None
    operation_plan: dict[str, Any] | None
    policy_decision: dict[str, Any] | None
    approval: dict[str, Any] | None
    approval_resume: dict[str, Any] | None
    planner_gate: str | None
    planner_block_error: str | None
    plan_execution: dict[str, Any] | None
    mcp_result: dict[str, Any] | None
    operation_id: str | None
    operation_result: dict[str, Any] | None
    user_delivery: dict[str, Any] | None
    result: dict[str, Any]
