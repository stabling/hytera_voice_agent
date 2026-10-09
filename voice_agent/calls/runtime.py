# =============================================================================
# 【维护注释】文件：voice_agent/calls/runtime.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.event_models, .correlator, .lifecycle_tracker, .models, .repository, .transcript_collector
# 主要公开符号：CallStoreRuntime, build_call_store_runtime
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Feature-flagged CallStore facade and restart-safe event projector."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from shared_contracts.event_models import DomainEvent

from .correlator import CallCorrelationEngine
from .lifecycle_tracker import CallLifecycleTracker
from .models import CallSelector
from .repository import CallStoreRepository
from .transcript_collector import TranscriptCollector

logger = logging.getLogger(__name__)


class CallStoreRuntime:
    def __init__(
        self,
        *,
        path: str | Path,
        mode: str = "false",
        fail_mode: str = "open",
        correlation_window_seconds: float = 120.0,
        correlation_min_score: float = 0.75,
        projector_poll_seconds: float = 1.0,
    ) -> None:
        normalized = str(mode or "false").strip().lower()
        self.mode = normalized if normalized in {"false", "shadow", "true"} else "false"
        failure = str(fail_mode or "open").strip().lower()
        self.fail_mode = failure if failure in {"open", "closed"} else "open"
        self.path = Path(path).expanduser().resolve(strict=False)
        self.correlation_window_seconds = max(1.0, float(correlation_window_seconds))
        self.correlation_min_score = max(0.0, min(1.0, float(correlation_min_score)))
        self.projector_poll_seconds = max(0.1, float(projector_poll_seconds))
        self._repository: CallStoreRepository | None = None
        self._query_repository: Any | None = None
        self._tracker: CallLifecycleTracker | None = None
        self._collector: TranscriptCollector | None = None
        self._stop = threading.Event()
        self._projector_thread: threading.Thread | None = None
        self._publisher: Any = None
        self._lock = threading.RLock()
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "true"}

    @property
    def readable(self) -> bool:
        return self.enabled or self.path.exists()

    @property
    def repository(self) -> CallStoreRepository:
        if self._repository is None:
            self._repository = CallStoreRepository(self.path)
        return self._repository

    @property
    def query_repository(self) -> Any:
        if os.environ.get("STORAGE_BACKEND", "sqlite").strip().lower() != "postgres":
            return self.repository
        if self._query_repository is None:
            from voice_agent.storage.runtime import get_storage_runtime
            from voice_agent.storage.postgres.repositories import PostgresCallRepository
            pool = get_storage_runtime().postgres_pool
            if pool is None:
                raise RuntimeError("PostgreSQL call read backend is unavailable")
            tenant = os.environ.get("CONTEXT_DEFAULT_TENANT_ID", "").strip() or "default"
            self._query_repository = PostgresCallRepository(pool, tenant_id=tenant)
        return self._query_repository

    @property
    def tracker(self) -> CallLifecycleTracker:
        if self._tracker is None:
            correlator = CallCorrelationEngine(
                self.repository,
                window_seconds=self.correlation_window_seconds,
                min_score=self.correlation_min_score,
            )
            self._tracker = CallLifecycleTracker(self.repository, correlator)
        return self._tracker

    @property
    def collector(self) -> TranscriptCollector:
        if self._collector is None:
            self._collector = TranscriptCollector(self.repository, self.tracker)
        return self._collector

    def consume(self, event: DomainEvent | dict[str, Any]) -> str | None:
        if not self.enabled:
            return None
        try:
            return self.tracker.project(event)
        except Exception as exc:
            self._handle_error("call_store_project_failed", exc)
            return None

    def collect_transcript(self, event: DomainEvent | dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            inserted, turn = self.collector.collect(event)
            return {
                "inserted": inserted,
                "duplicate": not inserted,
                "call_id": turn.call_id,
                "turn_id": turn.turn_id,
                "event_id": turn.event_id,
            }
        except Exception as exc:
            self._handle_error("call_turn_write_failed", exc)
            return None

    def resolve_call_id_for_event(self, event_id: object) -> str | None:
        value = str(event_id or "").strip()
        if not value or not self.readable:
            return None
        try:
            return self.repository.find_by_event_id(value)
        except Exception as exc:
            self._handle_error("call_event_lookup_failed", exc)
            return None

    def resolve_call_id_for_voice_data(self, data: dict[str, Any]) -> str | None:
        direct = str(data.get("call_id") or "").strip()
        if direct:
            return direct
        for key in ("accepted_event_id", "event_id"):
            call_id = self.resolve_call_id_for_event(data.get(key))
            if call_id:
                return call_id
        return None

    def start_projector(self, publisher: Any) -> None:
        if not self.enabled or self._projector_thread is not None:
            return
        self._publisher = publisher
        self._sweep_stale_sessions()
        self._stop.clear()
        self._projector_thread = threading.Thread(
            target=self._projector_loop,
            name="call-store-projector",
            daemon=True,
        )
        self._projector_thread.start()

    def _sweep_stale_sessions(self) -> None:
        """启动时清理陈旧 active 会话（僵尸会话防护）。

        事件链断裂（缺 call.ended、进程重启丢上下文）会让 active 会话
        永久存活，之后经 request_id 回绕不断吞掉新音频（2026-09-29 真机
        复现：8 月 25 日的 active 会话存活 35 天）。委托
        repository.close_stale_sessions 执行，阈值由
        CALL_STORE_STALE_SESSION_MAX_AGE_SECONDS 控制（默认 86400 秒）。
        """
        try:
            max_age = float(os.environ.get("CALL_STORE_STALE_SESSION_MAX_AGE_SECONDS", "86400"))
        except (TypeError, ValueError):
            max_age = 86400.0
        try:
            closed = self.repository.close_stale_sessions(max_age_seconds=max_age)
        except Exception as exc:
            self._handle_error("call_store_stale_sweep_failed", exc)
            return
        if closed:
            logger.info(
                "stale_active_session_sweep closed=%d max_age_seconds=%s path=%s",
                closed,
                max_age,
                self.path,
            )

    def stop_projector(self) -> None:
        self._stop.set()
        if self._projector_thread is not None:
            self._projector_thread.join(timeout=5.0)
            self._projector_thread = None

    def recover_once(self, publisher: Any, *, batch_size: int = 200) -> int:
        if not self.enabled:
            return 0
        offset = self.repository.get_projector_offset()
        rows = publisher.read_after_offset(offset, limit=max(1, int(batch_size)))
        projected = 0
        for item in rows:
            journal_offset = int(item.pop("journal_offset"))
            event = DomainEvent.model_validate(item)
            self.tracker.project(event)
            if event.event_type in {"asr.completed", "asr.failed"}:
                self.collector.collect(event)
            self.repository.set_projector_offset(journal_offset)
            projected += 1
        return projected

    def _projector_loop(self) -> None:
        while not self._stop.is_set():
            try:
                projected = self.recover_once(self._publisher)
                if projected:
                    continue
            except Exception as exc:
                self._handle_error("call_store_recovery_failed", exc)
            self._stop.wait(self.projector_poll_seconds)

    def list_calls(self, selector: CallSelector | dict[str, Any]) -> list[dict[str, Any]]:
        if not self.readable:
            return []
        return self.query_repository.list_sessions(selector)

    def get_call(
        self,
        call_id: str,
        *,
        include_transcript: bool = True,
        authorized_terminal_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.readable:
            return None
        if os.environ.get("STORAGE_BACKEND", "sqlite").strip().lower() == "postgres":
            return self.query_repository.get_call(
                call_id,
                include_transcript=include_transcript,
                authorized_terminal_id=authorized_terminal_id,
            )
        return self.query_repository.get_call(call_id, include_transcript=include_transcript)

    def metrics(self) -> dict[str, Any]:
        if not self.readable:
            return {
                "enabled": False,
                "mode": self.mode,
                "path": str(self.path),
                "call_total": 0,
                "turn_total": 0,
                "last_error": self._last_error,
            }
        return {
            **self.repository.metrics(low_confidence_threshold=self.correlation_min_score),
            "enabled": self.enabled,
            "mode": self.mode,
            "last_error": self._last_error,
        }

    def _handle_error(self, operation: str, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("%s path=%s error=%s", operation, self.path, self._last_error)
        if self.fail_mode == "closed":
            raise exc


def build_call_store_runtime(
    *,
    default_path: str | Path = "data/context/call_store.sqlite3",
) -> CallStoreRuntime:
    return CallStoreRuntime(
        path=os.environ.get("CALL_STORE_PATH", str(default_path)),
        mode=os.environ.get("CALL_STORE_ENABLE", "false"),
        fail_mode=os.environ.get("CALL_STORE_FAIL_MODE", "open"),
        correlation_window_seconds=float(os.environ.get("CALL_CORRELATION_WINDOW_SECONDS", "120")),
        correlation_min_score=float(os.environ.get("CALL_CORRELATION_MIN_SCORE", "0.75")),
        projector_poll_seconds=float(os.environ.get("CALL_PROJECTOR_POLL_SECONDS", "1.0")),
    )
