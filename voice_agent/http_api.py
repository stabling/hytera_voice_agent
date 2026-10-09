# =============================================================================
# 【维护注释】文件：voice_agent/http_api.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.runtime
# 主要公开符号：NonBlockingThreadingHTTPServer, CallbackHandler
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config
from .runtime import (
    ASR_QUEUE,
    CALLBACK_SPOOL,
    CALL_STORE_RUNTIME,
    CAPABILITY_REGISTRY,
    CONTEXT_REPOSITORY,
    EVENT_PUBLISHER,
    FRAMEWORK_BACKEND,
    HISTORY_SERVICE,
    MEMORY_REPOSITORY,
    MEMORY_VERIFIER,
    MESSAGE_ARCHIVE_SERVICE,
    OPERATION_RUNTIME,
    PLANNING_RUNTIME,
    SEMANTIC_PLANNER,
    VOICE_AGENT_PROCESSOR,
    CallbackPayloadInvalid,
    CallbackPayloadTooLarge,
    CallbackSpoolCapacityError,
    DomainEvent,
    EventQuery,
    LOCAL_API_TOKEN_HEADER,
    RepositoryScope,
    VOICE_CALLBACK_ACCEPTED,
    VOICE_CALLBACK_DUPLICATE,
    VoiceEvent,
    _enqueue_latest_asr_task,
    callback_message,
    configured_token,
    current_architecture,
    ensure_voice_event_contract,
    extract_trace_context,
    gateway_health,
    get_business_slots,
    get_contact_list,
    get_contact_map,
    get_latest_results,
    get_stats,
    get_zone_channel_list,
    get_zone_channel_map,
    inc_stat,
    log_event,
    logger,
    new_event_id,
    next_voice_request_id,
    now_ms,
    parse_call_selector,
    perf_ms,
    process_asr_task,
    query_scope,
    refresh_contact_slots,
    refresh_zone_channel_slots,
    token_is_valid,
    traced_http_handler,
    update_contact_map,
    update_stats,
    update_zone_channel_map,
    write_jsonl_async,
)

class NonBlockingThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = config.HTTP_REQUEST_QUEUE_SIZE


# ---------------------------------------------------------------------------
# 语音回调入口 ACK 前移（VOICE_INGRESS_ASYNC_PERSIST）
#
# 原同步路径：validate → spool 持久化(~75ms fsync) → ASR 入队(~50ms) → ACK，
# 网关在收到 ACK 前全程阻塞。异步路径：validate + 背压检查后立即回 ACK，
# 持久化/入队/事件归档由下方单线程 FIFO worker 串行执行，保证到达顺序。
# 失败不再能回传 4xx/5xx（ACK 已发出），仅记录诊断日志与统计；可靠性由
# 网关 outbox 重发 + spool event_id 幂等去重兑底。
# ---------------------------------------------------------------------------
_INGRESS_WORK_QUEUE: "queue.Queue[dict]" = queue.Queue()
_INGRESS_WORKER_LOCK = threading.Lock()
_INGRESS_WORKER_STARTED = False


def _ensure_ingress_worker() -> None:
    global _INGRESS_WORKER_STARTED
    with _INGRESS_WORKER_LOCK:
        if _INGRESS_WORKER_STARTED:
            return
        _INGRESS_WORKER_STARTED = True

    def _loop() -> None:
        while True:
            job = _INGRESS_WORK_QUEUE.get()
            try:
                if job is None:
                    return
                _voice_ingress_background_persist(job)
            except Exception:
                logger.exception(
                    "voice_ingress_background_failed event_id=%s",
                    (job or {}).get("event_id"),
                )
            finally:
                _INGRESS_WORK_QUEUE.task_done()

    thread = threading.Thread(
        target=_loop, name="voice-agent-ingress-persist", daemon=True
    )
    thread.start()


def _voice_ingress_background_persist(job: dict) -> None:
    """后台执行原同步路径的 spool 持久化 → shadow 写入 → ASR 入队 → 事件归档。"""
    data = job["data"]
    request_id = job["request_id"]
    correlation_id = job.get("correlation_id")
    event_id = str(data.get("event_id") or "")
    started = time.perf_counter()
    try:
        persist_t0 = time.perf_counter()
        try:
            accepted = CALLBACK_SPOOL.accept(
                data, request_id=request_id, queue_depth=ASR_QUEUE.qsize()
            )
        except (
            CallbackPayloadInvalid,
            CallbackPayloadTooLarge,
            CallbackSpoolCapacityError,
        ) as exc:
            # ACK 已提前返回，无法再回传 4xx/5xx；记录诊断。
            # 网关 outbox 在未收到 ACK 时会重发，spool 幂等可去重。
            inc_stat("backpressure_total" if int(getattr(exc, "status_code", 503)) >= 500 else "dropped_total")
            log_event(
                logger,
                "语音回调异步持久化被拒",
                correlation_id=correlation_id,
                event_id=event_id,
                reason=getattr(exc, "reason", "spool_error"),
                error=str(exc)[:300],
            )
            return
        persist_ms = perf_ms(persist_t0)

        if OPERATION_RUNTIME.enabled:
            try:
                OPERATION_RUNTIME.create_voice_command(
                    {
                        "task_id": request_id,
                        "request_id": request_id,
                        "spool_event_id": accepted.event_id,
                        "data": data,
                    },
                    None,
                )
            except Exception as exc:
                log_event(
                    logger,
                    "Command shadow write failed",
                    event_id=accepted.event_id,
                    error_type=type(exc).__name__,
                )

        enqueue_t0 = time.perf_counter()
        queue_inserted = False
        cancelled_count = 0
        # 真实入队时刻作为 queued_time_ms：ACK 前移后任务先经后台持久化链，
        # worker 的 agent_queue_wait 只应度量 ASR 队列驻留时间；
        # ACK→真入队 的后台链耗时单独记 persist_to_enqueue_ms。
        actual_queued_time_ms = now_ms()
        ack_epoch_ms = job.get("ack_epoch_ms")
        persist_to_enqueue_ms = (
            round(float(actual_queued_time_ms) - float(ack_epoch_ms), 3)
            if ack_epoch_ms is not None
            else None
        )
        if not accepted.duplicate:
            try:
                cancelled_count, queue_inserted = _enqueue_latest_asr_task(
                    {
                        "spool_event_id": accepted.event_id,
                        "received_time_ms": job.get("received_time_ms") or now_ms(),
                        "queued_time_ms": actual_queued_time_ms,
                        "agent_ingress_timing_ms": {
                            **(job.get("agent_ingress_timing_ms") or {}),
                            "persist_ms": persist_ms,
                            "persist_to_enqueue_ms": persist_to_enqueue_ms,
                        },
                    },
                    event_id=accepted.event_id,
                )
            except queue.Full:
                queue_inserted = False
            inc_stat("queued_total")
        else:
            inc_stat("duplicate_total")
        enqueue_ms = perf_ms(enqueue_t0)

        try:
            fact_type = (
                VOICE_CALLBACK_DUPLICATE if accepted.duplicate else VOICE_CALLBACK_ACCEPTED
            )
            fact_event_id = (
                new_event_id()
                if accepted.duplicate
                else str(data.get("accepted_event_id") or new_event_id())
            )
            fact = DomainEvent.from_voice_event(
                data,
                event_type=fact_type,
                source_service="voice-agent",
                event_id=fact_event_id,
                causation_id=accepted.event_id,
                data={
                    "voice_event_id": accepted.event_id,
                    "request_id": accepted.request_id,
                    "duplicate": accepted.duplicate,
                    "queued": not accepted.duplicate,
                    "queue_inserted": queue_inserted,
                    "payload_bytes": accepted.payload_bytes,
                    "payload_hash": accepted.payload_hash,
                    "observed_source_id": data.get("source_id"),
                    "observed_target_id": data.get("target_id"),
                    "group_id": data.get("group_id"),
                    "channel_id": data.get("channel_id"),
                    "call_type": data.get("call_type"),
                    "direction": data.get("direction") or "inbound",
                    "capture_session_id": data.get("session_id"),
                    "audio_path": str(accepted.spool_path),
                    "evidence_event_ids": list(data.get("evidence_event_ids") or []),
                    "context_confidence": data.get("context_confidence"),
                },
            )
            EVENT_PUBLISHER.publish(fact)
            CALL_STORE_RUNTIME.consume(fact)
        except Exception:
            if config.EVENT_JOURNAL_FAIL_MODE == "closed":
                raise
            logger.exception("callback_event_publish_failed event_id=%s", accepted.event_id)

        log_event(
            logger,
            "语音回调分段诊断",
            correlation_id=correlation_id,
            event_id=accepted.event_id,
            request_id=accepted.request_id,
            payload_bytes=accepted.payload_bytes,
            auth_ms=(job.get("agent_ingress_timing_ms") or {}).get("auth_ms"),
            read_body_ms=(job.get("agent_ingress_timing_ms") or {}).get("read_body_ms"),
            json_validate_ms=(job.get("agent_ingress_timing_ms") or {}).get("json_validate_ms"),
            ack_write_ms=(job.get("agent_ingress_timing_ms") or {}).get("ack_write_ms"),
            persist_ms=persist_ms,
            persist_to_enqueue_ms=persist_to_enqueue_ms,
            enqueue_ms=enqueue_ms,
            background_total_ms=perf_ms(started),
            total_ms=round(
                float((job.get("agent_ingress_timing_ms") or {}).get("total_ms") or 0.0)
                + perf_ms(started),
                3,
            ),
            queued=not accepted.duplicate,
            duplicate=accepted.duplicate,
            queue_depth=ASR_QUEUE.qsize(),
            exception_type=None,
        )
    except Exception:
        # 后台失败不影响已发出的 ACK；保留完整堆栈便于排查。
        if config.EVENT_JOURNAL_FAIL_MODE == "closed":
            raise
        logger.exception("voice_ingress_background_error event_id=%s", event_id)


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "VoiceCapabilityAgent"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def send_json(self, status_code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if status_code in {429, 503}:
            self.send_header("Retry-After", "1")
        correlation_id = getattr(self, "_request_correlation_id", None)
        if correlation_id:
            self.send_header("X-Correlation-ID", str(correlation_id))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (ConnectionError, OSError):
            # 客户端提前断开连接（如健康检查超时），属正常现象，静默忽略
            pass
        finally:
            self.close_connection = True

    def read_body(self) -> bytes:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(content_length) if content_length > 0 else b""

    def read_json(self) -> dict:
        raw_body = self.read_body()
        return json.loads(raw_body.decode("utf-8")) if raw_body else {}

    def is_authorized(self) -> bool:
        if not configured_token():
            return True
        return token_is_valid(self.headers.get(LOCAL_API_TOKEN_HEADER))

    def is_memory_admin_authorized(self) -> bool:
        """Apply a separate verifier token when one is configured.

        The normal loopback API token remains mandatory through ``is_authorized``.
        This second factor prevents a read-only local client from self-asserting an
        administrative role through ``X-Memory-Role`` alone.
        """
        expected = config.MEMORY_ADMIN_TOKEN
        if not expected:
            return True
        supplied = str(self.headers.get("X-Memory-Admin-Token") or "")
        return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def is_approval_admin_authorized(self) -> bool:
        expected = config.APPROVAL_ADMIN_TOKEN
        if not expected:
            return True
        supplied = str(self.headers.get("X-Approval-Admin-Token") or "")
        return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    @traced_http_handler("voice-agent.http")
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/health") and not self.is_authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if parsed.path in ("/", "/health"):
            try:
                peer = gateway_health(timeout=0.5)
            except Exception as exc:
                peer = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            runtime = current_architecture().as_dict()
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "langgraph_agent_x64",
                    "runtime": runtime,
                    "framework_backend": FRAMEWORK_BACKEND,
                    "gateway_peer": peer,
                    "message": "Qwen ASR + capability-planning agent is running",
                    "callback_url": "http://127.0.0.1:%d/voice/callback" % config.PORT,
                    "text_url": "http://127.0.0.1:%d/text/input" % config.PORT,
                    "plan_text_url": "http://127.0.0.1:%d/plan/text" % config.PORT,
                    "results_url": "http://127.0.0.1:%d/results/latest" % config.PORT,
                    "slots_url": "http://127.0.0.1:%d/business_slots" % config.PORT,
                    "contact_map_url": "http://127.0.0.1:%d/contact_map" % config.PORT,
                    "zone_channel_map_url": "http://127.0.0.1:%d/zone_channel_map" % config.PORT,
                    "dashscope_chat_completions_url": config.DASHSCOPE_CHAT_COMPLETIONS_URL,
                    "asr_model": (
                        config.LOCAL_ASR_MODEL
                        if config.ASR_PROVIDER == "local"
                        else config.ASR_MODEL
                    ),
                    "planner_model": config.PLANNER_DISPLAY_MODEL,
                    "language": config.ASR_LANGUAGE,
                    "enable_itn": config.ENABLE_ITN,
                    "sample_rate": config.DEFAULT_SAMPLE_RATE,
                    "workers": config.WORKER_COUNT,
                    "stats": get_stats(
                        {
                            "asr_queue_size": ASR_QUEUE.qsize(),
                            "asr_queue_max_size": config.MAX_QUEUE_SIZE,
                            "callback_spool": CALLBACK_SPOOL.stats(),
                            "event_journal": EVENT_PUBLISHER.stats(),
                            "call_store": CALL_STORE_RUNTIME.metrics(),
                            "operation_runtime": {
                                "enabled": OPERATION_RUNTIME.enabled,
                                "executor_mode": OPERATION_RUNTIME.executor.mode,
                                "recovery": OPERATION_RUNTIME.recovery,
                            },
                            "context_runtime": {
                                "mode": config.CONTEXT_BUILDER_MODE,
                                "max_items": config.CONTEXT_MAX_ITEMS,
                                "max_tokens": config.CONTEXT_MAX_TOKENS,
                            },
                            "message_archive": MESSAGE_ARCHIVE_SERVICE.snapshot(),
                            "memory_runtime": {
                                "mode": config.MEMORY_ENABLE,
                                "auto_verify": config.MEMORY_AUTO_VERIFY,
                                "fts": MEMORY_REPOSITORY.fts_available,
                                "vector_enabled": config.VECTOR_SEARCH_ENABLE,
                            },
                            "planning_runtime": {
                                "mode": "capability_planner",
                                "policy_mode": config.POLICY_MODE,
                                "auto_execute_max_risk": config.AUTO_EXECUTE_MAX_RISK,
                                "limits": {"max_steps": config.PLAN_MAX_STEPS, "max_replans": config.PLAN_MAX_REPLANS, "max_model_calls": config.PLAN_MAX_MODEL_CALLS, "timeout_seconds": config.PLAN_TOTAL_TIMEOUT_SECONDS},
                            },
                        }
                    ),
                },
            )
            return
        if parsed.path.startswith("/approvals/"):
            approval_id = parsed.path[len("/approvals/"):].strip()
            approval = PLANNING_RUNTIME.approvals.get(approval_id) if approval_id else None
            if approval is None:
                self.send_json(404, {"ok": False, "error": "approval not found"})
            else:
                self.send_json(200, {"ok": True, "approval": approval.model_dump(mode="json"), "events": PLANNING_RUNTIME.approvals.events(approval_id)})
            return
        if parsed.path.startswith("/plans/"):
            decision_id = parsed.path[len("/plans/"):].strip()
            plan = PLANNING_RUNTIME.repository.get_plan(decision_id) if decision_id else None
            if plan is None:
                self.send_json(404, {"ok": False, "error": "plan not found"})
            else:
                self.send_json(200, {"ok": True, "plan": plan.model_dump(mode="json")})
            return
        if parsed.path.startswith("/operations/"):
            operation_id = parsed.path[len("/operations/") :].strip()
            operation = OPERATION_RUNTIME.operations.get(operation_id) if operation_id else None
            if operation is None:
                self.send_json(404, {"ok": False, "error": "operation not found"})
            else:
                self.send_json(200, {
                    "ok": True,
                    "operation": OPERATION_RUNTIME.query_operations(operation_id=operation_id, limit=1)[0],
                    "attempts": OPERATION_RUNTIME.operations.attempts(operation_id),
                })
            return
        if parsed.path == "/operations":
            try:
                values = parse_qs(parsed.query, keep_blank_values=False)
                limit = min(max(1, int((values.get("limit") or [100])[0])), config.OPERATION_QUERY_MAX_LIMIT)
                offset = max(0, int((values.get("offset") or [0])[0]))
                operations = OPERATION_RUNTIME.query_operations(
                    command_id=(values.get("command_id") or [None])[0],
                    operation_id=(values.get("operation_id") or [None])[0],
                    actor_id=(values.get("actor_id") or [None])[0],
                    target_id=(values.get("target_id") or [None])[0],
                    operation_type=(values.get("operation_type") or [None])[0],
                    status=(values.get("status") or [None])[0],
                    limit=limit,
                    offset=offset,
                )
                self.send_json(200, {"ok": True, "operations": operations, "count": len(operations), "limit": limit, "offset": offset})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/commands/"):
            command_id = parsed.path[len("/commands/") :].strip()
            command = OPERATION_RUNTIME.commands.get(command_id) if command_id else None
            if command is None:
                self.send_json(404, {"ok": False, "error": "command not found"})
            else:
                self.send_json(200, {
                    "ok": True,
                    "command": command.model_dump(mode="json", exclude={"raw_text"}),
                    "operations": OPERATION_RUNTIME.query_operations(command_id=command_id),
                })
            return
        if parsed.path == "/commands":
            try:
                values = parse_qs(parsed.query, keep_blank_values=False)
                limit = min(max(1, int((values.get("limit") or [100])[0])), config.OPERATION_QUERY_MAX_LIMIT)
                offset = max(0, int((values.get("offset") or [0])[0]))
                commands = OPERATION_RUNTIME.query_commands(
                    actor_id=(values.get("actor_id") or [None])[0],
                    source_type=(values.get("source_type") or [None])[0],
                    status=(values.get("status") or [None])[0],
                    limit=limit,
                    offset=offset,
                )
                self.send_json(200, {"ok": True, "commands": commands, "count": len(commands), "limit": limit, "offset": offset})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/context/snapshots/"):
            snapshot_id = parsed.path[len("/context/snapshots/") :].strip()
            terminal_scope = str(self.headers.get("X-Hytera-Terminal-ID") or "").strip() or None
            tenant_scope = str(self.headers.get("X-Hytera-Tenant-ID") or "").strip() or config.CONTEXT_DEFAULT_TENANT_ID
            if not terminal_scope:
                self.send_json(403, {"ok": False, "error": "terminal scope required"})
                return
            replay = CONTEXT_REPOSITORY.replay(
                snapshot_id, actor_id=terminal_scope, terminal_id=terminal_scope, tenant_id=tenant_scope
            )
            if replay is None:
                self.send_json(404, {"ok": False, "error": "context snapshot not found"})
            else:
                self.send_json(200, {"ok": True, **replay})
            return
        if parsed.path.startswith("/history/"):
            terminal_scope = str(self.headers.get("X-Hytera-Terminal-ID") or "").strip()
            if not terminal_scope:
                self.send_json(403, {"ok": False, "error": "terminal scope required"})
                return
            try:
                values = parse_qs(parsed.query, keep_blank_values=False)
                limit = min(max(1, int((values.get("limit") or [20])[0])), config.CONTEXT_QUERY_MAX_LIMIT)
                if parsed.path == "/history/recent-calls":
                    result = HISTORY_SERVICE.recent_calls(
                        actor_terminal_id=terminal_scope,
                        days=int((values.get("days") or [config.CONTEXT_RECENT_CALL_DAYS])[0]),
                        limit=limit,
                    )
                elif parsed.path == "/history/entity-timeline":
                    result = HISTORY_SERVICE.entity_timeline(
                        actor_terminal_id=terminal_scope,
                        entity_id=(values.get("entity_id") or [""])[0],
                        limit=limit,
                    )
                elif parsed.path == "/history/similar-outcomes":
                    result = HISTORY_SERVICE.similar_outcomes(
                        actor_terminal_id=terminal_scope,
                        operation_type=(values.get("operation_type") or [""])[0],
                        limit=limit,
                    )
                elif parsed.path == "/history/open-tasks":
                    result = HISTORY_SERVICE.open_tasks(
                        actor_terminal_id=terminal_scope, limit=limit
                    )
                else:
                    self.send_json(404, {"ok": False, "error": "history endpoint not found"})
                    return
                self.send_json(200, result)
            except PermissionError as exc:
                self.send_json(403, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/memories/"):
            terminal_scope = str(self.headers.get("X-Hytera-Terminal-ID") or "").strip() or None
            tenant_scope = str(self.headers.get("X-Hytera-Tenant-ID") or "").strip() or config.CONTEXT_DEFAULT_TENANT_ID
            if not terminal_scope:
                self.send_json(403, {"ok": False, "error": "terminal scope required"})
                return
            memory_id = parsed.path[len("/memories/") :].strip()
            memory = MEMORY_REPOSITORY.get_authorized_public(
                memory_id, tenant_id=tenant_scope, actor_id=terminal_scope, terminal_id=terminal_scope
            )
            if memory is None:
                self.send_json(404, {"ok": False, "error": "memory not found"})
            else:
                self.send_json(200, {
                    "ok": True,
                    "memory": memory,
                    "transitions": MEMORY_REPOSITORY.transitions(
                        memory_id,
                        scope=RepositoryScope(tenant_scope or "default", terminal_scope, terminal_scope),
                    ),
                })
            return
        if parsed.path == "/memories":
            terminal_scope = str(self.headers.get("X-Hytera-Terminal-ID") or "").strip() or None
            tenant_scope = str(self.headers.get("X-Hytera-Tenant-ID") or "").strip() or config.CONTEXT_DEFAULT_TENANT_ID
            if not terminal_scope:
                self.send_json(403, {"ok": False, "error": "terminal scope required"})
                return
            try:
                values = parse_qs(parsed.query, keep_blank_values=False)
                statuses = tuple((values.get("status") or ["VERIFIED"]))
                memories = MEMORY_REPOSITORY.search(
                    tenant_id=tenant_scope, actor_id=terminal_scope, terminal_id=terminal_scope,
                    query=(values.get("query") or [None])[0],
                    statuses=statuses,
                    namespace=(values.get("namespace") or [None])[0],
                    subject_entity_id=(values.get("subject_entity_id") or [None])[0],
                    predicate=(values.get("predicate") or [None])[0],
                    limit=min(max(1, int((values.get("limit") or [30])[0])), config.CONTEXT_QUERY_MAX_LIMIT),
                    offset=max(0, int((values.get("offset") or [0])[0])),
                    use_fts=config.MEMORY_FTS_ENABLE,
                )
                self.send_json(200, {"ok": True, "memories": memories, "count": len(memories)})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/calls/metrics":
            allowed, terminal_scope = query_scope(self.headers)
            if not allowed:
                self.send_json(403, {"ok": False, "error": "terminal scope required"})
                return
            self.send_json(200, {"ok": True, "metrics": CALL_STORE_RUNTIME.metrics(), "terminal_scope": terminal_scope})
            return
        if parsed.path.startswith("/calls/"):
            allowed, terminal_scope = query_scope(self.headers)
            if not allowed:
                self.send_json(403, {"ok": False, "error": "terminal scope required"})
                return
            call_id = parsed.path[len("/calls/") :].strip()
            if not call_id:
                self.send_json(400, {"ok": False, "error": "call_id is required"})
                return
            call = CALL_STORE_RUNTIME.get_call(
                call_id, include_transcript=True, authorized_terminal_id=terminal_scope
            )
            if call is None:
                self.send_json(404, {"ok": False, "error": "call not found"})
                return
            if terminal_scope and terminal_scope not in {
                str(call.get("observed_source_id") or ""),
                str(call.get("observed_target_id") or ""),
                str(call.get("resolved_source_id") or ""),
                str(call.get("resolved_target_id") or ""),
            }:
                self.send_json(403, {"ok": False, "error": "call outside terminal scope"})
                return
            self.send_json(200, {"ok": True, "call": call})
            return
        if parsed.path == "/calls":
            try:
                allowed, terminal_scope = query_scope(self.headers)
                if not allowed:
                    self.send_json(403, {"ok": False, "error": "terminal scope required"})
                    return
                selector = parse_call_selector(
                    parsed.query,
                    max_limit=config.CALL_QUERY_MAX_LIMIT,
                    authorized_terminal_id=terminal_scope,
                )
                calls = CALL_STORE_RUNTIME.list_calls(selector)
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "calls": calls,
                        "count": len(calls),
                        "limit": selector.limit,
                        "offset": selector.offset,
                    },
                )
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/events/"):
            event_id = parsed.path[len("/events/") :].strip()
            if not event_id:
                self.send_json(400, {"ok": False, "error": "event_id is required"})
                return
            if config.STORAGE_BACKEND == "postgres":
                event_scope = RepositoryScope(
                    tenant_id=str(self.headers.get("X-Hytera-Tenant-ID") or "").strip() or config.CONTEXT_DEFAULT_TENANT_ID or "default",
                    actor_id=str(self.headers.get("X-Hytera-Actor-ID") or self.headers.get("X-Hytera-Terminal-ID") or "").strip() or None,
                    terminal_id=str(self.headers.get("X-Hytera-Terminal-ID") or "").strip() or None,
                )
                event = EVENT_PUBLISHER.get(event_id, scope=event_scope)
            else:
                event = EVENT_PUBLISHER.get(event_id)
            if event is None:
                self.send_json(404, {"ok": False, "error": "event not found"})
            else:
                self.send_json(200, {"ok": True, "event": event})
            return
        if parsed.path == "/events":
            try:
                values = parse_qs(parsed.query, keep_blank_values=False)
                requested_limit = int((values.get("limit") or [100])[0])
                limit = min(max(1, requested_limit), config.EVENT_QUERY_MAX_LIMIT)
                offset = max(0, int((values.get("offset") or [0])[0]))
                query = EventQuery(
                    event_id=(values.get("event_id") or [None])[0],
                    trace_id=(values.get("trace_id") or [None])[0],
                    correlation_id=(values.get("correlation_id") or [None])[0],
                    call_id=(values.get("call_id") or [None])[0],
                    event_type=(values.get("event_type") or [None])[0],
                    limit=limit,
                    offset=offset,
                )
                if config.STORAGE_BACKEND == "postgres":
                    event_scope = RepositoryScope(
                        tenant_id=str(self.headers.get("X-Hytera-Tenant-ID") or "").strip() or config.CONTEXT_DEFAULT_TENANT_ID or "default",
                        actor_id=str(self.headers.get("X-Hytera-Actor-ID") or self.headers.get("X-Hytera-Terminal-ID") or "").strip() or None,
                        terminal_id=str(self.headers.get("X-Hytera-Terminal-ID") or "").strip() or None,
                    )
                    events = EVENT_PUBLISHER.query(query, scope=event_scope)
                else:
                    events = EVENT_PUBLISHER.query(query)
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "events": events,
                        "count": len(events),
                        "limit": limit,
                        "offset": offset,
                    },
                )
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/voice/callback/spool":
            self.send_json(200, {"ok": True, "spool": CALLBACK_SPOOL.stats()})
            return
        if parsed.path == "/results/latest":
            self.send_json(200, {"ok": True, "results": get_latest_results()})
            return
        if parsed.path == "/business_slots":
            self.send_json(
                200,
                {
                    "ok": True,
                    "business_slots": get_business_slots(),
                    "capabilities": CAPABILITY_REGISTRY.planner_contracts(available_only=True),
                },
            )
            return
        if parsed.path == "/contact_map":
            self.send_json(
                200,
                {
                    "ok": True,
                    "index_base": 1,
                    "contact_map": get_contact_map(),
                    "contact_list": get_contact_list(),
                },
            )
            return
        if parsed.path == "/zone_channel_map":
            self.send_json(
                200,
                {
                    "ok": True,
                    "index_base": 1,
                    "zone_channel_map": get_zone_channel_map(),
                    "zone_channel_list": get_zone_channel_list(),
                },
            )
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    @traced_http_handler("voice-agent.http")
    def do_POST(self) -> None:
        request_t0 = time.perf_counter()
        parsed = urlparse(self.path)
        correlation_id = str(self.headers.get("X-Correlation-ID") or uuid.uuid4().hex)[:128]
        self._request_correlation_id = correlation_id
        auth_t0 = time.perf_counter()
        authorized = self.is_authorized()
        auth_ms = perf_ms(auth_t0)
        if parsed.path == "/voice/callback":
            log_event(
                logger,
                "语音回调请求开始",
                correlation_id=correlation_id,
                payload_bytes=int(self.headers.get("Content-Length", "0") or "0"),
                client_ip=self.client_address[0],
            )
        if not authorized:
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            if parsed.path == "/voice/callback":
                log_event(
                    logger,
                    "语音回调鉴权失败",
                    correlation_id=correlation_id,
                    auth_ms=auth_ms,
                    total_ms=perf_ms(request_t0),
                    exception_type="Unauthorized",
                )
            return
        if parsed.path == "/planning/waits/events":
            try:
                data = self.read_json()
                subject_type = str(data.get("subject_type") or "").strip()
                subject_id = str(data.get("subject_id") or "").strip()
                predicate = str(data.get("predicate") or "").strip()
                if subject_type not in {"call_id", "operation_id", "reminder_id"}:
                    raise ValueError("subject_type must be call_id, operation_id or reminder_id")
                if not subject_id or not predicate:
                    raise ValueError("subject_id and predicate are required")
                event = dict(data.get("event") or {})
                matched = PLANNING_RUNTIME.dispatch_wait_event(
                    subject_type=subject_type, subject_id=subject_id, predicate=predicate, event=event
                )
                resumed = []
                for item in matched:
                    payload = {
                        "schema_version": 1, "kind": "plan_wait",
                        "wait_id": item.get("wait_id"),
                        "subject_type": item.get("subject_type"),
                        "subject_id": item.get("subject_id"),
                        "predicate": item.get("predicate"),
                        "event": event,
                    }
                    resumed.append({
                        "wait_id": item.get("wait_id"),
                        "thread_id": item.get("thread_id"),
                        "result": VOICE_AGENT_PROCESSOR.resume_wait(str(item.get("thread_id")), payload),
                    })
                self.send_json(200, {"ok": True, "matched": matched, "resumed": resumed})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/approvals/") and any(parsed.path.endswith("/" + action) for action in ("approve", "edit", "reject")):
            if not self.is_approval_admin_authorized():
                self.send_json(403, {"ok": False, "error": "approval admin unauthorized"})
                return
            try:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) != 3 or parts[0] != "approvals":
                    raise ValueError("invalid approval path")
                approval_id, action = parts[1], parts[2]
                data = self.read_json()
                reviewer_id = str(self.headers.get("X-Approval-Actor-ID") or data.get("actor_id") or "").strip()
                reviewer_role = str(self.headers.get("X-Approval-Role") or data.get("actor_role") or "").strip()
                approval = PLANNING_RUNTIME.approval_service.decide(
                    approval_id, action=action, actor_id=reviewer_id, actor_role=reviewer_role,
                    reason=str(data.get("reason") or "") or None,
                    edited_payload=data.get("plan") if action == "edit" else None,
                )
                resume = VOICE_AGENT_PROCESSOR.resume_approval(
                    approval.thread_id, PLANNING_RUNTIME.approval_resume_payload(approval.approval_id)
                )
                self.send_json(200, {"ok": True, "approval": approval.model_dump(mode="json"), "resume_result": resume})
            except PermissionError as exc:
                self.send_json(403, {"ok": False, "error": str(exc)})
            except KeyError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path in {"/memories/review", "/memories/invalidate", "/memories/expire"}:
            if not self.is_memory_admin_authorized():
                self.send_json(403, {"ok": False, "error": "memory admin unauthorized"})
                return
            try:
                data = self.read_json()
                role = str(self.headers.get("X-Memory-Role") or data.get("actor_role") or "").strip()
                actor = str(self.headers.get("X-Memory-Actor-ID") or data.get("actor_id") or "").strip() or None
                tenant_scope = str(
                    self.headers.get("X-Hytera-Tenant-ID") or data.get("tenant_id") or config.CONTEXT_DEFAULT_TENANT_ID or "default"
                ).strip() or "default"
                terminal_scope = str(
                    self.headers.get("X-Hytera-Terminal-ID") or data.get("terminal_id") or ""
                ).strip() or None
                if config.STORAGE_BACKEND == "postgres" and not terminal_scope:
                    raise PermissionError("terminal scope required for PostgreSQL memory governance")
                repository_scope = RepositoryScope(tenant_scope, terminal_scope, terminal_scope)
                if parsed.path == "/memories/expire":
                    if role.lower() not in {"admin", "privacy_officer"}:
                        raise PermissionError("memory expiry requires admin or privacy_officer")
                    count = MEMORY_REPOSITORY.expire_due(
                        actor_id=actor or "system", scope=repository_scope
                    )
                    result = {"schema_version": 1, "expired": count}
                elif parsed.path == "/memories/invalidate":
                    result = MEMORY_VERIFIER.invalidate_evidence(
                        str(data.get("memory_id") or ""), actor_role=role, actor_id=actor,
                        reason=str(data.get("reason") or "evidence unavailable"),
                        scope=repository_scope,
                    )
                else:
                    result = MEMORY_VERIFIER.review(
                        str(data.get("memory_id") or ""),
                        decision=str(data.get("decision") or ""),
                        actor_role=role, actor_id=actor,
                        reason=str(data.get("reason") or "manual review"),
                        scope=repository_scope,
                    )
                self.send_json(200, {"ok": True, **result})
            except PermissionError as exc:
                self.send_json(403, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/voice/callback":
            self.handle_voice_callback(
                request_t0=request_t0,
                auth_ms=auth_ms,
                correlation_id=correlation_id,
            )
            return
        if parsed.path == "/test/callback":
            # 测试端点：用于诊断 POST 连接问题
            try:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_length) if content_length > 0 else b""
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "message": "POST test successful",
                        "content_length": content_length,
                        "body_size": len(raw_body),
                    },
                )
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/contact_map":
            try:
                data = self.read_json()
                contact_map = update_contact_map(data)
                slots = refresh_contact_slots()
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "index_base": 1,
                        "contact_map": contact_map,
                        "contact_list": get_contact_list(),
                        "business_slots": slots,
                    },
                )
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/zone_channel_map":
            try:
                data = self.read_json()
                zone_channel_map = update_zone_channel_map(data)
                slots = refresh_zone_channel_slots()
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "index_base": 1,
                        "zone_channel_map": zone_channel_map,
                        "zone_channel_list": get_zone_channel_list(),
                        "business_slots": slots,
                    },
                )
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/sms/callback":
            # 需求 §25/§35：短信回调入口；分类 medium=sms，缺省身份按直接入口补全
            # （source=模拟终端、target=LOCAL_CONTROL_ID）；user 短信只归档不规划。
            try:
                raw_data = self.read_json()
                text = str(raw_data.get("text") or raw_data.get("content") or "").strip()
                if not text:
                    raise ValueError("text is required")
                data = VoiceEvent.model_validate(raw_data).model_dump(exclude_none=True)
                data["text"] = text
                data["input_mode"] = "sms"
                data.setdefault("origin", "sms")
                data = ensure_voice_event_contract(data, generate_event_id=True)
                trace_context = extract_trace_context(self.headers, data)
                data["trace_id"] = trace_context.trace_id
                data["correlation_id"] = trace_context.correlation_id or str(data.get("correlation_id") or data["event_id"])
                data.setdefault("accepted_event_id", data["event_id"])
                request_id = next_voice_request_id()
                result = process_asr_task(
                    {
                        "task_id": request_id,
                        "request_id": request_id,
                        "received_time_ms": now_ms(),
                        "data": data,
                        "event_id": data["event_id"],
                        "correlation_id": data.get("correlation_id"),
                    }
                )
                if result is None:
                    self.send_json(500, {"ok": False, "error": "sms processing failed"})
                else:
                    self.send_json(
                        200,
                        {
                            "ok": True,
                            "input_mode": "sms",
                            "text": text,
                            "task_id": request_id,
                            "request_id": request_id,
                            "message": result.get("message"),
                            "result": result,
                        },
                    )
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/text/input":
            try:
                raw_data = self.read_json()
                text = str(raw_data.get("text") or raw_data.get("asr_text") or "").strip()
                if not text:
                    raise ValueError("text is required")
                data = VoiceEvent.model_validate(raw_data).model_dump(exclude_none=True)
                data["text"] = text
                data["input_mode"] = "text"
                data = ensure_voice_event_contract(data, generate_event_id=True)
                trace_context = extract_trace_context(self.headers, data)
                data["trace_id"] = trace_context.trace_id
                data["correlation_id"] = trace_context.correlation_id or str(data.get("correlation_id") or data["event_id"])
                for field_name in ("causation_id", "command_id", "operation_id", "call_id"):
                    value = getattr(trace_context, field_name)
                    if value:
                        data[field_name] = value
                data.setdefault("accepted_event_id", data["event_id"])
                request_id = next_voice_request_id()
                result = process_asr_task(
                    {
                        "task_id": request_id,
                        "request_id": request_id,
                        "received_time_ms": now_ms(),
                        "data": data,
                        "event_id": data["event_id"],
                        "correlation_id": data.get("correlation_id"),
                    }
                )
                if result is None:
                    self.send_json(500, {"ok": False, "error": "text processing failed"})
                else:
                    self.send_json(
                        200,
                        {
                            "ok": True,
                            "input_mode": "text",
                            "text": text,
                            "task_id": request_id,
                            "request_id": request_id,
                            "result": result,
                        },
                    )
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/plan/text":
            try:
                data = self.read_json()
                text = str(data.get("text") or data.get("asr_text") or "").strip()
                proposal = SEMANTIC_PLANNER.plan(text, context=data.get("context") or {})
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "text": text,
                        "proposal": proposal.model_dump(mode="json"),
                        "capability_catalog_hash": CAPABILITY_REGISTRY.catalog_hash,
                    },
                )
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def handle_voice_callback(
        self,
        *,
        request_t0: float | None = None,
        auth_ms: float = 0.0,
        correlation_id: str | None = None,
    ) -> None:
        """Authenticate, validate, deduplicate, durably spool and ACK quickly."""

        request_t0 = request_t0 if request_t0 is not None else time.perf_counter()
        ack_t0 = time.perf_counter()
        request_headers = getattr(self, "headers", {})
        correlation_id = correlation_id or str(
            request_headers.get("X-Correlation-ID") or uuid.uuid4().hex
        )[:128]
        self._request_correlation_id = correlation_id

        try:
            content_length = int(request_headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            self.send_json(400, {"ok": False, "error": callback_message("invalid_content_length")})
            return
        if content_length < 0 or content_length > config.CALLBACK_MAX_BODY_BYTES:
            self.send_json(
                413,
                {
                    "ok": False,
                    "queued": False,
                    "error": callback_message("body_too_large"),
                    "max_body_bytes": config.CALLBACK_MAX_BODY_BYTES,
                },
            )
            return

        body_t0 = time.perf_counter()
        raw_body = self.read_body()
        read_body_ms = perf_ms(body_t0)
        validate_t0 = time.perf_counter()
        try:
            raw_data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            validated = VoiceEvent.model_validate(raw_data).model_dump(exclude_none=True)
            data = ensure_voice_event_contract(
                validated, generate_event_id=True, deterministic_legacy_id=True
            )
            trace_context = extract_trace_context(request_headers, data)
            data["trace_id"] = trace_context.trace_id
            correlation_id = trace_context.correlation_id or correlation_id
            data["correlation_id"] = correlation_id
            for field_name in (
                "causation_id",
                "command_id",
                "operation_id",
                "call_id",
            ):
                value = getattr(trace_context, field_name)
                if value:
                    data[field_name] = value
            data.setdefault("accepted_event_id", new_event_id())
        except Exception as exc:
            json_validate_ms = perf_ms(validate_t0)
            send_t0 = time.perf_counter()
            self.send_json(400, {"ok": False, "queued": False, "error": callback_message("invalid_voice_event", error=exc)})
            log_event(
                logger,
                "语音回调分段诊断",
                correlation_id=correlation_id,
                payload_bytes=len(raw_body),
                auth_ms=auth_ms,
                read_body_ms=read_body_ms,
                json_validate_ms=json_validate_ms,
                persist_ms=0.0,
                enqueue_ms=0.0,
                ack_write_ms=perf_ms(send_t0),
                total_ms=perf_ms(request_t0),
                queued=False,
                duplicate=False,
                exception_type=type(exc).__name__,
            )
            return
        json_validate_ms = perf_ms(validate_t0)
        event_id = str(data["event_id"])
        request_id = next_voice_request_id()
        received_time_ms = now_ms()
        inc_stat("received_total")

        existing = CALLBACK_SPOOL.get(event_id)
        if (
            existing is None
            and ASR_QUEUE.full()
            and not config.CANCEL_QUEUED_TASKS_ON_NEW_TASK
        ):
            inc_stat("backpressure_total")
            self.send_json(
                503,
                {
                    "ok": False,
                    "event_id": event_id,
                    "queued": False,
                    "duplicate": False,
                    "dropped_reason": callback_message("queue_full"),
                    "queue_depth": ASR_QUEUE.qsize(),
                    "retryable": True,
                },
            )
            return

        if config.VOICE_INGRESS_ASYNC_PERSIST:
            # ACK 前移：背压检查通过后立即回 ACK，把 spool 持久化、
            # shadow 写入、ASR 入队与事件归档交给后台 FIFO worker。
            # 同步回退开关：VOICE_INGRESS_ASYNC_PERSIST=false。
            _ensure_ingress_worker()
            ack_ms = perf_ms(ack_t0)
            queue_depth = ASR_QUEUE.qsize()
            ingress_timing = {
                "auth_ms": round(float(auth_ms or 0.0), 3),
                "read_body_ms": round(float(read_body_ms or 0.0), 3),
                "json_validate_ms": round(float(json_validate_ms or 0.0), 3),
                "persist_ms": None,
                "enqueue_ms": None,
                "ack_write_ms": None,
                "persist_async": True,
            }
            send_t0 = time.perf_counter()
            self.send_json(
                config.CALLBACK_ACK_STATUS_CODE,
                {
                    "ok": True,
                    "message": callback_message("accepted"),
                    "event_id": event_id,
                    "task_id": request_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "queued": True,
                    "duplicate": False,
                    "queue_depth": queue_depth,
                    "queue_size": queue_depth,
                    "persist_async": True,
                    "ack_ms": ack_ms,
                    "timing_ms": {"agent_ingress_ms": ingress_timing},
                },
            )
            ack_write_ms = perf_ms(send_t0)
            ingress_timing["ack_write_ms"] = ack_write_ms
            ingress_timing["total_ms"] = round(
                sum(
                    float(ingress_timing.get(k) or 0.0)
                    for k in ("auth_ms", "read_body_ms", "json_validate_ms", "ack_write_ms")
                ),
                3,
            )
            ack_info = {
                "event_id": event_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "queued": True,
                "queue_inserted": None,
                "duplicate": False,
                "queue_depth": queue_depth,
                "ack_ms": ack_ms,
                "persist_async": True,
                "phase_ms": {
                    "auth_ms": auth_ms,
                    "read_body_ms": read_body_ms,
                    "json_validate_ms": json_validate_ms,
                    "persist_ms": None,
                    "enqueue_ms": None,
                },
                "payload_bytes": len(raw_body),
                "received_time_ms": received_time_ms,
                "source_id": data.get("source_id"),
                "session_id": data.get("session_id"),
            }
            update_stats(last_ack=ack_info)
            write_jsonl_async(config.RECEIVED_JSONL, ack_info)
            _INGRESS_WORK_QUEUE.put(
                {
                    "event_id": event_id,
                    "data": data,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "received_time_ms": received_time_ms,
                    # ACK 发出时刻：后台链真实入队时刻在 _voice_ingress_background_persist
                    # 内补打（queued_time_ms），二者差值即 persist_to_enqueue_ms。
                    "ack_epoch_ms": now_ms(),
                    "agent_ingress_timing_ms": ingress_timing,
                }
            )
            log_event(
                logger,
                "语音回调ACK前移",
                correlation_id=correlation_id,
                event_id=event_id,
                request_id=request_id,
                ack_ms=ack_ms,
                total_ms=perf_ms(request_t0),
                queue_depth=queue_depth,
            )
            return

        persist_t0 = time.perf_counter()
        try:
            accepted = CALLBACK_SPOOL.accept(
                data, request_id=request_id, queue_depth=ASR_QUEUE.qsize()
            )
        except (CallbackPayloadInvalid, CallbackPayloadTooLarge, CallbackSpoolCapacityError) as exc:
            status_code = int(getattr(exc, "status_code", 503))
            inc_stat("backpressure_total" if status_code >= 500 else "dropped_total")
            self.send_json(
                status_code,
                {
                    "ok": False,
                    "event_id": event_id,
                    "queued": False,
                    "duplicate": False,
                    "dropped_reason": getattr(exc, "reason", "spool_error"),
                    "error": str(exc),
                    "queue_depth": ASR_QUEUE.qsize(),
                    "retryable": status_code in {429, 503},
                },
            )
            return
        persist_ms = perf_ms(persist_t0)
        if OPERATION_RUNTIME.enabled:
            try:
                OPERATION_RUNTIME.create_voice_command(
                    {
                        "task_id": request_id,
                        "request_id": request_id,
                        "spool_event_id": accepted.event_id,
                        "data": data,
                    },
                    None,
                )
            except Exception as exc:
                log_event(
                    logger,
                    "Command shadow write failed",
                    event_id=accepted.event_id,
                    error_type=type(exc).__name__,
                )

        enqueue_t0 = time.perf_counter()
        queued_time_ms = now_ms()
        agent_ingress_timing_ms = {
            "auth_ms": round(float(auth_ms or 0.0), 3),
            "read_body_ms": round(float(read_body_ms or 0.0), 3),
            "json_validate_ms": round(float(json_validate_ms or 0.0), 3),
            "persist_ms": round(float(persist_ms or 0.0), 3),
            "enqueue_ms": 0.0,
            "ack_write_ms": None,
        }
        queue_inserted = False
        cancelled_count = 0
        if not accepted.duplicate:
            try:
                cancelled_count, queue_inserted = _enqueue_latest_asr_task(
                    {
                        "spool_event_id": accepted.event_id,
                        "received_time_ms": received_time_ms,
                        "queued_time_ms": queued_time_ms,
                        "agent_ingress_timing_ms": agent_ingress_timing_ms,
                    },
                    event_id=accepted.event_id,
                )
            except queue.Full:
                # The durable RECEIVED record remains recoverable by the feeder.
                queue_inserted = False
            inc_stat("queued_total")
        else:
            inc_stat("duplicate_total")
        enqueue_ms = perf_ms(enqueue_t0)
        agent_ingress_timing_ms["enqueue_ms"] = enqueue_ms
        agent_ingress_timing_ms["total_ms"] = round(
            sum(float(agent_ingress_timing_ms.get(k) or 0.0) for k in ("auth_ms", "read_body_ms", "json_validate_ms", "persist_ms", "enqueue_ms")), 3
        )
        queue_depth = ASR_QUEUE.qsize()
        ack_ms = perf_ms(ack_t0)
        ack_info = {
            "event_id": accepted.event_id,
            "request_id": accepted.request_id,
            "correlation_id": correlation_id,
            "queued": not accepted.duplicate,
            "queue_inserted": queue_inserted,
            "duplicate": accepted.duplicate,
            "queue_depth": queue_depth,
            "cancelled_queue_tasks": cancelled_count,
            "ack_ms": ack_ms,
            "phase_ms": {
                "auth_ms": auth_ms,
                "read_body_ms": read_body_ms,
                "json_validate_ms": json_validate_ms,
                "persist_ms": persist_ms,
                "enqueue_ms": enqueue_ms,
            },
            "payload_bytes": accepted.payload_bytes,
            "payload_hash": accepted.payload_hash,
            "received_time_ms": received_time_ms,
            "source_id": data.get("source_id"),
            "session_id": data.get("session_id"),
        }
        update_stats(last_ack=ack_info)
        write_jsonl_async(config.RECEIVED_JSONL, ack_info)
        send_t0 = time.perf_counter()
        self.send_json(
            config.CALLBACK_ACK_STATUS_CODE,
            {
                "ok": True,
                "message": callback_message("duplicate" if accepted.duplicate else "accepted"),
                "event_id": accepted.event_id,
                "task_id": accepted.request_id,
                "request_id": accepted.request_id,
                "correlation_id": correlation_id,
                "queued": not accepted.duplicate,
                "duplicate": accepted.duplicate,
                "queue_depth": queue_depth,
                "queue_size": queue_depth,
                "cancelled_queue_tasks": cancelled_count,
                "ack_ms": ack_ms,
                "timing_ms": {"agent_ingress_ms": dict(agent_ingress_timing_ms)},
            },
        )
        ack_write_ms = perf_ms(send_t0)
        agent_ingress_timing_ms["ack_write_ms"] = ack_write_ms
        agent_ingress_timing_ms["total_ms"] = round(float(agent_ingress_timing_ms.get("total_ms") or 0.0) + ack_write_ms, 3)
        try:
            existing_accepted_id = (
                existing.metadata.get("accepted_event_id") if existing is not None else None
            )
            fact_type = (
                VOICE_CALLBACK_DUPLICATE if accepted.duplicate else VOICE_CALLBACK_ACCEPTED
            )
            fact_event_id = (
                new_event_id()
                if accepted.duplicate
                else str(data.get("accepted_event_id") or new_event_id())
            )
            fact = DomainEvent.from_voice_event(
                data,
                event_type=fact_type,
                source_service="voice-agent",
                event_id=fact_event_id,
                causation_id=(
                    str(existing_accepted_id)
                    if accepted.duplicate and existing_accepted_id
                    else accepted.event_id
                ),
                data={
                    "voice_event_id": accepted.event_id,
                    "request_id": accepted.request_id,
                    "duplicate": accepted.duplicate,
                    "queued": not accepted.duplicate,
                    "queue_inserted": queue_inserted,
                    "payload_bytes": accepted.payload_bytes,
                    "payload_hash": accepted.payload_hash,
                    "observed_source_id": data.get("source_id"),
                    "observed_target_id": data.get("target_id"),
                    "group_id": data.get("group_id"),
                    "channel_id": data.get("channel_id"),
                    "call_type": data.get("call_type"),
                    "direction": data.get("direction") or "inbound",
                    "capture_session_id": data.get("session_id"),
                    "audio_path": str(accepted.spool_path),
                    "evidence_event_ids": list(data.get("evidence_event_ids") or []),
                    "context_confidence": data.get("context_confidence"),
                },
            )
            EVENT_PUBLISHER.publish(fact)
            CALL_STORE_RUNTIME.consume(fact)
        except Exception:
            if config.EVENT_JOURNAL_FAIL_MODE == "closed":
                raise
            logger.exception("callback_event_publish_failed event_id=%s", accepted.event_id)
        log_event(
            logger,
            "语音回调分段诊断",
            correlation_id=correlation_id,
            event_id=accepted.event_id,
            request_id=accepted.request_id,
            payload_bytes=accepted.payload_bytes,
            auth_ms=auth_ms,
            read_body_ms=read_body_ms,
            json_validate_ms=json_validate_ms,
            persist_ms=persist_ms,
            enqueue_ms=enqueue_ms,
            ack_write_ms=ack_write_ms,
            total_ms=perf_ms(request_t0),
            queued=not accepted.duplicate,
            duplicate=accepted.duplicate,
            queue_depth=queue_depth,
            exception_type=None,
        )

