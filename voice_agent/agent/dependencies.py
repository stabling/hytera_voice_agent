# =============================================================================
# 【维护注释】文件：voice_agent/agent/dependencies.py
# 所属层次：LangGraph 编排层
# 主要职责：负责状态图、节点、依赖绑定、检查点和工作流路由；业务能力通过注册表和 runtime 注入，不直接硬编码设备协议。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：VoiceAgentDependencies
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Dependency boundary between the LangGraph and application services."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class VoiceAgentDependencies:
    callback_audio_to_data_url: Callable[..., tuple[str | None, dict[str, Any]]]
    call_asr: Callable[[str], dict[str, Any]]
    execute_tool_direct: Callable[..., dict[str, Any]]
    send_text_to_terminal: Callable[..., dict[str, Any]]
    detect_payload_type: Callable[[dict[str, Any]], Any]
    detect_codec: Callable[[dict[str, Any]], Any]
    write_jsonl_async: Callable[[str | Path, dict[str, Any]], Any]
    append_latest_result: Callable[[dict[str, Any]], Any]
    inc_stat: Callable[..., Any]
    update_stats: Callable[..., Any]
    log_event: Callable[..., Any]
    now_ms: Callable[[], int]
    perf_ms: Callable[[float], float]
    result_jsonl: str | Path
    logger: logging.Logger
    capability_registry: Any
    planning_runtime: Any
    local_capability_executor: Any
    fixed_result_radio_id: int = 5
    publish_event: Callable[[Any], Any] | None = None
    project_event: Callable[[Any], Any] | None = None
    collect_transcript: Callable[[Any], Any] | None = None
    operation_runtime: Any | None = None
    context_builder: Any | None = None
    # 固定流程：通话概要文本直接经 sms_speak 在源终端 TTS 播报（不经短信）。
    speak_announcement_on_terminal: Callable[..., dict[str, Any]] | None = None
    # 归档回绑：网关回调未携带 call_id 时，按 event_id 反查 CallStore 通话会话
    # （spool 物化期的解析发生在 ASR 事件投影之前，必须延迟到归档阶段回绑）。
    resolve_call_id: Callable[[str], str | None] | None = None
    # Optional for backwards-compatible runtime assembly and overlay deployments.
    user_delivery_policy: Any | None = None
