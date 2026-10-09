# =============================================================================
# 【维护注释】文件：voice_agent/memory/models.py
# 所属层次：长期记忆层
# 主要职责：负责记忆候选、检索、验证、向量/全文索引与生命周期；记忆不能直接触发设备命令。
# 项目内直接依赖：shared_contracts.models
# 主要公开符号：MemoryType, MemoryStatus, MemorySensitivity, MemoryItem, MemoryTransition, transition_allowed
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Governed long-term-memory contracts."""

from __future__ import annotations

import json
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared_contracts.models import utc_now_iso


class MemoryType(str, Enum):
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL_CANDIDATE = "PROCEDURAL_CANDIDATE"


class MemoryStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"
    EXPIRED = "EXPIRED"
    RETIRED = "RETIRED"


class MemorySensitivity(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RESTRICTED = "RESTRICTED"
    SECRET = "SECRET"


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    memory_id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex}", min_length=1, max_length=192)
    namespace: str = Field(min_length=1, max_length=256)
    memory_type: MemoryType
    tenant_id: str | None = Field(default=None, max_length=192)
    actor_id: str | None = Field(default=None, max_length=192)
    terminal_id: str | None = Field(default=None, max_length=192)
    subject_entity_id: str | None = Field(default=None, max_length=192)
    predicate: str | None = Field(default=None, max_length=192)
    object: Any = None
    summary: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=100_000)
    confidence: float = Field(ge=0.0, le=1.0)
    verification_status: MemoryStatus = MemoryStatus.CANDIDATE
    valid_from: str | None = Field(default=None, max_length=64)
    valid_to: str | None = Field(default=None, max_length=64)
    expires_at: str | None = Field(default=None, max_length=64)
    evidence_event_ids: list[str] = Field(default_factory=list, max_length=512)
    source_type: str = Field(min_length=1, max_length=64)
    source_model: str | None = Field(default=None, max_length=256)
    source_operation_id: str | None = Field(default=None, max_length=192)
    source_call_id: str | None = Field(default=None, max_length=192)
    sensitivity: MemorySensitivity = MemorySensitivity.INTERNAL
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso, max_length=64)
    updated_at: str = Field(default_factory=utc_now_iso, max_length=64)

    @model_validator(mode="after")
    def _evidence_and_verification_rules(self) -> "MemoryItem":
        self.evidence_event_ids = sorted({str(item) for item in self.evidence_event_ids if str(item)})
        if self.source_type != "directory" and not self.evidence_event_ids:
            raise ValueError("non-directory memory requires evidence_event_ids")
        if self.memory_type == MemoryType.PROCEDURAL_CANDIDATE and self.verification_status == MemoryStatus.VERIFIED:
            raise ValueError("procedural candidates cannot become online VERIFIED behavior")
        return self

    @property
    def object_json(self) -> str:
        return json.dumps(self.object, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def conflict_key(self) -> str | None:
        if not self.subject_entity_id or not self.predicate:
            return None
        return f"{self.namespace}:{self.subject_entity_id}:{self.predicate}"


class MemoryTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    memory_id: str
    from_status: MemoryStatus
    to_status: MemoryStatus
    actor_role: str
    actor_id: str | None = None
    reason: str = Field(min_length=1, max_length=1000)
    occurred_at: str = Field(default_factory=utc_now_iso)


_ALLOWED_TRANSITIONS: dict[MemoryStatus, set[MemoryStatus]] = {
    MemoryStatus.CANDIDATE: {MemoryStatus.VERIFIED, MemoryStatus.DISPUTED, MemoryStatus.EXPIRED, MemoryStatus.RETIRED},
    MemoryStatus.VERIFIED: {MemoryStatus.DISPUTED, MemoryStatus.EXPIRED, MemoryStatus.RETIRED},
    MemoryStatus.DISPUTED: {MemoryStatus.VERIFIED, MemoryStatus.EXPIRED, MemoryStatus.RETIRED},
    MemoryStatus.EXPIRED: {MemoryStatus.RETIRED},
    MemoryStatus.RETIRED: set(),
}


def transition_allowed(source: MemoryStatus, target: MemoryStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[source]
