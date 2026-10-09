# =============================================================================
# 【维护注释】文件：voice_agent/messages/writer.py
# 所属层次：归档异步写层
# 主要职责：独立后台线程消费归档队列并写入四库；BUSY 重试、幂等、
#           队列满/持续失败告警（需求 §14/§15）。
# 项目内直接依赖：.metrics, .store
# 主要公开符号：ArchiveWriter
# 兼容性：Planner 不等待 SQLite 写入；SQLite commit 时间不计入 Agent 主处理耗时。
# 修改原则：队列有界；写入失败按退避重试，超过上限计数并告警丢弃。
# 排障顺序：先看 queue_depth/retry/failure 指标，再对照告警日志。
# =============================================================================
"""异步归档写入线程（需求 §14/§15）。"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

from .metrics import MessageMetrics
from .store import ArchiveRecord, MessageArchiveStore

logger = logging.getLogger(__name__)

_SENTINEL = object()

_MAX_ATTEMPTS = 5
_RETRY_BACKOFF_SECONDS = 0.2


class ArchiveWriter:
    """单写线程 + 有界队列；submit() 只做内存入队（O(1)）。"""

    def __init__(
        self,
        store: MessageArchiveStore,
        metrics: MessageMetrics,
        *,
        queue_size: int = 1024,
        name: str = "message-archive-writer",
    ) -> None:
        self._store = store
        self._metrics = metrics
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._name = name
        self._drop_hook: Callable[[ArchiveRecord, str], None] | None = None
        self._writing = False
        metrics.set_queue_depth_provider(self.queue_depth)

    # ---- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def set_drop_hook(self, hook: Callable[[ArchiveRecord, str], None]) -> None:
        self._drop_hook = hook

    # ---- 提交 -----------------------------------------------------------
    def submit(self, record: ArchiveRecord) -> bool:
        """把归档任务交给后台线程；队列满时返回 False（调用方负责告警/降级）。"""
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self._metrics.inc("archive_drop_total")
            logger.error(
                "message_archive_queue_full category=%s event_id=%s "
                "queue_size=%s",
                record.category,
                record.event_id,
                self._queue.maxsize,
            )
            return False

    def flush(self, *, timeout: float = 5.0) -> bool:
        """等待队列排空（测试与优雅停机用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and not self._writing:
                return True
            time.sleep(0.01)
        return False

    # ---- 主循环 ---------------------------------------------------------
    def _run(self) -> None:
        logger.info("message archive writer started")
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                self._queue.task_done()
                break
            try:
                self._writing = True
                self._write_with_retry(item)
            except Exception as exc:  # 单条异常不能终止写线程
                self._metrics.inc("archive_failure_count")
                logger.exception(
                    "message_archive_write_failed category=%s event_id=%s",
                    getattr(item, "category", None),
                    getattr(item, "event_id", None),
                    exc_info=exc,
                )
                hook = self._drop_hook
                if hook is not None:
                    try:
                        hook(item, f"{type(exc).__name__}: {exc}")
                    except Exception:
                        pass
            finally:
                self._writing = False
                self._queue.task_done()
        logger.info("message archive writer stopped")

    def _write_with_retry(self, record: ArchiveRecord) -> None:
        attempts = 0
        while True:
            attempts += 1
            started = time.perf_counter()
            try:
                inserted = self._store.upsert(record)
            except Exception as exc:
                self._metrics.inc("archive_retry_count")
                if attempts >= _MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "message_archive_write_retry category=%s attempt=%s error=%s",
                    record.category,
                    attempts,
                    exc,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * attempts)
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._metrics.observe("archive_write_ms", elapsed_ms)
            logger.info(
                "message_archive_write_timing category=%s event_id=%s archive_write_ms=%.3f attempts=%d inserted=%s async_completed=true",
                record.category, record.event_id, elapsed_ms, attempts, inserted,
            )
            if attempts > 1:
                self._metrics.inc("archive_retry_count", attempts - 1)
            if inserted:
                self._metrics.inc(f"{record.category}_total")
            return
