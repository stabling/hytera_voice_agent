# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/terminal_sessions.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：validate_terminal_id, normalize_optional_channel_id, TerminalDeliveryContext, TerminalSession, TerminalSessionManager
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Thread-safe terminal delivery sessions for voice callback routing."""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

_MIN_RADIO_ID = 1
_MAX_RADIO_ID = 16_777_215
_MAX_CHANNEL_ID = 0xFFFFFFFF


def validate_terminal_id(value: object) -> int:
    """Return a strict Hytera Radio ID or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("terminal_id must be an integer")
    if not _MIN_RADIO_ID <= value <= _MAX_RADIO_ID:
        raise ValueError("terminal_id must be 1..16777215")
    return value


def normalize_optional_channel_id(value: object) -> int | None:
    """Normalize an optional positive UInt32 channel ID without guessing strings."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 1 <= value <= _MAX_CHANNEL_ID:
        return None
    return value


@dataclass(frozen=True)
class TerminalDeliveryContext:
    """Immutable routing metadata bound to one voice task before queueing."""

    terminal_id: int
    terminal_session_id: str
    channel_id: int | None
    capture_session_id: str | None
    source_request_id: int
    terminal_sequence: int


@dataclass
class TerminalSession:
    """Mutable per-terminal synchronization state owned by the manager."""

    terminal_id: int
    session_id: str
    created_at: float
    last_seen_at: float
    channel_id: int | None
    processing_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    next_sequence_to_assign: int = 1
    next_sequence_to_process: int = 1
    pending_tasks: int = 0
    active_tasks: int = 0
    cancelled_sequences: set[int] = field(default_factory=set, repr=False, compare=False)
    processing_condition: threading.Condition = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.processing_condition = threading.Condition(self.processing_lock)


class TerminalSessionManager:
    """Manage isolated per-radio sessions and deterministic per-radio task order."""

    def __init__(
        self,
        idle_timeout_seconds: float = 1800.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        timeout = float(idle_timeout_seconds)
        if timeout <= 0:
            raise ValueError("idle_timeout_seconds must be > 0")
        self.idle_timeout_seconds = timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[int, TerminalSession] = {}

    def get_or_create(
        self,
        terminal_id: int,
        *,
        channel_id: int | None = None,
    ) -> tuple[TerminalSession, bool]:
        terminal_id = validate_terminal_id(terminal_id)
        channel_id = normalize_optional_channel_id(channel_id)
        with self._lock:
            return self._get_or_create_locked(terminal_id, channel_id=channel_id)

    def update(
        self,
        terminal_id: int,
        *,
        channel_id: int | None = None,
    ) -> TerminalSession | None:
        terminal_id = validate_terminal_id(terminal_id)
        normalized_channel = normalize_optional_channel_id(channel_id)
        with self._lock:
            session = self._sessions.get(terminal_id)
            if session is None:
                return None
            with session.processing_lock:
                session.last_seen_at = self._clock()
                if normalized_channel is not None:
                    session.channel_id = normalized_channel
            return session

    def get(self, terminal_id: int) -> TerminalSession | None:
        terminal_id = validate_terminal_id(terminal_id)
        with self._lock:
            return self._sessions.get(terminal_id)

    def bind_delivery(
        self,
        *,
        terminal_id: int,
        channel_id: int | None,
        capture_session_id: object,
        source_request_id: int,
    ) -> tuple[TerminalDeliveryContext, bool]:
        """Create an immutable task context and reserve its per-terminal FIFO turn."""
        terminal_id = validate_terminal_id(terminal_id)
        normalized_channel = normalize_optional_channel_id(channel_id)
        capture_id = str(capture_session_id).strip() if capture_session_id not in (None, "") else None
        with self._lock:
            session, created = self._get_or_create_locked(
                terminal_id, channel_id=normalized_channel
            )
            with session.processing_lock:
                sequence = session.next_sequence_to_assign
                session.next_sequence_to_assign += 1
                session.pending_tasks += 1
                session.last_seen_at = self._clock()
                if normalized_channel is not None:
                    session.channel_id = normalized_channel
                context = TerminalDeliveryContext(
                    terminal_id=terminal_id,
                    terminal_session_id=session.session_id,
                    channel_id=normalized_channel,
                    capture_session_id=capture_id,
                    source_request_id=int(source_request_id),
                    terminal_sequence=sequence,
                )
        return context, created

    def cancel_delivery(self, context: TerminalDeliveryContext) -> None:
        """Release a reserved turn when queue insertion fails."""
        session = self._session_for_context(context)
        if session is None:
            return
        with session.processing_condition:
            if context.terminal_sequence < session.next_sequence_to_process:
                return
            session.cancelled_sequences.add(context.terminal_sequence)
            if session.pending_tasks > 0:
                session.pending_tasks -= 1
            self._advance_cancelled_locked(session)
            session.last_seen_at = self._clock()
            session.processing_condition.notify_all()

    @contextmanager
    def processing_turn(self, context: TerminalDeliveryContext) -> Iterator[TerminalSession]:
        """Run one task in reserved FIFO order while serializing only its terminal."""
        session = self._session_for_context(context)
        if session is None:
            raise RuntimeError("terminal session is unavailable or has changed")

        with session.processing_condition:
            while context.terminal_sequence != session.next_sequence_to_process:
                session.processing_condition.wait()
            if session.pending_tasks > 0:
                session.pending_tasks -= 1
            session.active_tasks += 1
            session.last_seen_at = self._clock()
            try:
                yield session
            finally:
                session.active_tasks -= 1
                session.next_sequence_to_process += 1
                self._advance_cancelled_locked(session)
                session.last_seen_at = self._clock()
                session.processing_condition.notify_all()

    def cleanup_expired(self) -> list[TerminalSession]:
        """Remove idle sessions that have no queued or active tasks."""
        now = self._clock()
        removed: list[TerminalSession] = []
        with self._lock:
            for terminal_id, session in list(self._sessions.items()):
                with session.processing_lock:
                    idle = now - session.last_seen_at
                    if (
                        idle >= self.idle_timeout_seconds
                        and session.pending_tasks == 0
                        and session.active_tasks == 0
                    ):
                        removed.append(self._sessions.pop(terminal_id))
        return removed

    def _get_or_create_locked(
        self,
        terminal_id: int,
        *,
        channel_id: int | None,
    ) -> tuple[TerminalSession, bool]:
        session = self._sessions.get(terminal_id)
        now = self._clock()
        if session is None:
            session = TerminalSession(
                terminal_id=terminal_id,
                session_id=uuid.uuid4().hex,
                created_at=now,
                last_seen_at=now,
                channel_id=channel_id,
            )
            self._sessions[terminal_id] = session
            return session, True
        with session.processing_lock:
            session.last_seen_at = now
            if channel_id is not None:
                session.channel_id = channel_id
        return session, False

    def _session_for_context(
        self, context: TerminalDeliveryContext
    ) -> TerminalSession | None:
        with self._lock:
            session = self._sessions.get(context.terminal_id)
            if session is None or session.session_id != context.terminal_session_id:
                return None
            return session

    @staticmethod
    def _advance_cancelled_locked(session: TerminalSession) -> None:
        while session.next_sequence_to_process in session.cancelled_sequences:
            session.cancelled_sequences.remove(session.next_sequence_to_process)
            session.next_sequence_to_process += 1
