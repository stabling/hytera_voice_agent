# =============================================================================
# 【维护注释】文件：voice_agent/capabilities/registry.py
# 所属层次：能力注册与执行门面
# 主要职责：把 MCP 工具和本地能力转换为稳定 CapabilitySpec，供规划、策略和执行共同使用，是后续扩展能力时的重要兼容边界。
# 项目内直接依赖：voice_agent, voice_agent.mcp_http_server.tool_catalog, .models
# 主要公开符号：CapabilityRegistry
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from voice_agent import config
from voice_agent.mcp_http_server.tool_catalog import ToolCatalog, ToolSpec

from .models import CapabilitySpec

_RISK_OVERRIDES = {
    "call_start": "R3",
    "call_answer": "R3",
    "call_end": "R3",
    "sms_send": "R3",
    "sms_play": "R3",
    "sms_delete": "R3",
    "sms_emergency_alert_send": "R4",
    "sms_location_send": "R3",
    "sms_status_send": "R3",
    "zone_channel_active_set": "R2",
    "zone_add": "R3",
    "zone_delete": "R3",
    "call_get_transcript": "R1",
    "call_summarize": "R1",
    "call_translate": "R1",
    "contact_resolve": "R0",
    "radio_resolve_zone_channel": "R0",
    "reminder_schedule": "R1",
}

_SERVER_OWNED = {
    "call_query": ("actor_terminal_id",),
    "call_get_transcript": ("actor_terminal_id",),
    "call_summarize": ("actor_terminal_id", "operation_id"),
    "call_translate": ("actor_terminal_id", "operation_id"),
    "history_recent_calls": ("actor_terminal_id",),
    "history_entity_timeline": ("actor_terminal_id",),
    "history_similar_outcomes": ("actor_terminal_id",),
    "history_open_tasks": ("actor_terminal_id",),
    "history_call_by_position": ("actor_terminal_id",),
    "history_recent_sms": ("actor_terminal_id",),
}


def _risk(spec: ToolSpec) -> str:
    if spec.name in _RISK_OVERRIDES:
        return _RISK_OVERRIDES[spec.name]
    if bool((spec.annotations or {}).get("readOnlyHint")):
        return "R0"
    return "R3" if str(spec.permission).upper() == "WRITE" else "R1"


def _side_effect(spec: ToolSpec) -> bool:
    annotations = spec.annotations or {}
    if annotations.get("readOnlyHint") is True:
        return False
    return str(spec.permission).upper() == "WRITE"


def _available(tool_name: str) -> tuple[bool, str | None]:
    if tool_name in {"call_query", "call_get_transcript", "call_summarize", "call_translate"}:
        if config.CALL_STORE_ENABLE == "false":
            return False, "call_store_disabled"
    return True, None


class CapabilityRegistry:
    """Single runtime registry used by planning, policy and execution.

    MCP YAML remains the source of truth for device/content tool schemas. Only
    agent-local primitives that do not exist in MCP are defined here.
    """

    def __init__(self, tool_catalog: ToolCatalog | None = None) -> None:
        self.tool_catalog = tool_catalog or ToolCatalog()
        self._specs = self._build()
        # 能力契约在进程生命周期内是静态的。预先缓存模型最小视图，避免每个请求重复
        # 构造同一批 schema；新增能力时只需重建 Registry，不影响旧调用方接口。
        self._planner_prompt_contracts = tuple(
            item.planner_prompt_contract() for item in self.specs(available_only=True)
        )
        self.catalog_hash = self._hash()

    def _build(self) -> dict[str, CapabilitySpec]:
        """完全从 ToolCatalog 构造 Capability；不再在 Python 中手工追加本地能力。"""
        result: dict[str, CapabilitySpec] = {}
        for tool in self.tool_catalog.specs():
            capability_id = tool.name
            available, reason = _available(capability_id)
            output_schema = self.tool_catalog.output_schema_for(tool)
            if tool.executor_kind == "local" and isinstance(output_schema, dict):
                output_schema = json.loads(json.dumps(output_schema, ensure_ascii=False))
                output_schema["additionalProperties"] = False
            result[capability_id] = CapabilitySpec(
                id=capability_id,
                description=tool.model_description,
                input_schema=self.tool_catalog.input_schema_for(tool),
                output_schema=output_schema,
                executor_kind="local" if tool.executor_kind == "local" else "mcp",
                risk_level=_risk(tool),
                side_effect=_side_effect(tool),
                available=available,
                unavailable_reason=reason,
                tool_name=capability_id,
                domain=tool.domain or tool.resource or "mcp",
                server_owned_arguments=_SERVER_OWNED.get(capability_id, ()),
                metadata={
                    "resource": tool.resource,
                    "permission": tool.permission,
                    "source_file": tool.source_file,
                    "executor_kind": tool.executor_kind,
                },
            )
        return dict(sorted(result.items()))

    def _hash(self) -> str:
        payload = {
            name: spec.planner_contract()
            for name, spec in sorted(self._specs.items())
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, capability_id: str) -> CapabilitySpec:
        try:
            return self._specs[str(capability_id)]
        except KeyError as exc:
            raise KeyError(f"unknown capability: {capability_id}") from exc

    def contains(self, capability_id: str) -> bool:
        return str(capability_id) in self._specs

    def specs(self, *, available_only: bool = False) -> list[CapabilitySpec]:
        values = list(self._specs.values())
        return [item for item in values if item.available] if available_only else values

    def planner_contracts(self) -> list[dict[str, Any]]:
        """返回完整规划契约；保留原接口，避免旧调用方受重构影响。"""

        return [item.planner_contract() for item in self.specs(available_only=True)]

    def planner_prompt_contracts(self) -> list[dict[str, Any]]:
        """返回供 LLM prompt 使用的缓存最小契约列表。

        返回新的 list 容器，防止调用方误修改 Registry 内部 tuple；其中每个字典按约定
        视为只读。执行时仍会依据完整 CapabilitySpec 再校验，因此该视图只优化上下文。
        """

        return list(self._planner_prompt_contracts)

    def domain_index(self) -> dict[str, tuple[str, ...]]:
        """按领域导出稳定能力索引，为未来分阶段/分域工具暴露提供兼容扩展点。"""

        grouped: dict[str, list[str]] = {}
        for item in self.specs(available_only=True):
            grouped.setdefault(item.domain, []).append(item.id)
        return {domain: tuple(sorted(ids)) for domain, ids in sorted(grouped.items())}

    def availability(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "available": item.available,
                "reason": item.unavailable_reason,
                "domain": item.domain,
            }
            for item in self.specs()
        ]

    def subset(self, ids: Iterable[str]) -> dict[str, CapabilitySpec]:
        return {item: self.get(item) for item in ids if self.contains(item)}
