# =============================================================================
# 【维护注释】文件：voice_agent/context/builder.py
# 所属层次：决策上下文层
# 主要职责：负责事件、历史、终端状态等上下文的构建、裁剪、脱敏和快照，为规划器提供受控信息。
# 项目内直接依赖：.models, .redaction
# 主要公开符号：ContextBuilder
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Authority-first, permission-scoped, budgeted DecisionContext builder."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import (
    ContextBuildRequest,
    ContextItem,
    ContextSection,
    ContextSnapshot,
    DecisionContext,
    Sensitivity,
)
from .redaction import redact_payload


_AUTHORITY = {
    "current_request": 100,
    "policy": 100,
    "terminal": 98,
    "active_call": 96,
    "directory": 94,
    "capability_catalog": 90,
    "verified_memory": 86,
    "call": 80,
    "open_task": 78,
    "operation": 72,
    "episodic_memory": 68,
    "semantic_search": 55,
}
_MANDATORY = {ContextSection.CURRENT_REQUEST, ContextSection.CURRENT_TERMINAL, ContextSection.AUTHORITATIVE_DIRECTORY, ContextSection.POLICY_CONSTRAINTS}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _freshness(observed_at: Any, *, half_life_days: float = 7.0) -> float:
    parsed = _parse_time(observed_at)
    if parsed is None:
        return 0.5
    age_days = max(0.0, (_now() - parsed).total_seconds() / 86400.0)
    return max(0.05, math.exp(-age_days * math.log(2.0) / max(0.1, half_life_days)))


def _token_estimate(payload: Any) -> int:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # A conservative mixed Chinese/Latin approximation; budgets remain deterministic.
    return max(1, math.ceil(len(text) / 3.0))


class ContextBuilder:
    def __init__(self, repository: Any, *, semantic_enabled: bool = False) -> None:
        self.repository = repository
        self.semantic_enabled = bool(semantic_enabled)

    def _item(
        self,
        *,
        section: ContextSection,
        source_type: str,
        source_id: str,
        payload: dict[str, Any],
        authority: int,
        confidence: float = 1.0,
        observed_at: str | None = None,
        evidence_event_ids: Iterable[str] = (),
        memory_id: str | None = None,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        access_level: Sensitivity = Sensitivity.INTERNAL,
        conflict_key: str | None = None,
        sanitize_untrusted: bool = True,
        allow_restricted_fields: bool = False,
    ) -> ContextItem:
        safe, redacted, untrusted = redact_payload(
            payload, sensitivity=sensitivity, access_level=access_level,
            sanitize_untrusted=sanitize_untrusted,
            allow_restricted_fields=allow_restricted_fields,
        )
        fresh = _freshness(observed_at)
        score = authority * 0.65 + confidence * 20.0 + fresh * 15.0
        return ContextItem(
            item_id=f"{source_type}:{source_id}",
            section=section,
            source_type=source_type,
            source_id=str(source_id),
            payload=dict(safe),
            authority=authority,
            confidence=confidence,
            freshness=fresh,
            observed_at=observed_at,
            evidence_event_ids=list(evidence_event_ids),
            memory_id=memory_id,
            sensitivity=sensitivity,
            redacted=redacted,
            untrusted_content=untrusted,
            conflict_key=conflict_key,
            rank_score=round(score, 6),
            token_estimate=_token_estimate(safe),
        )

    def _collect(self, request: ContextBuildRequest) -> list[ContextItem]:
        items: list[ContextItem] = []
        request_payload = {
            "request_id": request.request_id,
            "text": request.request_text,
            "command_id": request.command_id,
        }
        items.append(self._item(
            section=ContextSection.CURRENT_REQUEST,
            source_type="current_request",
            source_id=request.command_id or request.request_id or "current",
            payload=request_payload,
            authority=_AUTHORITY["current_request"],
            observed_at=_now().isoformat().replace("+00:00", "Z"),
            sensitivity=Sensitivity.INTERNAL,
            access_level=request.access_level,
            sanitize_untrusted=False,
            allow_restricted_fields=True,
        ))
        if request.authoritative_directory:
            items.append(self._item(
                section=ContextSection.AUTHORITATIVE_DIRECTORY,
                source_type="directory",
                source_id="zone_channel",
                payload=dict(request.authoritative_directory),
                authority=_AUTHORITY["directory"],
                observed_at=_now().isoformat().replace("+00:00", "Z"),
                access_level=request.access_level,
            ))
        if request.terminal_id:
            terminal_payload = {"terminal_id": request.terminal_id, **request.terminal_state}
            items.append(self._item(
                section=ContextSection.CURRENT_TERMINAL,
                source_type="terminal",
                source_id=request.terminal_id,
                payload=terminal_payload,
                authority=_AUTHORITY["terminal"],
                observed_at=_now().isoformat().replace("+00:00", "Z"),
                access_level=request.access_level,
            ))
            active = self.repository.active_call(terminal_id=request.terminal_id)
            if active:
                items.append(self._item(
                    section=ContextSection.ACTIVE_CALL,
                    source_type="active_call",
                    source_id=str(active.get("call_id")),
                    payload=active,
                    authority=_AUTHORITY["active_call"],
                    confidence=float(active.get("correlation_confidence") or 1.0),
                    observed_at=active.get("last_audio_at") or active.get("updated_at"),
                    access_level=request.access_level,
                    conflict_key=f"active_call:{request.terminal_id}",
                ))
            for row in self.repository.recent_calls(
                terminal_id=request.terminal_id, days=request.recent_call_days, limit=request.max_items * 2
            ):
                call_id = str(row.get("call_id") or "")
                if active and call_id == str(active.get("call_id")):
                    continue
                items.append(self._item(
                    section=ContextSection.RECENT_CALLS,
                    source_type="call",
                    source_id=call_id,
                    payload=row,
                    authority=_AUTHORITY["call"],
                    confidence=float(row.get("correlation_confidence") or 1.0),
                    observed_at=row.get("ended_at") or row.get("last_audio_at") or row.get("created_at"),
                    access_level=request.access_level,
                ))
        if request.actor_id:
            for row in self.repository.recent_operations(actor_id=request.actor_id, limit=request.max_items * 2):
                items.append(self._item(
                    section=ContextSection.RECENT_OPERATIONS,
                    source_type="operation",
                    source_id=str(row.get("operation_id")),
                    payload=row,
                    authority=_AUTHORITY["operation"],
                    confidence=1.0 if row.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"} else 0.7,
                    observed_at=row.get("completed_at") or row.get("updated_at") or row.get("created_at"),
                    access_level=request.access_level,
                ))
            for row in self.repository.open_tasks(actor_id=request.actor_id, limit=request.max_items):
                items.append(self._item(
                    section=ContextSection.OPEN_TASKS,
                    source_type="open_task",
                    source_id=str(row.get("command_id")),
                    payload=row,
                    authority=_AUTHORITY["open_task"],
                    observed_at=row.get("updated_at") or row.get("created_at"),
                    access_level=request.access_level,
                ))
        memories = self.repository.verified_memories(
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            terminal_id=request.terminal_id,
            query=request.semantic_query,
            limit=request.max_items * 2,
            use_semantic=self.semantic_enabled and bool(request.semantic_query),
        )
        for row in memories:
            source_type = "directory" if row.get("source_type") == "directory" else "verified_memory"
            authority = _AUTHORITY[source_type]
            payload = {
                "memory_type": row.get("memory_type"),
                "subject_entity_id": row.get("subject_entity_id"),
                "predicate": row.get("predicate"),
                "object": row.get("object"),
                "summary": row.get("summary"),
                "source_type": row.get("source_type"),
                "verification_status": row.get("verification_status"),
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
            }
            items.append(self._item(
                section=ContextSection.VERIFIED_MEMORIES,
                source_type=source_type,
                source_id=str(row.get("memory_id")),
                payload=payload,
                authority=authority,
                confidence=float(row.get("confidence") or 0.0),
                observed_at=row.get("updated_at") or row.get("created_at"),
                evidence_event_ids=row.get("evidence_event_ids") or [],
                memory_id=str(row.get("memory_id")),
                sensitivity=Sensitivity(str(row.get("sensitivity") or "INTERNAL")),
                access_level=request.access_level,
                conflict_key=(
                    f"{row.get('namespace')}:{row.get('subject_entity_id')}:{row.get('predicate')}"
                    if row.get("predicate") else None
                ),
            ))
        for row in request.available_capabilities:
            items.append(self._item(
                section=ContextSection.AVAILABLE_CAPABILITIES,
                source_type="capability_catalog",
                source_id=str(row.get("id") or "unknown"),
                payload=row,
                authority=_AUTHORITY["capability_catalog"],
                observed_at=_now().isoformat().replace("+00:00", "Z"),
                access_level=request.access_level,
            ))
        for index, row in enumerate(request.policy_constraints):
            items.append(self._item(
                section=ContextSection.POLICY_CONSTRAINTS,
                source_type="policy",
                source_id=str(row.get("policy_id") or index),
                payload=row,
                authority=_AUTHORITY["policy"],
                observed_at=_now().isoformat().replace("+00:00", "Z"),
                access_level=request.access_level,
            ))
        return items

    @staticmethod
    def _conflicts(items: list[ContextItem], request: ContextBuildRequest) -> list[ContextItem]:
        grouped: dict[str, list[ContextItem]] = {}
        for item in items:
            if item.conflict_key:
                grouped.setdefault(item.conflict_key, []).append(item)
        conflicts: list[ContextItem] = []
        for key, rows in grouped.items():
            canonical = {
                json.dumps(row.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for row in rows
            }
            if len(canonical) <= 1:
                continue
            winner = max(rows, key=lambda row: (row.authority, row.confidence, row.freshness, row.item_id))
            conflicts.append(ContextItem(
                item_id=f"conflict:{key}",
                section=ContextSection.UNRESOLVED_CONFLICTS,
                source_type="conflict",
                source_id=key,
                payload={
                    "conflict_key": key,
                    "candidate_item_ids": sorted(row.item_id for row in rows),
                    "preferred_item_id": winner.item_id,
                    "rule": "authority_then_confidence_then_freshness",
                },
                authority=100,
                confidence=1.0,
                freshness=1.0,
                sensitivity=Sensitivity.INTERNAL,
                rank_score=100.0,
                token_estimate=32,
            ))
        return conflicts

    @staticmethod
    def _select(items: list[ContextItem], *, max_items: int, max_tokens: int) -> tuple[list[ContextItem], bool]:
        ordered = sorted(
            items,
            key=lambda row: (
                row.section not in _MANDATORY,
                -row.rank_score,
                -row.authority,
                row.item_id,
            ),
        )
        chosen: list[ContextItem] = []
        used = 0
        for item in ordered:
            mandatory = item.section in _MANDATORY
            if len(chosen) >= max_items and not mandatory:
                continue
            if used + item.token_estimate > max_tokens and not mandatory:
                continue
            chosen.append(item)
            used += item.token_estimate
        return chosen, len(chosen) < len(items)

    def build(self, request: ContextBuildRequest | dict[str, Any]) -> tuple[DecisionContext, ContextSnapshot]:
        req = request if isinstance(request, ContextBuildRequest) else ContextBuildRequest.model_validate(request)
        candidates = self._collect(req)
        candidates.extend(self._conflicts(candidates, req))
        selected, truncated = self._select(candidates, max_items=req.max_items, max_tokens=req.max_tokens)
        grouped: dict[str, list[ContextItem]] = {section.value: [] for section in ContextSection}
        for item in selected:
            grouped[item.section.value].append(item)
        for rows in grouped.values():
            rows.sort(key=lambda row: (-row.rank_score, row.item_id))
        context = DecisionContext(
            command_id=req.command_id,
            actor_id=req.actor_id,
            terminal_id=req.terminal_id,
            tenant_id=req.tenant_id,
            budget={"items": req.max_items, "tokens": req.max_tokens},
            used_items=len(selected),
            used_tokens=sum(item.token_estimate for item in selected),
            truncated=truncated,
            **grouped,
        )
        snapshot = ContextSnapshot.from_context(context)
        self.repository.save_snapshot(snapshot)
        return context, snapshot
