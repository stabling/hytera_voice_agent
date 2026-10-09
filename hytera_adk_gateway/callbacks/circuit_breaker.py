# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/circuit_breaker.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：CircuitState, CircuitSnapshot, CircuitBreaker
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitSnapshot:
    state: CircuitState
    consecutive_failures: int
    opened_at: float | None
    next_probe_at: float | None
    half_open_inflight: int

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at,
            "next_probe_at": self.next_probe_at,
            "half_open_inflight": self.half_open_inflight,
        }


class CircuitBreaker:
    """Thread-safe closed/open/half-open circuit breaker."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        half_open_max_probes: int = 1,
        clock=time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_seconds = max(0.1, float(reset_seconds))
        self.half_open_max_probes = max(1, int(half_open_max_probes))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_inflight = 0

    def allow_request(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._state is CircuitState.OPEN:
                assert self._opened_at is not None
                if now - self._opened_at < self.reset_seconds:
                    return False
                self._state = CircuitState.HALF_OPEN
                self._half_open_inflight = 0
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_inflight >= self.half_open_max_probes:
                    return False
                self._half_open_inflight += 1
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_inflight = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                self._half_open_inflight = 0
                self._consecutive_failures = max(
                    self.failure_threshold, self._consecutive_failures + 1
                )
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    def release_probe(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN and self._half_open_inflight:
                self._half_open_inflight -= 1

    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            next_probe_at = (
                None
                if self._opened_at is None
                else self._opened_at + self.reset_seconds
            )
            return CircuitSnapshot(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                opened_at=self._opened_at,
                next_probe_at=next_probe_at,
                half_open_inflight=self._half_open_inflight,
            )
