# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/retry_policy.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：RetryDecision, RetryPolicy
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryDecision:
    category: str
    retryable: bool
    delay_seconds: float
    dead_letter: bool


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff for callback delivery failures."""

    base_seconds: float = 1.0
    max_seconds: float = 30.0
    jitter_ratio: float = 0.20
    max_attempts: int = 12

    def decide(
        self,
        *,
        attempt: int,
        category: str,
        status_code: int | None = None,
        random_value: float | None = None,
    ) -> RetryDecision:
        normalized = str(category or "unexpected_error")
        retryable = normalized in {
            "connect_timeout",
            "write_timeout",
            "read_timeout",
            "transport_error",
            "http_429",
            "http_5xx",
            "ack_parse_error",
        }
        if status_code is not None:
            if status_code == 429:
                normalized = "http_429"
                retryable = True
            elif 500 <= status_code <= 599:
                normalized = "http_5xx"
                retryable = True
            elif 400 <= status_code <= 499:
                normalized = "http_4xx_rejected"
                retryable = False

        exhausted = max(1, int(attempt)) >= max(1, int(self.max_attempts))
        if not retryable or exhausted:
            return RetryDecision(
                category=normalized,
                retryable=False,
                delay_seconds=0.0,
                dead_letter=True,
            )

        exponent = max(0, int(attempt) - 1)
        base_delay = min(float(self.max_seconds), float(self.base_seconds) * (2**exponent))
        sample = random.random() if random_value is None else max(0.0, min(1.0, random_value))
        jitter = base_delay * float(self.jitter_ratio) * ((sample * 2.0) - 1.0)
        delay = max(0.05, min(float(self.max_seconds), base_delay + jitter))
        return RetryDecision(
            category=normalized,
            retryable=True,
            delay_seconds=delay,
            dead_letter=False,
        )
