# =============================================================================
# 【维护注释】文件：voice_agent/memory/extractor.py
# 所属层次：长期记忆层
# 主要职责：负责记忆候选、检索、验证、向量/全文索引与生命周期；记忆不能直接触发设备命令。
# 项目内直接依赖：.models
# 主要公开符号：MemoryExtractor
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Offline-only extraction of memory candidates from completed facts."""

from __future__ import annotations

from typing import Any, Iterable

from .models import MemoryItem, MemoryStatus, MemoryType

_TERMINAL_OPERATION_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "UNKNOWN"}


class MemoryExtractor:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def episodic_from_operation(
        self,
        operation: dict[str, Any],
        *,
        evidence_event_ids: Iterable[str],
        tenant_id: str | None = None,
        summary: str | None = None,
    ) -> MemoryItem:
        status = str(operation.get("status") or "")
        if status not in _TERMINAL_OPERATION_STATUSES:
            raise ValueError("only completed/terminal operations can create episodic candidates")
        events = sorted({str(item) for item in evidence_event_ids if str(item)})
        if not events:
            raise ValueError("episodic candidate requires evidence event IDs")
        item = MemoryItem(
            namespace=f"actor:{operation.get('actor_id') or 'unknown'}:episodes",
            memory_type=MemoryType.EPISODIC,
            tenant_id=tenant_id,
            actor_id=None if operation.get("actor_id") is None else str(operation.get("actor_id")),
            terminal_id=None if operation.get("actor_id") is None else str(operation.get("actor_id")),
            subject_entity_id=str(operation.get("target_id") or operation.get("operation_id")),
            predicate=str(operation.get("operation_type") or "operation.outcome"),
            object={
                "status": status,
                "operation_id": operation.get("operation_id"),
                "operation_type": operation.get("operation_type"),
                "target_id": operation.get("target_id"),
            },
            summary=summary or f"{operation.get('operation_type')} -> {status}",
            content="",
            confidence=1.0,
            verification_status=MemoryStatus.CANDIDATE,
            evidence_event_ids=events,
            source_type="operation_extractor",
            source_operation_id=str(operation.get("operation_id")),
            sensitivity="INTERNAL",
            provenance={"extractor": "operation"},
        )
        return self.repository.create(item)

    def semantic_candidate(
        self,
        *,
        namespace: str,
        subject_entity_id: str,
        predicate: str,
        object_value: Any,
        summary: str,
        evidence_event_ids: Iterable[str],
        confidence: float,
        actor_id: str | None = None,
        terminal_id: str | None = None,
        tenant_id: str | None = None,
        source_model: str | None = None,
    ) -> MemoryItem:
        return self.repository.create(MemoryItem(
            namespace=namespace,
            memory_type=MemoryType.SEMANTIC,
            tenant_id=tenant_id,
            actor_id=actor_id,
            terminal_id=terminal_id,
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            object=object_value,
            summary=summary,
            content="",
            confidence=confidence,
            verification_status=MemoryStatus.CANDIDATE,
            evidence_event_ids=list(evidence_event_ids),
            source_type="model_extractor",
            source_model=source_model,
            sensitivity="INTERNAL",
            provenance={"extractor": "semantic"},
        ))

    def procedural_candidate(
        self,
        *,
        namespace: str,
        operation_type: str,
        statistics: dict[str, Any],
        evidence_event_ids: Iterable[str],
        tenant_id: str | None = None,
    ) -> MemoryItem:
        """Create an offline capability-improvement candidate; never alters code or tools."""
        return self.repository.create(MemoryItem(
            namespace=namespace,
            memory_type=MemoryType.PROCEDURAL_CANDIDATE,
            tenant_id=tenant_id,
            subject_entity_id=operation_type,
            predicate="capability.improvement_candidate",
            object=statistics,
            summary=f"Offline improvement candidate for {operation_type}",
            content="",
            confidence=float(statistics.get("confidence") or 0.5),
            verification_status=MemoryStatus.CANDIDATE,
            evidence_event_ids=list(evidence_event_ids),
            source_type="aggregate_extractor",
            sensitivity="INTERNAL",
            provenance={"online_code_change": False, "online_tool_change": False},
        ))
