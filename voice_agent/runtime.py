# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/runtime.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.event_models, shared_contracts.event_types, shared_contracts.identifiers, shared_contracts.models, shared_contracts.trace_context, shared_contracts.routes, shared_contracts.security, shared_runtime.architecture
# 主要公开符号：SpoolTaskAlreadyHandled, process_asr_task, asr_worker, start_workers
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Non-blocking HTTP callback server."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from shared_contracts.event_models import DomainEvent, EventQuery
from shared_contracts.event_types import (
    VOICE_CALLBACK_ACCEPTED,
    VOICE_CALLBACK_DUPLICATE,
)
from shared_contracts.identifiers import new_event_id
from shared_contracts.models import VoiceEvent, ensure_voice_event_contract
from shared_contracts.trace_context import (
    TraceContext,
    extract_trace_context,
    use_trace_context,
)
from shared_contracts.routes import LOCAL_API_TOKEN_HEADER
from shared_contracts.security import configured_token, token_is_valid
from shared_runtime.architecture import current_architecture
from shared_runtime.observability import traced_http_handler

from . import config
from .audio import callback_audio_to_data_url, detect_codec, detect_payload_type
from .agent import VoiceAgentDependencies, VoiceAgentProcessor
from .agent.framework import FRAMEWORK_BACKEND
from .agent.logging_fields import delivery_log_fields as _delivery_log_fields
from .dashscope_client import call_configured_asr
from .delivery_policy import DEFAULT_USER_DELIVERY_POLICY
from .gateway_client import health as gateway_health, send_private_sms
from .callback_messages import callback_message
from .context.event_publisher import build_event_publisher
from .mcp_result import build_mcp_result
from .terminal_delivery import deliver_mcp_result_to_terminal
from .context.runtime import get_default_context_memory_runtime
from .callback_spool import (
    CallbackPayloadInvalid,
    CallbackPayloadTooLarge,
    CallbackSpool,
    CallbackSpoolCapacityError,
)
from .calls.api import parse_call_selector, query_scope
from .calls.runtime import build_call_store_runtime
from .calls.command_plan import ExactCallContentPlan, ExactCallPlanExecutor
from .calls.content_service import CallContentService
from .calls.derived_repository import DerivedContentRepository
from .calls.resolver import CallResolver
from .contact_directory import (
    get_contact_list,
    get_contact_map,
    load_contact_map,
    update_contact_map,
)
from .zone_channel_directory import (
    get_zone_channel_list,
    get_zone_channel_map,
    load_zone_channel_map,
    update_zone_channel_map,
)
from .logging_utils import configure_console_logging, log_event
from .mcp_executor import execute_tool_via_http
from .messages import MEDIUM_SMS, MEDIUM_VOICE, UserHistoryService, get_message_archive_service
from .reminder_scheduler import REMINDER_SCHEDULER, configure_operation_runtime
from .request_ids import next_voice_request_id
from .operations.runtime import build_operation_runtime
from .planning.runtime import PlanningRuntime
from .capabilities import CapabilityRegistry, LocalCapabilityExecutor
from .reasoning import SemanticPlanner
from .storage.archive_worker import build_archive_worker
from .storage.interfaces import RepositoryScope
from .slots import (
    get_business_slots,
    load_business_slots,
    refresh_contact_slots,
    refresh_zone_channel_slots,
)
from .utils import (
    append_latest_result,
    ensure_dirs,
    get_latest_results,
    get_stats,
    inc_stat,
    log_worker,
    now_iso,
    now_ms,
    perf_ms,
    update_stats,
    write_jsonl_async,
)
from .terminal_sessions import (
    TerminalDeliveryContext,
    TerminalSessionManager,
    normalize_optional_channel_id,
    validate_terminal_id,
)

ASR_QUEUE: "queue.Queue[dict | None]" = queue.Queue(maxsize=config.MAX_QUEUE_SIZE)
ASR_QUEUE_REPLACE_LOCK = threading.Lock()
CALLBACK_SPOOL = CallbackSpool(
    config.CALLBACK_SPOOL_DIR,
    max_events=config.CALLBACK_SPOOL_MAX_EVENTS,
    max_bytes=config.CALLBACK_SPOOL_MAX_BYTES,
    max_audio_bytes=config.CALLBACK_MAX_AUDIO_BYTES,
    retention_seconds=config.CALLBACK_SPOOL_RETENTION_SECONDS,
)
TERMINAL_SESSION_MANAGER = TerminalSessionManager(
    config.TERMINAL_SESSION_IDLE_TIMEOUT_SECONDS
)
logger = logging.getLogger(__name__)
EVENT_PUBLISHER = build_event_publisher(
    source_service="voice-agent",
    default_path=config.EVENT_JOURNAL_PATH,
)
CALL_STORE_RUNTIME = build_call_store_runtime(default_path=config.CALL_STORE_PATH)
OPERATION_RUNTIME = build_operation_runtime(
    path=config.OPERATION_STORE_PATH,
    executor_mode=config.OPERATION_EXECUTOR_MODE,
    default_timeout_seconds=config.OPERATION_DEFAULT_TIMEOUT_SECONDS,
    enabled=True,
    audit_enabled=config.OPERATION_AUDIT_ENABLE,
)
configure_operation_runtime(OPERATION_RUNTIME)
CAPABILITY_REGISTRY = CapabilityRegistry()
SEMANTIC_PLANNER = SemanticPlanner(CAPABILITY_REGISTRY, max_steps=config.PLAN_MAX_STEPS)
LOCAL_CAPABILITY_EXECUTOR = LocalCapabilityExecutor(REMINDER_SCHEDULER)
PLANNING_RUNTIME = PlanningRuntime(
    operation_runtime=OPERATION_RUNTIME,
    capability_registry=CAPABILITY_REGISTRY,
    semantic_planner=SEMANTIC_PLANNER,
    planning_store_path=config.PLANNING_STORE_PATH,
    approval_store_path=config.APPROVAL_STORE_PATH,
    policy_mode=config.POLICY_MODE,
    auto_execute_max_risk=config.AUTO_EXECUTE_MAX_RISK,
    r2_allowlist=config.POLICY_R2_ALLOWLIST,
    max_steps=config.PLAN_MAX_STEPS,
    max_replans=config.PLAN_MAX_REPLANS,
    max_model_calls=config.PLAN_MAX_MODEL_CALLS,
    total_timeout_seconds=config.PLAN_TOTAL_TIMEOUT_SECONDS,
    approval_allowed_roles=config.APPROVAL_ALLOWED_ROLES,
)
CONTEXT_MEMORY_RUNTIME = get_default_context_memory_runtime()
CONTEXT_REPOSITORY = CONTEXT_MEMORY_RUNTIME.context_repository
CONTEXT_BUILDER = CONTEXT_MEMORY_RUNTIME.context_builder
HISTORY_SERVICE = CONTEXT_MEMORY_RUNTIME.history_service
MEMORY_REPOSITORY = CONTEXT_MEMORY_RUNTIME.memory_repository
MEMORY_VERIFIER = CONTEXT_MEMORY_RUNTIME.memory_verifier
OBJECT_ARCHIVER = build_archive_worker(
    enabled=config.OBJECT_STORE_ENABLE, bucket=config.OBJECT_STORE_BUCKET,
    prefix=config.OBJECT_STORE_PREFIX, endpoint_url=config.OBJECT_STORE_ENDPOINT_URL,
    kms_key_id=config.OBJECT_STORE_KMS_KEY_ID, queue_size=config.OBJECT_ARCHIVE_QUEUE_SIZE,
    filesystem_root=config.OBJECT_STORE_FILESYSTEM_ROOT,
)
_FIXED_MCP_RESULT_RADIO_ID = 5
# 消息身份分类 + 四库归档服务（需求 §2-§15）；进程内单例。
MESSAGE_ARCHIVE_SERVICE = get_message_archive_service()
# QUEUED 记录滞留超过该窗口才允许恢复 feeder 补投（默认覆盖进程重启场景）；
# 窗口内的 QUEUED 视为仍在内存队列中等待 worker 领取，补投会导致重复处理。
_QUEUED_REQUEUE_GRACE_SECONDS = 30.0


class SpoolTaskAlreadyHandled(Exception):
    """同一 spool 事件被重复入队时，后续任务抢不到处理权时抛出。"""


CALL_CONTENT_RESOLVER = CallResolver(
    CALL_STORE_RUNTIME.repository,
    default_scope=config.CALL_RESOLVE_DEFAULT_SCOPE,
    max_candidates=config.CALL_RESOLVE_MAX_CANDIDATES,
    # 摘要对象=用户间通话（需求 §22）：端点四元组未命中时回退归档
    # user_voice 参与方判定，避免频道广播被投影为中转台端点后拒收。
    user_voice_access=UserHistoryService(
        MESSAGE_ARCHIVE_SERVICE.store, MESSAGE_ARCHIVE_SERVICE.metrics
    ).call_access,
)
CALL_DERIVED_REPOSITORY = DerivedContentRepository(config.CALL_STORE_PATH)
CALL_CONTENT_SERVICE = CallContentService(
    repository=CALL_STORE_RUNTIME.repository,
    derived_repository=CALL_DERIVED_REPOSITORY,
    resolver=CALL_CONTENT_RESOLVER,
)
CALL_CONTENT_PLAN_EXECUTOR = ExactCallPlanExecutor(
    CALL_CONTENT_SERVICE, timeout_seconds=config.CALL_CONTENT_WAIT_TIMEOUT_SECONDS
)


def _schedule_exact_call_content(**values):
    return CALL_CONTENT_PLAN_EXECUTOR.schedule(ExactCallContentPlan(**values))


# 固定流程：概要文本直接经 sms_speak TTS 播报，不再短信下发。
def _speak_announcement_on_terminal(
    target_id: int, text: str, *, channel_id: int | None = None
) -> dict:
    """固定流程：把通话概要文本直接下发 sms_speak，在源终端 TTS 播报。

    概要不再经 /sms/private 短信送达（终端不会入库、不占短信存储），
    而是作为 TTS 播报文本以 protobuf 控制指令下发（save=false，
    不在设备生成短信记录）。channel_id 不传语音采集通道，让网关按
    终端当前路由自动选择（与 GatewayTerminalExecutor 下行策略一致）。
    """
    envelope = build_mcp_result(
        "sms_speak",
        {"text": text, "save": False},
        request_id=next_voice_request_id(),
    )
    return deliver_mcp_result_to_terminal(
        envelope,
        target_id=int(target_id),
        channel_id=channel_id,
        encode=True,
    )


def _agent_dependencies() -> VoiceAgentDependencies:
    # Keep runtime assembly compatible with older deployments where
    # VoiceAgentDependencies did not yet expose ``user_delivery_policy``.
    # This matters for incremental/overlay deployments: runtime.py may be
    # updated before voice_agent/agent/dependencies.py is replaced.
    dependency_kwargs = {
        "callback_audio_to_data_url": callback_audio_to_data_url,
        "call_asr": call_configured_asr,
        "execute_tool_direct": execute_tool_via_http,
        "send_text_to_terminal": send_private_sms,
        "detect_payload_type": detect_payload_type,
        "detect_codec": detect_codec,
        "write_jsonl_async": write_jsonl_async,
        "append_latest_result": append_latest_result,
        "inc_stat": inc_stat,
        "update_stats": update_stats,
        "log_event": log_event,
        "now_ms": now_ms,
        "perf_ms": perf_ms,
        "result_jsonl": config.RESULT_JSONL,
        "logger": logger,
        "capability_registry": CAPABILITY_REGISTRY,
        "planning_runtime": PLANNING_RUNTIME,
        "local_capability_executor": LOCAL_CAPABILITY_EXECUTOR,
        "fixed_result_radio_id": _FIXED_MCP_RESULT_RADIO_ID,
        "publish_event": EVENT_PUBLISHER.publish,
        "project_event": CALL_STORE_RUNTIME.consume,
        "collect_transcript": CALL_STORE_RUNTIME.collect_transcript,
        "operation_runtime": OPERATION_RUNTIME,
        "context_builder": (CONTEXT_BUILDER if config.CONTEXT_BUILDER_MODE != "off" else None),
    }

    try:
        dependency_parameters = inspect.signature(VoiceAgentDependencies).parameters
    except (TypeError, ValueError):
        dependency_parameters = {}

    if "user_delivery_policy" in dependency_parameters:
        dependency_kwargs["user_delivery_policy"] = DEFAULT_USER_DELIVERY_POLICY
    else:
        logger.warning(
            "VoiceAgentDependencies compatibility fallback: "
            "user_delivery_policy is not accepted by the loaded dependency class; "
            "using node-level DEFAULT_USER_DELIVERY_POLICY fallback",
            extra={
                "event": "voice_agent_dependencies_compat_fallback",
                "dependencies_class": f"{VoiceAgentDependencies.__module__}.{VoiceAgentDependencies.__qualname__}",
            },
        )

    if "speak_announcement_on_terminal" in dependency_parameters:
        dependency_kwargs["speak_announcement_on_terminal"] = _speak_announcement_on_terminal

    if "resolve_call_id" in dependency_parameters:
        # 归档阶段回绑 CallStore call_id：spool 物化期解析过早，此时
        # call_event_link 尚未建立，必须延迟到 finalize 归档时反查。
        dependency_kwargs["resolve_call_id"] = CALL_STORE_RUNTIME.resolve_call_id_for_event

    return VoiceAgentDependencies(**dependency_kwargs)


VOICE_AGENT_PROCESSOR = VoiceAgentProcessor(_agent_dependencies)


def _bind_delivery_for_worker(task: dict) -> dict:
    """Perform session, summary and routing work outside the ACK critical path."""

    data = task.get("data") or {}
    request_id = task.get("request_id")
    received_time_ms = int(task.get("received_time_ms") or now_ms())
    delivery_context = task.get("delivery_context")
    routing_error = task.get("routing_error")
    if delivery_context is None and routing_error is None:
        try:
            source_terminal_id = validate_terminal_id(data.get("source_id"))
        except ValueError:
            source_terminal_id = None
            routing_error = "missing_or_invalid_source_terminal_id"
        for expired in TERMINAL_SESSION_MANAGER.cleanup_expired():
            log_event(
                logger,
                "终端会话已过期清理",
                request_id=request_id,
                terminal_id=expired.terminal_id,
                terminal_session_id=expired.session_id,
                channel_id=expired.channel_id,
                tool_name=None,
                delivery_status="expired",
            )
        if source_terminal_id is not None:
            delivery_context, created = TERMINAL_SESSION_MANAGER.bind_delivery(
                terminal_id=source_terminal_id,
                channel_id=normalize_optional_channel_id(data.get("channel_id")),
                capture_session_id=data.get("session_id"),
                source_request_id=request_id,
            )
            if created:
                log_event(
                    logger,
                    "终端会话已创建",
                    **_delivery_log_fields(delivery_context, delivery_status="created"),
                )
            log_event(
                logger,
                "语音任务已绑定终端",
                **_delivery_log_fields(delivery_context, delivery_status="bound"),
                terminal_sequence=delivery_context.terminal_sequence,
                capture_session_id=delivery_context.capture_session_id,
            )
    task["delivery_context"] = delivery_context
    task["routing_error"] = routing_error
    return task


def _materialize_spooled_task(task: dict) -> dict:
    event_id = str(task.get("spool_event_id") or "")
    if not event_id:
        return task
    if not CALLBACK_SPOOL.mark_processing(event_id):
        # 未抢到处理权：记录已在处理中或已完成（同一回调被重复入队时
        # 的第二个任务），直接跳过执行，避免待办/短信被重复发送。
        raise SpoolTaskAlreadyHandled(event_id)
    record = CALLBACK_SPOOL.get(event_id)
    if record is None:
        raise KeyError(f"callback spool record not found: {event_id}")
    data = CALLBACK_SPOOL.load_task_data(event_id)
    data["audio_path"] = str(record.spool_path)
    try:
        data["audio_sha256"] = hashlib.sha256(record.spool_path.read_bytes()).hexdigest()
    except OSError:
        data["audio_sha256"] = None
    data.setdefault("capture_session_id", data.get("session_id"))
    projected_call_id = CALL_STORE_RUNTIME.resolve_call_id_for_voice_data(data)
    if projected_call_id:
        data["call_id"] = projected_call_id
    request_id: int | str
    try:
        request_id = int(record.request_id)
    except ValueError:
        request_id = record.request_id
    materialized = {
        **task,
        "task_id": request_id,
        "request_id": request_id,
        "received_time_ms": int(task.get("received_time_ms") or now_ms()),
        "data": data,
        "event_id": event_id,
        "correlation_id": data.get("correlation_id") or event_id,
    }
    return _bind_delivery_for_worker(materialized)


def process_asr_task(task: dict) -> dict | None:
    """Process one audio or direct-text task through the same LangGraph workflow."""

    task.setdefault("worker_dequeued_time_ms", now_ms())
    task.setdefault("worker_started_time_ms", now_ms())
    event_id = str(task.get("spool_event_id") or "")
    try:
        materialized = (
            _materialize_spooled_task(task)
            if event_id
            else _bind_delivery_for_worker(dict(task))
        )
    except SpoolTaskAlreadyHandled as exc:
        log_event(
            logger,
            "重复入队的回调已跳过执行",
            event_id=str(exc),
        )
        return None
    delivery_context = materialized.get("delivery_context")
    data = materialized.get("data") or {}
    # 需求 §2-§10：进程启动时加载 CONTROL_IDS，分类只做内存 O(1) 集合判断。
    # direct-text/sms 测试入口补 source=模拟终端、target=LOCAL_CONTROL_ID（§25）；
    # 网关语音回调到达本系统即接收方为中转台，target/group 均缺失时补
    # target=LOCAL_CONTROL_ID（§35 场景 5：入站私语音），否则会被误判为
    # user_voice 而拒绝进入 Planner。
    if MESSAGE_ARCHIVE_SERVICE.enabled:
        input_mode = str(data.get("input_mode") or "").strip().lower()
        is_direct_entry = input_mode in {"text", "sms"}
        MESSAGE_ARCHIVE_SERVICE.classify_and_annotate(
            data,
            medium=(MEDIUM_SMS if input_mode == "sms" else MEDIUM_VOICE),
            complete_direct_text=is_direct_entry,
            default_target_to_local_control=not is_direct_entry,
        )
    trace_context = extract_trace_context(payload=data)
    result: dict | None = None
    try:
        with use_trace_context(trace_context):
            if isinstance(delivery_context, TerminalDeliveryContext):
                with TERMINAL_SESSION_MANAGER.processing_turn(delivery_context):
                    result = VOICE_AGENT_PROCESSOR.process(materialized, delivery_context)
            else:
                result = VOICE_AGENT_PROCESSOR.process(materialized, None)
    except Exception as exc:
        if event_id:
            CALLBACK_SPOOL.mark_failed(event_id, f"{type(exc).__name__}: {exc}")
        raise
    else:
        if event_id:
            archive_record = CALLBACK_SPOOL.get(event_id)
            CALLBACK_SPOOL.mark_done(event_id, cleanup_audio=not OBJECT_ARCHIVER.enabled)
            if OBJECT_ARCHIVER.enabled and archive_record is not None and not OBJECT_ARCHIVER.submit(archive_record):
                logger.error("object_archive_backpressure event_id=%s local_audio_retained=true", event_id)
        return result


def _cancel_queued_asr_tasks(*, keep_event_id: str | None, reason: str) -> int:
    """Cancel only tasks still waiting in memory or the durable pending spool."""
    cancelled_event_ids: set[str] = set()
    if keep_event_id is not None:
        cancelled_event_ids.update(
            CALLBACK_SPOOL.cancel_pending_except(keep_event_id, reason=reason)
        )

    sentinels = 0
    preserved: list[dict] = []
    cancelled = 0
    while True:
        try:
            queued_task = ASR_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            if queued_task is None:
                sentinels += 1
                continue
            event_id = str(queued_task.get("spool_event_id") or "")
            if event_id and event_id == keep_event_id:
                preserved.append(queued_task)
                continue
            if event_id:
                cancelled_event_ids.add(event_id)
            cancelled += 1
        finally:
            ASR_QUEUE.task_done()

    for queued_task in preserved:
        ASR_QUEUE.put_nowait(queued_task)
    for _ in range(sentinels):
        ASR_QUEUE.put_nowait(None)

    cancelled_operations = 0
    for cancelled_event_id in sorted(cancelled_event_ids):
        cancelled_operations += OPERATION_RUNTIME.cancel_source(
            cancelled_event_id, reason=reason
        )
    if cancelled or cancelled_event_ids:
        inc_stat("queued_cancelled_total", max(cancelled, len(cancelled_event_ids)))
        log_event(
            logger,
            "新任务替换排队中的旧任务",
            keep_event_id=keep_event_id,
            cancelled_queue_items=cancelled,
            cancelled_event_ids=sorted(cancelled_event_ids),
            cancelled_operations=cancelled_operations,
            reason=reason,
        )
    return max(cancelled, len(cancelled_event_ids))


def _enqueue_latest_asr_task(
    task: dict, *, event_id: str | None = None
) -> tuple[int, bool]:
    """Place a new task next, replacing only tasks that workers have not started."""
    with ASR_QUEUE_REPLACE_LOCK:
        if event_id is not None:
            current = CALLBACK_SPOOL.get(event_id)
            if current is None or current.status not in {"RECEIVED", "QUEUED", "FAILED"}:
                return 0, False
        cancelled = 0
        if config.CANCEL_QUEUED_TASKS_ON_NEW_TASK:
            cancelled = _cancel_queued_asr_tasks(
                keep_event_id=event_id,
                reason="replaced_by_new_voice_task",
            )
        # Persist QUEUED while the same replacement lock is still held.
        # Otherwise the recovery feeder can observe the short RECEIVED window
        # after put_nowait() and enqueue the same event a second time.
        if event_id is not None:
            CALLBACK_SPOOL.mark_queued(event_id)
        ASR_QUEUE.put_nowait(task)
        return cancelled, True


def asr_worker(worker_id: int) -> None:
    log_event(logger, "ASR 工作线程已启动", worker_id=worker_id)
    while True:
        task = ASR_QUEUE.get()
        try:
            if task is None:
                return
            if isinstance(task, dict):
                task.setdefault("worker_dequeued_time_ms", now_ms())
            process_asr_task(task)
        except Exception as exc:
            log_event(
                logger,
                "ASR 工作线程任务失败",
                worker_id=worker_id,
                event_id=task.get("spool_event_id") if isinstance(task, dict) else None,
                exception_type=type(exc).__name__,
                error=str(exc),
            )
        finally:
            ASR_QUEUE.task_done()


def _spool_recovery_feeder() -> None:
    """Continuously feed persisted work without exceeding the bounded ASR queue."""

    enqueued: set[str] = set()
    while True:
        try:
            records = CALLBACK_SPOOL.recover_pending(limit=max(1, config.MAX_QUEUE_SIZE))
            pending_ids = {record.event_id for record in records}
            enqueued.intersection_update(pending_ids)
            for record in records:
                if record.event_id in enqueued:
                    continue
                # QUEUED 新鲜窗口：刚入队尚未被 worker 领取的记录很可能还在
                # 内存队列里，重复投递会导致同一回调被处理两次；
                # 只有滞留超过窗口的 QUEUED 才视为丢失需要补投（如进程重启）。
                if (
                    record.status == "QUEUED"
                    and record.updated_at > 0
                    and (time.time() - record.updated_at) < _QUEUED_REQUEUE_GRACE_SECONDS
                ):
                    continue
                try:
                    with ASR_QUEUE_REPLACE_LOCK:
                        current = CALLBACK_SPOOL.get(record.event_id)
                        if current is None or current.status not in {
                            "RECEIVED",
                            "QUEUED",
                            "FAILED",
                        }:
                            continue
                        ASR_QUEUE.put_nowait(
                            {
                                "spool_event_id": record.event_id,
                                "received_time_ms": now_ms(),
                            }
                        )
                        CALLBACK_SPOOL.mark_queued(record.event_id)
                        enqueued.add(record.event_id)
                except queue.Full:
                    break
        except Exception as exc:
            log_event(
                logger,
                "回调 spool 恢复扫描失败",
                exception_type=type(exc).__name__,
                error=str(exc),
            )
        time.sleep(0.2)


def start_workers() -> None:
    threading.Thread(target=log_worker, daemon=True).start()
    for i in range(config.WORKER_COUNT):
        threading.Thread(target=asr_worker, args=(i + 1,), daemon=True).start()
    threading.Thread(
        target=_spool_recovery_feeder,
        name="callback-spool-recovery-feeder",
        daemon=True,
    ).start()
    CALL_STORE_RUNTIME.start_projector(EVENT_PUBLISHER)

