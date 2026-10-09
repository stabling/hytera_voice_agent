# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/lifecycle.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：..capture, ..capture.io_utils, shared_contracts.event_models, shared_contracts.event_types, shared_contracts.models, .circuit_breaker, .models, .outbox
# 主要公开符号：CallbackLifecycleMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..capture import TSharkVoiceCaptureManager
from ..capture.io_utils import now_iso
from shared_contracts.event_models import DomainEvent
from shared_contracts.event_types import VOICE_RECEIVED
from shared_contracts.models import ensure_voice_event_contract
from shared_runtime.latency import iso_to_epoch_ms, log_latency_tree
from .circuit_breaker import CircuitBreaker
from .models import VoiceCallbackSettings
from .outbox import CallbackOutbox, OutboxCapacityError
from .retry_policy import RetryPolicy

logger = logging.getLogger("hytera_adk_gateway.callbacks")


class CallbackLifecycleMixin:
    """语音回调内部能力分组。"""

    def __init__(
        self,
        capture_manager: TSharkVoiceCaptureManager,
        *,
        get_context: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.capture_manager = capture_manager
        self.get_context = get_context or (lambda: {})
        # Keep a stable base for relative runtime storage paths.  The ADK loader
        # changes ``os.getcwd()`` process-wide while vendor DLLs are active.
        self._storage_base_dir = Path.cwd().resolve()
        self._settings = VoiceCallbackSettings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._jsonl_path: Path | None = None
        self._jsonl_pos: int = 0
        self._total_sent: int = 0
        self._total_failed: int = 0
        self._last_tick_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._last_error: str | None = None
        self._last_status_code: int | None = None
        self._last_payload_preview: dict[str, Any] | None = None
        self._merge_buffer: dict[str, Any] | None = None
        self._last_read_count: int = 0
        self._last_audio_item_count: int = 0
        self._last_ignored_count: int = 0
        self._last_flush_reason: str | None = None
        self._audio_raw_path: Path | None = None
        self._audio_raw_pos: int = 0
        self._last_raw_fallback_bytes: int = 0
        self._pending_callbacks: deque[dict[str, Any]] = deque()
        self._capture_generation = capture_manager.packet_generation()
        self._outbox: CallbackOutbox | None = None
        self._sender_threads: list[threading.Thread] = []
        self._sender_wakeup = threading.Event()
        self._retry_policy = RetryPolicy()
        self._circuit_breaker = CircuitBreaker()
        self._last_outbox_error: str | None = None
        self._outbox_enqueue_meta: dict[str, dict[str, float]] = {}
        self._event_publisher = None
        try:
            from shared_runtime.event_publisher import build_event_publisher

            self._event_publisher = build_event_publisher(
                source_service="adk-gateway",
                default_path=self._storage_base_dir / "data" / "context" / "context_journal.sqlite3",
            )
        except Exception as exc:
            logger.warning("gateway event publisher unavailable: %s: %s", type(exc).__name__, exc)
        self._failure_episode_started_at: float | None = None
        self._failure_log_last_at: float | None = None
        self._failure_log_category: str | None = None
        self._failure_episode_retries: int = 0
        self._configure_reliable_delivery(self._settings.normalized())

    def configure(self, settings: VoiceCallbackSettings) -> dict[str, Any]:
        """更新回调配置。线程安全，可在服务运行中调用。"""
        normalized = settings.normalized()
        self._configure_reliable_delivery(normalized)
        with self._lock:
            self._settings = normalized
        logger.info(
            "语音回调配置已更新: enabled=%s url=%s interval=%ss",
            normalized.enabled,
            normalized.callback_url,
            normalized.interval_seconds,
        )
        return self.status()

    def update(
        self,
        *,
        enabled: bool | None = None,
        callback_url: str | None = None,
        interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
        max_packets_per_tick: int | None = None,
    ) -> dict[str, Any]:
        """按需更新部分配置。"""
        with self._lock:
            current = self._settings
            settings = VoiceCallbackSettings(
                enabled=current.enabled if enabled is None else enabled,
                callback_url=current.callback_url if callback_url is None else callback_url,
                interval_seconds=current.interval_seconds
                if interval_seconds is None
                else interval_seconds,
                timeout_seconds=current.timeout_seconds
                if timeout_seconds is None
                else timeout_seconds,
                max_packets_per_tick=current.max_packets_per_tick
                if max_packets_per_tick is None
                else max_packets_per_tick,
                merge_continuous_audio=current.merge_continuous_audio,
                merge_gap_seconds=current.merge_gap_seconds,
                merge_max_packets=current.merge_max_packets,
                merge_max_audio_bytes=current.merge_max_audio_bytes,
                merge_flush_on_idle_tick=current.merge_flush_on_idle_tick,
                merge_flush_on_batch_end=current.merge_flush_on_batch_end,
                fallback_read_audio_raw=current.fallback_read_audio_raw,
                delivery_mode=current.delivery_mode,
                outbox_path=current.outbox_path,
                outbox_max_events=current.outbox_max_events,
                outbox_max_bytes=current.outbox_max_bytes,
                outbox_retention_seconds=current.outbox_retention_seconds,
                outbox_lease_seconds=current.outbox_lease_seconds,
                sender_workers=current.sender_workers,
                sender_poll_seconds=current.sender_poll_seconds,
                retry_max_attempts=current.retry_max_attempts,
                circuit_failure_threshold=current.circuit_failure_threshold,
                circuit_reset_seconds=current.circuit_reset_seconds,
            )
        return self.configure(settings)

    def start(self) -> None:
        """启动后台回调线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            settings = self._settings
            # Outbox mode must start even while the agent is unavailable. A synchronous
            # reachability probe is retained only for the rollback-safe legacy path.
            if (
                settings.delivery_mode == "legacy"
                and settings.enabled
                and settings.callback_url
            ):
                self._test_callback_reachability(settings.callback_url)
            self._stop.clear()
            self._sender_wakeup.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="hytera-voice-callback-pusher",
                daemon=True,
            )
            self._thread.start()
            self._start_sender_threads_locked(settings)
        logger.info("语音回调后台线程已启动")

    def _test_callback_reachability(self, url: str) -> None:
        """启动时测试回调地址是否可达。"""
        try:
            parsed = urlparse(url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 80
            # 测试 TCP 连接
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                logger.info("回调地址连通性测试成功: %s (TCP %s:%d 可达)", url, host, port)
            else:
                logger.error(
                    "回调地址连通性测试失败: %s (TCP %s:%d 不可达, errno=%d)",
                    url,
                    host,
                    port,
                    result,
                )
                logger.error("请确认语音 ASR 服务已启动并监听端口 %d", port)
                # 尝试 netstat 诊断
                try:
                    netstat_output = subprocess.check_output(
                        ["netstat", "-ano"],
                        text=True,
                        timeout=5.0,
                        stderr=subprocess.DEVNULL,
                    )
                    for line in netstat_output.splitlines():
                        if f":{port}" in line and "LISTENING" in line:
                            logger.info("netstat 发现端口 %d: %s", port, line.strip())
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("回调地址连通性测试异常: %s - %s", url, exc)

    def _diagnose_connection_refused(self, url: str) -> None:
        """连接被拒绝时的深度诊断。"""
        try:
            parsed = urlparse(url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 80

            # 1. 测试 IPv4 连接
            try:
                sock4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock4.settimeout(2.0)
                result4 = sock4.connect_ex((host, port))
                sock4.close()
                logger.error(
                    "诊断 IPv4 %s:%d -> errno=%d (%s)",
                    host,
                    port,
                    result4,
                    "成功" if result4 == 0 else "失败",
                )
            except Exception as e:
                logger.error("诊断 IPv4 异常: %s", e)

            # 2. 测试 IPv6 连接 (如果是 localhost)
            if host in ("localhost", "127.0.0.1"):
                try:
                    sock6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    sock6.settimeout(2.0)
                    result6 = sock6.connect_ex(("::1", port))
                    sock6.close()
                    logger.error(
                        "诊断 IPv6 [::1]:%d -> errno=%d (%s)",
                        port,
                        result6,
                        "成功" if result6 == 0 else "失败",
                    )
                except Exception as e:
                    logger.error("诊断 IPv6 异常: %s", e)

            # 3. 检查端口监听状态
            try:
                output = subprocess.check_output(
                    ["netstat", "-ano"],
                    text=True,
                    timeout=5.0,
                    stderr=subprocess.DEVNULL,
                )
                found = False
                for line in output.splitlines():
                    if f":{port}" in line:
                        logger.error("netstat 端口 %d: %s", port, line.strip())
                        found = True
                if not found:
                    logger.error("netstat 未发现端口 %d 的任何监听", port)
            except Exception as e:
                logger.error("netstat 诊断失败: %s", e)

        except Exception as exc:
            logger.error("诊断异常: %s", exc)

    def stop(self) -> None:
        """停止后台回调线程。"""
        self._stop.set()
        self._sender_wakeup.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        for sender in list(self._sender_threads):
            sender.join(timeout=3.0)
        with self._lock:
            self._thread = None
            self._sender_threads = []
        logger.info("语音回调后台线程已停止")

    def status(self) -> dict[str, Any]:
        """返回回调线程与发送统计。"""
        with self._lock:
            settings = self._settings.as_dict()
            jsonl_path = str(self._jsonl_path) if self._jsonl_path else None
            jsonl_pos = self._jsonl_pos
            total_sent = self._total_sent
            total_failed = self._total_failed
            last_tick_at = self._last_tick_at
            last_success_at = self._last_success_at
            last_error_at = self._last_error_at
            last_error = self._last_error
            last_status_code = self._last_status_code
            last_payload_preview = self._last_payload_preview
            pending_merge = self._merge_preview_locked()
            last_read_count = self._last_read_count
            last_audio_item_count = self._last_audio_item_count
            last_ignored_count = self._last_ignored_count
            last_flush_reason = self._last_flush_reason
            audio_raw_path = str(self._audio_raw_path) if self._audio_raw_path else None
            audio_raw_pos = self._audio_raw_pos
            last_raw_fallback_bytes = self._last_raw_fallback_bytes
            pending_callback_count = len(self._pending_callbacks)
            alive = self._thread is not None and self._thread.is_alive()
            outbox = self._outbox
            last_outbox_error = self._last_outbox_error
        outbox_stats = outbox.stats() if outbox is not None else None
        circuit = self._circuit_breaker.snapshot().as_dict()
        return {
            "running": alive,
            "settings": settings,
            "cursor": {
                "jsonl_path": jsonl_path,
                "jsonl_pos": jsonl_pos,
                "audio_raw_path": audio_raw_path,
                "audio_raw_pos": audio_raw_pos,
            },
            "stats": {
                "total_sent": total_sent,
                "total_failed": total_failed,
                "last_tick_at": last_tick_at,
                "last_success_at": last_success_at,
                "last_error_at": last_error_at,
                "last_status_code": last_status_code,
                "last_error": last_error,
            },
            "reader": {
                "last_read_count": last_read_count,
                "last_audio_item_count": last_audio_item_count,
                "last_ignored_count": last_ignored_count,
                "last_flush_reason": last_flush_reason,
                "last_raw_fallback_bytes": last_raw_fallback_bytes,
                "pending_callback_count": pending_callback_count,
            },
            "pending_merge": pending_merge,
            "last_payload_preview": last_payload_preview,
            "outbox": outbox_stats,
            "circuit_breaker": circuit,
            "last_outbox_error": last_outbox_error,
        }

    def dead_letters(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return redacted DEAD_LETTER metadata for operational recovery."""

        outbox = self._outbox
        if outbox is None:
            return []
        return [
            {
                "event_id": record.event_id,
                "terminal_id": record.terminal_id,
                "session_id": record.session_id,
                "created_at": record.created_at,
                "payload_hash": record.payload_hash,
                "payload_bytes": record.payload_bytes,
                "attempts": record.attempts,
                "last_error": record.last_error,
            }
            for record in outbox.list_dead_letters(limit=limit)
        ]

    def _loop(self) -> None:
        generation = self._capture_generation
        while not self._stop.is_set():
            with self._lock:
                settings = self._settings
                self._last_tick_at = now_iso()
            try:
                if settings.enabled and settings.callback_url:
                    self._tick(settings)
            except Exception as exc:
                self._remember_error(f"回调轮询异常: {type(exc).__name__}: {exc}")

            # 静音确认加速：当存在等待静音确认的合并缓存时，缩短空闲等待，
            # 使 merge_gap_seconds 到期后 ≤0.2s 内即可 flush，而不是再等一个
            # 完整的 interval_seconds（默认 2s）轮询周期。
            # 静音确认总时长目标 <1s：tick 粒度上限取 min(merge_gap, 0.2)，
            # 即最坏 flush = gap + 0.2s（默认 0.4+0.2=0.6s）。有新包时
            # wait_for_packet 会提前返回，缩短超时不影响采包吞吐。
            wait_timeout = max(0.2, settings.interval_seconds)
            if self._has_pending_merge():
                fine_tick = max(0.05, min(settings.merge_gap_seconds, 0.2))
                wait_timeout = min(wait_timeout, fine_tick)
            generation = self.capture_manager.wait_for_packet(
                generation,
                wait_timeout,
            )
        self._capture_generation = generation

    def _tick(self, settings: VoiceCallbackSettings) -> None:
        if settings.delivery_mode == "legacy" and not self._flush_pending_callbacks(settings):
            return

        items = self._read_new_audio_items(max_items=settings.max_packets_per_tick)

        # 如果 JSONL 已经成功读到音频包，说明主链路正常。此时把 raw 兜底文件游标
        # 同步到文件末尾，避免同一批音频又被 audio_payload_raw.bin 兜底逻辑重复发送。
        if items:
            self._sync_audio_raw_cursor_to_end()

        # 兜底：只有在 JSONL 本轮没有音频、且当前没有正在等待合并的语音段时，
        # 才读取 audio_payload_raw.bin。否则会把同一段语音拆成“JSONL 回调 + raw 回调”。
        if not items and settings.fallback_read_audio_raw and not self._has_pending_merge():
            raw_item = self._read_new_audio_raw_item()
            if raw_item is not None:
                items = [raw_item]

        if settings.merge_continuous_audio:
            self._handle_merged_tick(settings, items)
            return

        if not items:
            return
        payloads = [self._build_callback_payload(detail) for detail in items]
        self._deliver_or_queue(payloads, settings)

    def _handle_merged_tick(
        self, settings: VoiceCallbackSettings, items: list[dict[str, Any]]
    ) -> None:
        """把同一条连续 RTP 音频流合并成一个回调。

        连续判断规则：
        - 源/目的 IP、端口、SSRC、payload_type 一致；
        - 并且 RTP sequence 连续，或相邻包的捕获时间间隔不超过 merge_gap_seconds。

        现场联调优先保证“能拿到数据”：
        - 如果本轮没有新的音频包，但缓存里已有上一段音频，则立即 flush；
        - 如果有新包，则按连续性合并；
        - 达到静音/断流阈值、包数上限或字节上限时也会 flush。
        """
        if not items:
            # 没有新音频时，不再 force=True 立即发送，而是等 merge_gap_seconds
            # 确认这次语音确实结束后再发送。这样一次连续语音只会产生一次回调。
            if settings.merge_flush_on_idle_tick:
                idle_payload = self._pop_stale_merge_payload(
                    settings, force=False, reason="idle_gap"
                )
                if idle_payload is not None:
                    self._deliver_or_queue([idle_payload], settings)
            return

        ready_payloads: list[dict[str, Any]] = []
        for detail in items:
            buffered_payload = self._push_merge_item(detail, settings)
            if buffered_payload is not None:
                ready_payloads.append(buffered_payload)

        # 正常情况下不要在 batch_end 发送；等待后续轮询确认静音/断流。
        # 只有显式打开 merge_flush_on_batch_end 时才保留旧行为。
        if settings.merge_flush_on_batch_end:
            batch_payload = self._pop_stale_merge_payload(settings, force=True, reason="batch_end")
            if batch_payload is not None:
                ready_payloads.append(batch_payload)
        else:
            stale_payload = self._pop_stale_merge_payload(
                settings, force=False, reason="stale_after_items"
            )
            if stale_payload is not None:
                ready_payloads.append(stale_payload)

        self._deliver_or_queue(ready_payloads, settings)

    def _deliver_or_queue(
        self,
        payloads: list[dict[str, Any]],
        settings: VoiceCallbackSettings,
    ) -> bool:
        """Persist new callbacks before delivery; retain legacy rollback behavior."""

        if not payloads:
            return True
        payloads = [ensure_voice_event_contract(payload, generate_event_id=True) for payload in payloads]
        if settings.delivery_mode == "legacy":
            url = settings.callback_url or ""
            for index, payload in enumerate(payloads):
                self._publish_gateway_voice_event(payload)
                if self._post_json(url, payload, timeout=settings.timeout_seconds):
                    continue
                with self._lock:
                    self._pending_callbacks.extend(payloads[index:])
                return False
            return True

        outbox = self._outbox
        if outbox is None:
            self._remember_error("callback outbox is not configured")
            return False
        accepted = True
        for payload in payloads:
            try:
                enqueued_at_ms = time.time() * 1000.0
                enqueue_started = time.perf_counter()
                record, inserted = outbox.enqueue(payload)
                outbox_enqueue_ms = round((time.perf_counter() - enqueue_started) * 1000.0, 3)
                self._outbox_enqueue_meta[record.event_id] = {
                    "enqueued_at_ms": enqueued_at_ms, "outbox_enqueue_ms": outbox_enqueue_ms
                }
                if inserted:
                    self._publish_gateway_voice_event(record.payload)
                    logger.info(
                        "voice_callback_outbox event_id=%s terminal_id=%s state=PENDING "
                        "payload_bytes=%d outbox_enqueue_ms=%.3f",
                        record.event_id,
                        record.terminal_id,
                        record.payload_bytes,
                        outbox_enqueue_ms,
                    )
                with self._lock:
                    self._last_payload_preview = self._preview_payload(record.payload)
                    self._last_outbox_error = None
            except OutboxCapacityError as exc:
                accepted = False
                with self._lock:
                    self._last_outbox_error = str(exc)
                self._remember_error(str(exc))
                logger.error(
                    "voice_callback_backpressure reason=outbox_full event_count=%d",
                    len(payloads),
                )
                break
        self._sender_wakeup.set()
        return accepted


    def _publish_gateway_voice_event(self, payload: dict[str, Any]) -> None:
        publisher = self._event_publisher
        if publisher is None:
            return
        try:
            publisher.publish(
                DomainEvent.from_voice_event(
                    payload,
                    event_type=VOICE_RECEIVED,
                    source_service="adk-gateway",
                    event_id=str(payload.get("event_id")),
                    causation_id=payload.get("causation_id"),
                    data={
                        "terminal_id": payload.get("terminal_id") or payload.get("source_id"),
                        "session_id": payload.get("session_id"),
                        "channel_id": payload.get("channel_id"),
                        "payload_bytes": payload.get("payload_bytes"),
                        "payload_hash": payload.get("payload_hash"),
                        "event_type": payload.get("event_type"),
                    },
                )
            )
        except Exception as exc:
            logger.warning(
                "gateway event publish failed event_id=%s error=%s: %s",
                payload.get("event_id"),
                type(exc).__name__,
                exc,
            )

    def _flush_pending_callbacks(self, settings: VoiceCallbackSettings) -> bool:
        """Legacy deque retry path retained behind VOICE_CALLBACK_DELIVERY_MODE."""

        url = settings.callback_url or ""
        while True:
            with self._lock:
                if not self._pending_callbacks:
                    return True
                payload = self._pending_callbacks[0]
            if not self._post_json(url, payload, timeout=settings.timeout_seconds):
                return False
            with self._lock:
                self._pending_callbacks.popleft()

    def _resolve_outbox_path(self, configured_path: str | Path) -> Path:
        candidate = Path(configured_path).expanduser()
        if not candidate.is_absolute():
            candidate = self._storage_base_dir / candidate
        return candidate.resolve(strict=False)

    def _configure_reliable_delivery(self, settings: VoiceCallbackSettings) -> None:
        if settings.delivery_mode == "outbox":
            current = self._outbox
            desired = self._resolve_outbox_path(settings.outbox_path)
            if current is None or current.path != desired:
                self._outbox = CallbackOutbox(
                    desired,
                    max_events=settings.outbox_max_events,
                    max_bytes=settings.outbox_max_bytes,
                    retention_seconds=settings.outbox_retention_seconds,
                    lease_seconds=settings.outbox_lease_seconds,
                )
            self._retry_policy = RetryPolicy(max_attempts=settings.retry_max_attempts)
            self._circuit_breaker = CircuitBreaker(
                failure_threshold=settings.circuit_failure_threshold,
                reset_seconds=settings.circuit_reset_seconds,
            )

    def _start_sender_threads_locked(self, settings: VoiceCallbackSettings) -> None:
        self._sender_threads = []
        if settings.delivery_mode != "outbox":
            return
        for index in range(settings.sender_workers):
            thread = threading.Thread(
                target=self._sender_loop,
                args=(index + 1,),
                name=f"hytera-callback-outbox-sender-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._sender_threads.append(thread)

    def _sender_loop(self, worker_id: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                settings = self._settings
                outbox = self._outbox
            if settings.delivery_mode != "outbox" or outbox is None or not settings.callback_url:
                self._sender_wakeup.wait(settings.sender_poll_seconds)
                self._sender_wakeup.clear()
                continue
            if not self._circuit_breaker.allow_request():
                self._sender_wakeup.wait(min(settings.sender_poll_seconds, 1.0))
                self._sender_wakeup.clear()
                continue
            records = outbox.lease_due(limit=1)
            if not records:
                self._circuit_breaker.release_probe()
                self._sender_wakeup.wait(settings.sender_poll_seconds)
                self._sender_wakeup.clear()
                continue
            record = records[0]
            send_started_ms = time.time() * 1000.0
            enqueue_meta = self._outbox_enqueue_meta.get(record.event_id, {})
            enqueued_at_ms = enqueue_meta.get("enqueued_at_ms")
            if enqueued_at_ms is None:
                enqueued_at_ms = iso_to_epoch_ms(record.created_at) or send_started_ms
            outbox_queue_wait_ms = round(max(0.0, send_started_ms - float(enqueued_at_ms)), 3)
            result = self._post_json_once(
                settings.callback_url,
                record.payload,
                timeout=settings.timeout_seconds,
                attempt=record.attempts,
            )
            if result.ok:
                outbox.mark_acked(record.event_id)
                self._circuit_breaker.record_success()
                with self._lock:
                    self._total_sent += 1
                    self._last_success_at = now_iso()
                    self._last_status_code = result.status_code
                    self._last_error = None
                    self._last_payload_preview = self._preview_payload(record.payload)
                    episode_started = self._failure_episode_started_at
                    episode_retries = self._failure_episode_retries
                    self._failure_episode_started_at = None
                    self._failure_log_last_at = None
                    self._failure_log_category = None
                    self._failure_episode_retries = 0
                if episode_started is not None:
                    logger.info(
                        "voice_callback_recovered duration_seconds=%.3f retries=%d backlog=%d",
                        max(0.0, time.monotonic() - episode_started),
                        episode_retries,
                        outbox.stats()["active_count"],
                    )
                logger.info(
                    "voice_callback_outbox event_id=%s worker_id=%d state=ACKED "
                    "attempts=%d status_code=%s backlog=%d outbox_queue_wait_ms=%.3f",
                    record.event_id, worker_id, record.attempts, result.status_code,
                    outbox.stats()["active_count"], outbox_queue_wait_ms,
                )
                capture_ms = iso_to_epoch_ms(record.payload.get("occurred_at") or (record.payload.get("packet") or {}).get("captured_at"))
                callback_done_ms = time.time() * 1000.0
                callback_total_ms = round(max(0.0, callback_done_ms - float(capture_ms)), 3) if capture_ms is not None else None
                payload_timing = dict(record.payload.get("timing_ms") or {})
                ack_timing = result.acknowledgement.get("timing_ms") if isinstance(result.acknowledgement, dict) else None
                speech_ms = payload_timing.get("speech_duration_ms")
                silence_ms = payload_timing.get("silence_confirm_ms")
                flushed_at_ms = payload_timing.get("flushed_at_epoch_ms")
                # flush→outbox 入队开始：包含 payload 构建与交付线程调度。
                flush_to_enqueue_ms = (
                    round(max(0.0, float(enqueued_at_ms) - float(flushed_at_ms)), 3)
                    if flushed_at_ms is not None and enqueued_at_ms is not None
                    else None
                )
                gateway_tree = {
                    "gateway_capture_to_callback_ms": {
                        "total_ms": callback_total_ms,
                        "async_completed": True,
                        "first_packet_at": payload_timing.get("first_packet_at"),
                        "last_packet_at": payload_timing.get("last_packet_at"),
                        "speech_duration_ms": speech_ms,
                        "silence_confirm_ms": silence_ms,
                        "callback_build_ms": payload_timing.get("callback_build_ms"),
                        "flush_to_enqueue_ms": flush_to_enqueue_ms,
                        "outbox_enqueue_ms": enqueue_meta.get("outbox_enqueue_ms"),
                        "outbox_queue_wait_ms": outbox_queue_wait_ms,
                        "callback_http_ms": {
                            "connect_ms": result.connect_ms, "write_ms": result.write_ms,
                            "read_ms": result.read_ms, "total_ms": result.total_ms,
                        },
                    },
                    "agent_ingress_ms": (ack_timing or {}).get("agent_ingress_ms") if isinstance(ack_timing, dict) else None,
                }
                # 时间树闭合：total = speech + silence + flush_to_enqueue
                # + outbox_queue_wait(已含 enqueue 执行) + callback_http + unattributed。
                # unattributed 为负时通常是抓包时钟与 time.time() 的微小偏移。
                if callback_total_ms is not None:
                    known_parts = (
                        speech_ms,
                        silence_ms,
                        flush_to_enqueue_ms,
                        outbox_queue_wait_ms,
                        result.total_ms,
                    )
                    gateway_tree["gateway_capture_to_callback_ms"]["unattributed_ms"] = round(
                        float(callback_total_ms)
                        - sum(float(v) for v in known_parts if v is not None),
                        3,
                    )
                log_latency_tree(
                    logger, "E2E_TIME_TREE_GATEWAY_CALLBACK", gateway_tree,
                    event_id=record.event_id, correlation_id=record.payload.get("correlation_id"), attempts=record.attempts,
                )
                self._outbox_enqueue_meta.pop(record.event_id, None)
                continue

            decision = self._retry_policy.decide(
                attempt=record.attempts,
                category=result.category,
                status_code=result.status_code,
            )
            error = f"{decision.category}: {result.message}"
            if decision.dead_letter:
                outbox.mark_dead_letter(record.event_id, error=error)
            else:
                outbox.mark_retry(
                    record.event_id,
                    error=error,
                    delay_seconds=decision.delay_seconds,
                )
            if decision.category in {
                "connect_timeout",
                "write_timeout",
                "read_timeout",
                "transport_error",
                "http_429",
                "http_5xx",
                "ack_parse_error",
            }:
                self._circuit_breaker.record_failure()
            else:
                self._circuit_breaker.release_probe()
            with self._lock:
                self._total_failed += 1
                self._last_error_at = now_iso()
                self._last_error = error
                self._last_status_code = result.status_code
            now_monotonic = time.monotonic()
            with self._lock:
                if self._failure_episode_started_at is None:
                    self._failure_episode_started_at = now_monotonic
                self._failure_episode_retries += 1
                should_log = (
                    self._failure_log_category != decision.category
                    or self._failure_log_last_at is None
                    or now_monotonic - self._failure_log_last_at >= 30.0
                    or decision.dead_letter
                )
                if should_log:
                    self._failure_log_category = decision.category
                    self._failure_log_last_at = now_monotonic
            if should_log:
                logger.warning(
                    "voice_callback_outbox event_id=%s worker_id=%d state=%s attempts=%d "
                    "failure_category=%s next_delay_seconds=%.3f backlog=%d",
                    record.event_id,
                    worker_id,
                    "DEAD_LETTER" if decision.dead_letter else "RETRY",
                    record.attempts,
                    decision.category,
                    decision.delay_seconds,
                    outbox.stats()["active_count"],
                )

    def _has_pending_merge(self) -> bool:
        """当前是否已有等待静音后合并发送的语音段。"""
        with self._lock:
            return self._merge_buffer is not None

    def _sync_audio_raw_cursor_to_end(self) -> None:
        """JSONL 主链路已读到音频时，跳过 raw 兜底文件的同批历史字节。

        audio_payload_raw.bin 是兜底通道，不应该和 packets.jsonl 主通道同时发送同一段语音。
        如果不同步游标，JSONL 读完之后下一轮没有 items，raw fallback 会从 0 开始
        再读一遍整段音频，导致同一次语音重复回调。
        """
        path = self._current_audio_raw_path()
        if path is None or not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        with self._lock:
            if self._audio_raw_path != path:
                self._audio_raw_path = path
            if self._audio_raw_pos < size:
                self._audio_raw_pos = size
            self._last_raw_fallback_bytes = 0
