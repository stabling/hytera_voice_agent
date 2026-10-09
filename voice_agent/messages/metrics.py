# =============================================================================
# 【维护注释】文件：voice_agent/messages/metrics.py
# 所属层次：消息子系统可观测性
# 主要职责：分类/归档/历史查询的计数与时延指标（classify_ms、archive_enqueue_ms、
#           archive_write_ms、archive_queue_depth、archive_retry_count 等，需求 §32）。
# 项目内直接依赖：无
# 主要公开符号：MessageMetrics
# 兼容性：线程安全；ASR/Planner/Executor/MCP 原有计时不得混入 archive 后台耗时。
# 修改原则：计数器用锁保护；时延用有界环形样本计算分位数。
# 排障顺序：先看 snapshot()，再对照 ArchiveWriter 告警日志。
# =============================================================================
"""消息子系统指标（需求 §32）。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

_WINDOW_SIZE = 512
_PERCENTILES = (0.50, 0.95, 0.99)

_TIMER_NAMES = (
    "classify_ms",
    "archive_enqueue_ms",
    "archive_write_ms",
    "history_query_ms",
)

_COUNTER_NAMES = (
    "classify_total",
    "user_voice_total",
    "user_sms_total",
    "control_voice_total",
    "control_sms_total",
    "endpoint_incomplete_count",
    "archive_enqueue_total",
    "archive_drop_total",
    "archive_retry_count",
    "archive_failure_count",
    "history_query_count",
)


class MessageMetrics:
    """线程安全计数器 + 有界时延样本。"""

    def __init__(self, *, window_size: int = _WINDOW_SIZE) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {name: 0 for name in _COUNTER_NAMES}
        self._timers: dict[str, deque[float]] = {
            name: deque(maxlen=max(16, int(window_size))) for name in _TIMER_NAMES
        }
        self._queue_depth_provider: Callable[[], int] | None = None

    # ---- 计数 -----------------------------------------------------------
    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + int(value)

    # ---- 计时 -----------------------------------------------------------
    def observe(self, name: str, milliseconds: float) -> None:
        with self._lock:
            timer = self._timers.get(name)
            if timer is not None:
                timer.append(float(milliseconds))

    def time_block(self, name: str) -> "_Timer":
        return _Timer(self, name)

    def set_queue_depth_provider(self, provider: Callable[[], int]) -> None:
        self._queue_depth_provider = provider

    # ---- 读取 -----------------------------------------------------------
    @staticmethod
    def _percentile(samples: list[float], fraction: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            timers: dict[str, Any] = {}
            for name, samples in self._timers.items():
                values = list(samples)
                timers[name] = {
                    "count": len(values),
                    "p50": round(self._percentile(values, 0.50), 3),
                    "p95": round(self._percentile(values, 0.95), 3),
                    "p99": round(self._percentile(values, 0.99), 3),
                }
        depth = 0
        provider = self._queue_depth_provider
        if provider is not None:
            try:
                depth = int(provider())
            except Exception:
                depth = -1
        return {
            "counters": counters,
            "timers": timers,
            "archive_queue_depth": depth,
        }


class _Timer:
    """上下文管理器计时器；只测量同步区间，不跨 await。"""

    def __init__(self, metrics: MessageMetrics, name: str) -> None:
        self._metrics = metrics
        self._name = name
        self._started = 0.0

    def __enter__(self) -> "_Timer":
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed_ms = (time.perf_counter() - self._started) * 1000.0
        self._metrics.observe(self._name, elapsed_ms)
