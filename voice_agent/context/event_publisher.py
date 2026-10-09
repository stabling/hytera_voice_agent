# =============================================================================
# 【维护注释】文件：voice_agent/context/event_publisher.py
# 所属层次：决策上下文层
# 主要职责：负责事件、历史、终端状态等上下文的构建、裁剪、脱敏和快照，为规划器提供受控信息。
# 项目内直接依赖：shared_runtime.event_publisher
# 主要公开符号：build_event_publisher
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from pathlib import Path

from shared_runtime.event_publisher import EventPublisher
from shared_runtime.event_publisher import build_event_publisher as _build_shared_publisher


def build_event_publisher(*, source_service: str, default_path: str | Path = "data/state.sqlite3") -> EventPublisher:
    from ..storage.runtime import StorageBackendMode, get_storage_runtime

    runtime = get_storage_runtime()
    store = None
    if runtime.postgres_enabled:
        from ..storage.postgres.event_repository import PostgresEventRepository

        pool = runtime.postgres_pool
        if pool is not None:
            store = PostgresEventRepository(pool)
    return _build_shared_publisher(
        source_service=source_service,
        default_path=default_path,
        external_store=store,
        external_primary=runtime.mode is StorageBackendMode.POSTGRES,
        external_fail_open=runtime.fail_open,
    )


__all__ = ["EventPublisher", "build_event_publisher"]
