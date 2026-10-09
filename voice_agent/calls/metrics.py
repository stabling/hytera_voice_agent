# =============================================================================
# 【维护注释】文件：voice_agent/calls/metrics.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：.repository
# 主要公开符号：collect_call_store_metrics
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Data-integrity metrics for CallStore shadow verification."""

from __future__ import annotations

from typing import Any

from .repository import CallStoreRepository


def collect_call_store_metrics(
    repository: CallStoreRepository,
    *,
    low_confidence_threshold: float = 0.75,
) -> dict[str, Any]:
    return repository.metrics(low_confidence_threshold=low_confidence_threshold)
