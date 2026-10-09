# =============================================================================
# 【维护注释】文件：voice_agent/capabilities/models.py
# 所属层次：能力注册与执行门面
# 主要职责：把 MCP 工具和本地能力转换为稳定 CapabilitySpec，供规划、策略和执行共同使用，是后续扩展能力时的重要兼容边界。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：CapabilitySpec
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    executor_kind: str = "mcp"
    risk_level: str = "R1"
    side_effect: bool = False
    available: bool = True
    unavailable_reason: str | None = None
    tool_name: str | None = None
    domain: str = "general"
    server_owned_arguments: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def planner_contract(self) -> dict[str, Any]:
        """返回完整的规划契约。

        该结构同时供策略、校验、诊断和兼容 API 使用，因此保留执行器类型、风险等级、
        副作用标记和领域信息。不要为了缩短 LLM prompt 直接删减这里的字段；模型侧应使用
        :meth:`planner_prompt_contract`，从而把“内部完整契约”和“模型最小视图”解耦。
        """

        schema = self._planner_input_schema()
        return {
            "id": self.id,
            "description": self.description,
            "input_schema": schema,
            "output_schema": self.output_schema,
            "executor_kind": self.executor_kind,
            "risk_level": self.risk_level,
            "side_effect": self.side_effect,
            "domain": self.domain,
        }

    def planner_prompt_contract(self) -> dict[str, Any]:
        """返回给大模型的最小能力视图。

        模型只需要知道“能力名称、用途、可填写参数”。风险级别、执行器类型和副作用由
        服务端策略引擎负责，继续塞进 prompt 既增加 token，也容易让模型承担本不属于它的
        执行决策。该最小视图不会改变后续服务端 schema 校验，因此兼容旧工具行为。
        """

        result = {
            "id": self.id,
            "description": self.description,
            "input_schema": self._planner_input_schema(),
        }
        output_fields = self._planner_output_fields()
        if output_fields:
            result["output_fields"] = output_fields
        return result

    def _planner_output_fields(self) -> dict[str, Any]:
        """提取可被后续步骤引用的顶层输出字段，避免把完整输出 Schema 塞进提示词。"""

        schema = self.output_schema or {}
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            return {}
        fields: dict[str, Any] = {}
        for name, raw in properties.items():
            item = raw if isinstance(raw, dict) else {}
            compact: dict[str, Any] = {}
            if item.get("type") is not None:
                compact["type"] = item.get("type")
            if item.get("enum"):
                compact["enum"] = list(item["enum"])
            description = str(item.get("description") or "").strip()
            if description:
                compact["description"] = description
            fields[str(name)] = compact or {"type": "any"}
        return fields

    def output_path_declared(self, path: str) -> bool | None:
        """判断跨步骤引用路径是否由输出契约明确声明。

        返回 ``True`` 表示字段可引用，``False`` 表示契约明确不存在该字段，
        ``None`` 表示旧能力没有足够输出 Schema，保持向后兼容并交给运行时处理。
        """

        parts = [part for part in str(path or "").split(".") if part]
        if not parts:
            return False
        schema: Any = self.output_schema
        if not isinstance(schema, dict) or not schema:
            return None

        current: Any = schema
        for part in parts:
            if not isinstance(current, dict):
                return None
            properties = current.get("properties")
            additional = current.get("additionalProperties")
            if isinstance(properties, dict) and part in properties:
                current = properties[part]
                continue
            if isinstance(additional, dict):
                current = additional
                continue
            if additional is True:
                return None
            if isinstance(properties, dict):
                # 一旦能力提供了显式 properties，就把它视为可引用字段白名单；
                # 这比 JSON Schema 默认允许额外字段更适合作为计划间数据契约。
                return False
            return None
        return True

    def _planner_input_schema(self) -> dict[str, Any]:
        """生成剔除服务端注入字段后的模型输入 schema。"""

        schema = dict(self.input_schema)
        properties = dict(schema.get("properties") or {})
        required = list(schema.get("required") or [])
        for key in self.server_owned_arguments:
            properties.pop(key, None)
            required = [item for item in required if item != key]
        schema["properties"] = properties
        schema["required"] = required
        schema["additionalProperties"] = False
        return schema
