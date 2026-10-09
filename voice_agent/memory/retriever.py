# =============================================================================
# 【维护注释】文件：voice_agent/memory/retriever.py
# 所属层次：长期记忆层
# 主要职责：负责记忆候选、检索、验证、向量/全文索引与生命周期；记忆不能直接触发设备命令。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：MemoryRetriever
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Structured-first retrieval with optional FTS/vector candidate layers."""

from __future__ import annotations

from typing import Any, Callable


class MemoryRetriever:
    def __init__(
        self,
        repository: Any,
        *,
        fts_enabled: bool = False,
        vector_enabled: bool = False,
        vector_search: Callable[..., list[str]] | None = None,
    ) -> None:
        self.repository = repository
        self.fts_enabled = bool(fts_enabled)
        self.vector_enabled = bool(vector_enabled)
        self.vector_search = vector_search

    def retrieve(
        self,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
        query: str | None,
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        exact = (
            self.repository.search(
                tenant_id=tenant_id, actor_id=actor_id, terminal_id=terminal_id,
                subject_entity_id=subject_entity_id, predicate=predicate,
                statuses=("VERIFIED",), limit=limit,
            )
            if subject_entity_id is not None or predicate is not None
            else []
        )
        seen = {str(row["memory_id"]) for row in exact}
        values = list(exact)
        if query and len(values) < limit:
            semantic = self.repository.search(
                tenant_id=tenant_id, actor_id=actor_id, terminal_id=terminal_id,
                query=query, statuses=("VERIFIED",), limit=limit,
                use_fts=self.fts_enabled,
            )
            for row in semantic:
                if str(row["memory_id"]) not in seen:
                    values.append(row)
                    seen.add(str(row["memory_id"]))
        if query and self.vector_enabled and self.vector_search and len(values) < limit:
            # Vector IDs are untrusted candidates. Every candidate is re-read through
            # permission-scoped repository filters before it can enter context. The
            # vector layer is optional: an outage must not suppress exact/FTS results.
            try:
                candidate_ids = self.vector_search(query=query, limit=limit * 3)
            except Exception:
                candidate_ids = []
            for memory_id in candidate_ids:
                item = self.repository.get_authorized(
                    str(memory_id), tenant_id=tenant_id, actor_id=actor_id, terminal_id=terminal_id
                )
                if item is None or item.verification_status.value != "VERIFIED":
                    continue
                if item.memory_id not in seen:
                    public = self.repository.search(
                        tenant_id=tenant_id, actor_id=actor_id, terminal_id=terminal_id,
                        memory_ids=(item.memory_id,), statuses=("VERIFIED",), limit=1,
                    )
                    if public:
                        values.extend(public)
                        seen.add(item.memory_id)
                if len(values) >= limit:
                    break
        return values[: max(1, int(limit))]
