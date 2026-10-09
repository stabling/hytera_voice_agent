# =============================================================================
# 【维护注释】文件：voice_agent/context/models.py
# 所属层次：决策上下文层
# 主要职责：负责事件、历史、终端状态等上下文的构建、裁剪、脱敏和快照，为规划器提供受控信息。
# 项目内直接依赖：shared_contracts.models, shared_contracts.identifiers
# 主要公开符号：ContextSection, Sensitivity, ContextItem, DecisionContext, ContextSnapshot, ContextBuildRequest
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Typed contracts for bounded, replayable decision context."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared_contracts.models import utc_now_iso
from shared_contracts.identifiers import new_event_id


class ContextSection(str, Enum):
    CURRENT_REQUEST = "current_request"
    CURRENT_TERMINAL = "current_terminal"
    AUTHORITATIVE_DIRECTORY = "authoritative_directory"
    ACTIVE_CALL = "active_call"
    RECENT_CALLS = "recent_calls"
    RECENT_OPERATIONS = "recent_operations"
    VERIFIED_MEMORIES = "verified_memories"
    OPEN_TASKS = "open_tasks"
    AVAILABLE_CAPABILITIES = "available_capabilities"
    POLICY_CONSTRAINTS = "policy_constraints"
    UNRESOLVED_CONFLICTS = "unresolved_conflicts"


class Sensitivity(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RESTRICTED = "RESTRICTED"
    SECRET = "SECRET"


class ContextItem(BaseModel):
    """One independently attributable item supplied to planning."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    item_id: str = Field(min_length=1, max_length=192)
    section: ContextSection
    source_type: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=192)
    payload: dict[str, Any] = Field(default_factory=dict)
    authority: int = Field(ge=0, le=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    freshness: float = Field(default=1.0, ge=0.0, le=1.0)
    observed_at: str | None = Field(default=None, max_length=64)
    expires_at: str | None = Field(default=None, max_length=64)
    evidence_event_ids: list[str] = Field(default_factory=list, max_length=256)
    memory_id: str | None = Field(default=None, max_length=192)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    redacted: bool = False
    untrusted_content: bool = False
    conflict_key: str | None = Field(default=None, max_length=256)
    rank_score: float = 0.0
    token_estimate: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _normalize_evidence(self) -> "ContextItem":
        self.evidence_event_ids = sorted({str(value) for value in self.evidence_event_ids if str(value)})
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"rank_score"})


class DecisionContext(BaseModel):
    """The exact bounded context available to one command's planner."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    command_id: str | None = Field(default=None, max_length=192)
    actor_id: str | None = Field(default=None, max_length=192)
    terminal_id: str | None = Field(default=None, max_length=192)
    tenant_id: str | None = Field(default=None, max_length=192)
    current_request: list[ContextItem] = Field(default_factory=list)
    current_terminal: list[ContextItem] = Field(default_factory=list)
    authoritative_directory: list[ContextItem] = Field(default_factory=list)
    active_call: list[ContextItem] = Field(default_factory=list)
    recent_calls: list[ContextItem] = Field(default_factory=list)
    recent_operations: list[ContextItem] = Field(default_factory=list)
    verified_memories: list[ContextItem] = Field(default_factory=list)
    open_tasks: list[ContextItem] = Field(default_factory=list)
    available_capabilities: list[ContextItem] = Field(default_factory=list)
    policy_constraints: list[ContextItem] = Field(default_factory=list)
    unresolved_conflicts: list[ContextItem] = Field(default_factory=list)
    budget: dict[str, int] = Field(default_factory=lambda: {"items": 30, "tokens": 3000})
    used_items: int = Field(default=0, ge=0)
    used_tokens: int = Field(default=0, ge=0)
    truncated: bool = False
    built_at: str = Field(default_factory=utc_now_iso, max_length=64)

    def all_items(self) -> list[ContextItem]:
        values: list[ContextItem] = []
        for section in ContextSection:
            values.extend(getattr(self, section.value))
        return values

    def planner_payload(self) -> dict[str, Any]:
        """Return the bounded context view that is actually useful to planning.

        The full ``DecisionContext`` is still retained in ``ContextSnapshot`` for
        replay/audit.  This projection intentionally removes data already supplied
        elsewhere in the Planner request:

        * ``current_request`` is duplicated by the top-level ``request`` field.
        * ``available_capabilities`` is duplicated by the authoritative capability
          contracts supplied by ``CapabilityRegistry``.
        * budget/build bookkeeping is useful for observability, not semantic planning.

        ``policy_constraints`` remains in this projection so the transport layer can
        move its stable payload into the cached static prefix without coupling the
        context builder to the model transport format.
        """
        data: dict[str, Any] = {
            key: value
            for key, value in (
                ("command_id", self.command_id),
                ("actor_id", self.actor_id),
                ("terminal_id", self.terminal_id),
                ("tenant_id", self.tenant_id),
            )
            if value is not None
        }
        for section in (
            ContextSection.CURRENT_TERMINAL,
            ContextSection.AUTHORITATIVE_DIRECTORY,
            ContextSection.ACTIVE_CALL,
            ContextSection.RECENT_CALLS,
            ContextSection.RECENT_OPERATIONS,
            ContextSection.VERIFIED_MEMORIES,
            ContextSection.OPEN_TASKS,
            ContextSection.POLICY_CONSTRAINTS,
            ContextSection.UNRESOLVED_CONFLICTS,
        ):
            values = getattr(self, section.value)
            if values:
                data[section.value] = [item.canonical_payload() for item in values]
        return data

    def content_hash(self) -> str:
        raw = json.dumps(
            self.planner_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class ContextSnapshot(BaseModel):
    """Replay record of what was actually supplied to planning."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    snapshot_id: str = Field(default_factory=lambda: f"ctx_{new_event_id()}", min_length=1, max_length=192)
    command_id: str | None = Field(default=None, max_length=192)
    actor_id: str | None = Field(default=None, max_length=192)
    terminal_id: str | None = Field(default=None, max_length=192)
    tenant_id: str | None = Field(default=None, max_length=192)
    item_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    content_hash: str = Field(min_length=64, max_length=64)
    context: DecisionContext
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)

    @classmethod
    def from_context(cls, context: DecisionContext) -> "ContextSnapshot":
        items = context.all_items()
        return cls(
            command_id=context.command_id,
            actor_id=context.actor_id,
            terminal_id=context.terminal_id,
            tenant_id=context.tenant_id,
            item_ids=sorted({item.item_id for item in items}),
            event_ids=sorted({event_id for item in items for event_id in item.evidence_event_ids}),
            memory_ids=sorted({item.memory_id for item in items if item.memory_id}),
            content_hash=context.content_hash(),
            context=context,
        )


class ContextBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    command_id: str | None = None
    request_id: str | None = None
    actor_id: str | None = None
    terminal_id: str | None = None
    tenant_id: str | None = None
    request_text: str = ""
    terminal_state: dict[str, Any] = Field(default_factory=dict)
    authoritative_directory: dict[str, Any] = Field(default_factory=dict)
    available_capabilities: list[dict[str, Any]] = Field(default_factory=list)
    policy_constraints: list[dict[str, Any]] = Field(default_factory=list)
    access_level: Sensitivity = Sensitivity.INTERNAL
    max_items: int = Field(default=30, ge=1, le=500)
    max_tokens: int = Field(default=3000, ge=128, le=100_000)
    recent_call_days: int = Field(default=7, ge=1, le=3650)
    semantic_query: str | None = None
    mode: Literal["shadow", "active"] = "shadow"
