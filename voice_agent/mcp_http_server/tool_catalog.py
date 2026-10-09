# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/tool_catalog.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：.
# 主要公开符号：ToolField, ToolSpec, ToolCatalog
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Load the canonical MCP tool catalog from YAML and validate calls."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path
import threading
from typing import Any, Dict, Iterable, List, Optional

import yaml
from jsonschema import Draft202012Validator

from . import config


_JSON_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "double": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


@dataclass(frozen=True)
class ToolField:
    """工具输入字段定义。"""

    field: str
    type: str
    required: Any = False
    description: str = ""
    enum: Optional[List[Any]] = None
    default: Any = None
    minimum: Any = None
    maximum: Any = None
    enum_ref: Optional[str] = None


ENUM_DEFINITIONS_FILE = "enums.yaml"


def load_enum_registry(tool_defs_dir: Path | str) -> Dict[str, List[str]]:
    """加载公共枚举集 enums.yaml，返回 {枚举名: [取值名, ...]}（按 wire value 升序）。

    文件缺失时返回空注册表，保持旧行为（仅内联 enum）可用。
    """
    path = Path(tool_defs_dir) / ENUM_DEFINITIONS_FILE
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    registry: Dict[str, List[str]] = {}
    for entry in data.get("enums") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        values = entry.get("values") or []
        pairs = sorted(
            ((int(v.get("value", 0)), str(v.get("name", ""))) for v in values if isinstance(v, dict)),
            key=lambda pair: pair[0],
        )
        registry[str(entry["name"])] = [name for _, name in pairs]
    return registry


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的完整 YAML 规格。"""

    name: str
    resource: str
    permission: str
    description: Dict[str, Any]
    input: List[ToolField] = dc_field(default_factory=list)
    output: List[Dict[str, Any]] = dc_field(default_factory=list)
    annotations: Dict[str, Any] = dc_field(default_factory=dict)
    examples: Dict[str, Any] = dc_field(default_factory=dict)
    error_codes: List[Dict[str, Any]] = dc_field(default_factory=list)
    source_file: str = ""
    domain: str = "mcp"
    executor_kind: str = "mcp"
    constraints: Dict[str, Any] = dc_field(default_factory=dict)

    @property
    def function(self) -> str:
        return str((self.description or {}).get("function", "")).strip()

    @property
    def input_field_names(self) -> list[str]:
        return [x.field for x in self.input]

    @property
    def model_description(self) -> str:
        """组合适合模型选工具和填参数的紧凑说明。"""
        desc = self.description or {}
        parts: list[str] = []
        labels = (
            ("function", "功能"),
            ("precondition", "前置条件"),
            ("side_effect_and_idempotency", "副作用与幂等性"),
            ("recommended_tools", "推荐配合工具"),
        )
        for key, label in labels:
            value = str(desc.get(key, "") or "").strip()
            if value:
                parts.append(f"{label}：{value}")
        return "\n".join(parts) or self.name


class ToolCatalog:
    """Thread-safe in-memory tool catalog derived from canonical YAML."""

    def __init__(self, tool_defs_dir: Path | str | None = None) -> None:
        self.tool_defs_dir = Path(tool_defs_dir or config.TOOL_DEFS_DIR)
        self._lock = threading.RLock()
        self._tools: Dict[str, ToolSpec] = {}
        self._enum_registry: Dict[str, List[str]] = {}
        self._generation = 0
        self.reload()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @staticmethod
    def _resolve_enum_values(
        raw_field: dict[str, Any],
        enum_registry: Dict[str, List[str]] | None,
        context: str,
    ) -> tuple[Optional[List[Any]], Optional[str]]:
        """展开 enum_ref：优先内联 enum，否则用公共枚举集填充。"""
        inline = list(raw_field.get("enum") or []) or None
        ref = raw_field.get("enum_ref")
        if inline or not ref:
            return inline, (str(ref) if ref else None)
        if not enum_registry or str(ref) not in enum_registry:
            raise ValueError(
                f"{context}: unknown enum_ref {str(ref)!r}; "
                f"known enums: {sorted(enum_registry or {})}"
            )
        return list(enum_registry[str(ref)]), str(ref)

    @classmethod
    def _parse_paths(
        cls,
        paths: Iterable[Path],
        enum_registry: Dict[str, List[str]] | None = None,
    ) -> Dict[str, ToolSpec]:
        parsed: Dict[str, ToolSpec] = {}
        for path in sorted((Path(item).resolve() for item in paths), key=lambda item: str(item)):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{path}: YAML root must be an object")
            default_domain = str(data.get("domain") or path.stem).strip().lower()
            default_executor_kind = str(data.get("executor_kind") or "mcp").strip().lower()
            tools = data.get("tools") or []
            if not isinstance(tools, list):
                raise ValueError(f"{path}: 'tools' must be a list")
            for item in tools:
                if not isinstance(item, dict) or not item.get("name"):
                    raise ValueError(f"{path}: each tool must be an object with a non-empty name")
                name = str(item["name"]).strip()
                if name in parsed:
                    raise ValueError(f"Duplicate tool name {name!r}: {parsed[name].source_file} and {path.name}")
                fields: list[ToolField] = []
                seen_fields: set[str] = set()
                for raw_field in item.get("input") or []:
                    if not isinstance(raw_field, dict) or not raw_field.get("field"):
                        raise ValueError(f"{path}:{name}: invalid input field: {raw_field!r}")
                    field_name = str(raw_field["field"]).strip()
                    if field_name in seen_fields:
                        raise ValueError(f"{path}:{name}: duplicate input field {field_name!r}")
                    seen_fields.add(field_name)
                    field_type = str(raw_field.get("type", "string")).strip().lower()
                    if field_type not in _JSON_TYPE_MAP:
                        raise ValueError(f"{path}:{name}.{field_name}: unsupported YAML type {field_type!r}")
                    enum_values, enum_ref = cls._resolve_enum_values(
                        raw_field, enum_registry, f"{path}:{name}.{field_name}"
                    )
                    fields.append(ToolField(
                        field=field_name,
                        type=field_type,
                        required=raw_field.get("required", False),
                        description=str(raw_field.get("description", "") or ""),
                        enum=enum_values,
                        default=raw_field.get("default"),
                        minimum=raw_field.get("min"),
                        maximum=raw_field.get("max"),
                        enum_ref=enum_ref,
                    ))
                # 输出字段同样支持 enum_ref：解析时展开为内联 enum，保持
                # output_schema_for 逻辑不变。
                normalized_output: List[Dict[str, Any]] = []
                for raw_output in item.get("output") or []:
                    if not isinstance(raw_output, dict):
                        continue
                    entry = dict(raw_output)
                    enum_values, _ = cls._resolve_enum_values(
                        entry, enum_registry, f"{path}:{name}.output.{entry.get('field', '')}"
                    )
                    if enum_values:
                        entry["enum"] = enum_values
                    normalized_output.append(entry)

                domain = str(item.get("domain") or default_domain or item.get("resource") or "mcp").strip().lower()
                parsed[name] = ToolSpec(
                    name=name,
                    resource=str(item.get("resource", "") or ""),
                    permission=str(item.get("permission", "") or ""),
                    description=item.get("description") or {},
                    input=fields,
                    output=normalized_output,
                    annotations=item.get("annotations") or {},
                    examples=item.get("examples") or {},
                    error_codes=item.get("error_codes") or [],
                    source_file=path.name,
                    domain=domain,
                    executor_kind=str(item.get("executor_kind") or default_executor_kind or "mcp").strip().lower(),
                    constraints=dict(item.get("constraints") or {}),
                )
        return parsed

    def reload(self) -> None:
        if not self.tool_defs_dir.exists():
            raise FileNotFoundError(f"TOOL_DEFS_DIR not found: {self.tool_defs_dir}")
        enum_registry = load_enum_registry(self.tool_defs_dir)
        # enums.yaml 是公共枚举定义文件，不是工具文件，跳过工具解析。
        paths = sorted(
            path for path in self.tool_defs_dir.glob("*.yaml")
            if path.name != ENUM_DEFINITIONS_FILE
        )
        if not paths:
            raise FileNotFoundError(f"No YAML tool definitions found in: {self.tool_defs_dir}")
        candidate = self._parse_paths(paths, enum_registry)
        with self._lock:
            self._enum_registry = enum_registry
            self._tools = dict(sorted(candidate.items()))
            self._generation += 1

    def enum_values(self, enum_name: str) -> List[str]:
        """返回公共枚举集取值（wire value 升序）；未知枚举返回空列表。"""
        with self._lock:
            return list(self._enum_registry.get(str(enum_name), ()))

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._tools))
            raise ValueError(f"Unknown tool {name!r}. Known tools: {known}") from exc

    def contains(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, names: Iterable[str] | None = None) -> list[ToolSpec]:
        if names is None:
            return [self._tools[name] for name in self.names()]
        return [self.get(name) for name in names]

    # ------------------------------------------------------------------
    # YAML -> JSON Schema
    # ------------------------------------------------------------------

    @staticmethod
    def _field_schema(field: ToolField) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": _JSON_TYPE_MAP[field.type],
        }
        if field.description:
            schema["description"] = field.description
        if field.enum:
            schema["enum"] = list(field.enum)
        if field.default is not None:
            schema["default"] = field.default
        if field.minimum is not None:
            if field.type in {"integer", "number", "float", "double"}:
                schema["minimum"] = field.minimum
            elif field.type == "string":
                schema["minLength"] = int(field.minimum)
            elif field.type == "array":
                schema["minItems"] = int(field.minimum)
        if field.maximum is not None:
            if field.type in {"integer", "number", "float", "double"}:
                schema["maximum"] = field.maximum
            elif field.type == "string":
                schema["maxLength"] = int(field.maximum)
            elif field.type == "array":
                schema["maxItems"] = int(field.maximum)
        if field.type == "array":
            schema.setdefault("items", {})
        return schema

    @classmethod
    def input_schema_for(cls, spec: ToolSpec) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []

        # 先建立顶层字段；items[].resource 这类路径不是顶层参数。
        for field in spec.input:
            if "[]." in field.field:
                continue
            properties[field.field] = cls._field_schema(field)
            if field.required is True:
                required.append(field.field)

        # 再把 items[].xxx 合并到标准 JSON Schema 的 array.items.object 中。
        for field in spec.input:
            if "[]." not in field.field:
                continue
            parent, child_path = field.field.split("[].", 1)
            parent_schema = properties.setdefault(parent, {"type": "array", "items": {}})
            parent_schema["type"] = "array"
            item_schema = parent_schema.setdefault("items", {})
            if not isinstance(item_schema, dict):
                item_schema = {}
                parent_schema["items"] = item_schema
            item_schema.setdefault("type", "object")
            item_schema.setdefault("properties", {})
            item_schema.setdefault("additionalProperties", False)
            # 当前 tools 只使用一层 items[].field；若未来出现更深路径，明确拒绝，避免静默误解。
            if "." in child_path or "[]" in child_path:
                raise ValueError(f"{spec.name}: unsupported nested input field path {field.field!r}")
            item_schema["properties"][child_path] = cls._field_schema(field)
            if field.required is True:
                item_schema.setdefault("required", []).append(child_path)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required

        # 跨字段约束也由 tools YAML 的统一 constraints 元数据驱动。
        constraints = spec.constraints or {}
        at_least_one = constraints.get("at_least_one")
        if isinstance(at_least_one, list) and at_least_one:
            schema["anyOf"] = [
                {"required": [str(field_name)]}
                for field_name in at_least_one
                if str(field_name) in properties
            ]

        conditional_required = constraints.get("conditional_required") or []
        if isinstance(conditional_required, list):
            for rule in conditional_required:
                if not isinstance(rule, dict):
                    continue
                when = rule.get("when") or {}
                field_name = str(when.get("field") or "").strip()
                values = list(when.get("in") or [])
                required_fields = [str(x) for x in (rule.get("required") or [])]
                if not field_name or not values or not required_fields:
                    continue
                schema.setdefault("allOf", []).append(
                    {
                        "if": {
                            "properties": {field_name: {"enum": values}},
                            "required": [field_name],
                        },
                        "then": {"required": required_fields},
                    }
                )
        return schema

    @staticmethod
    def _output_leaf_schema(raw: dict[str, Any]) -> dict[str, Any]:
        yaml_type = str(raw.get("type", "string") or "string").strip().lower()
        schema: dict[str, Any] = {"type": _JSON_TYPE_MAP.get(yaml_type, "string")}
        description = str(raw.get("description", "") or "").strip()
        if description:
            schema["description"] = description
        if raw.get("enum"):
            schema["enum"] = list(raw["enum"])
        if yaml_type == "array":
            schema["items"] = {}
        return schema

    @classmethod
    def output_schema_for(cls, spec: ToolSpec) -> dict[str, Any] | None:
        if not spec.output:
            return None

        root: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": True}
        for raw in spec.output:
            field_path = str(raw.get("field", "") or "").strip()
            if not field_path:
                continue
            parts = field_path.split(".")
            node = root
            for index, part in enumerate(parts):
                is_array = part.endswith("[]")
                key = part[:-2] if is_array else part
                is_last = index == len(parts) - 1
                properties = node.setdefault("properties", {})

                if is_last:
                    leaf = cls._output_leaf_schema(raw)
                    if is_array:
                        leaf = {"type": "array", "items": leaf}
                    properties[key] = leaf
                    continue

                if key not in properties:
                    if is_array:
                        properties[key] = {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": True,
                            },
                        }
                    else:
                        properties[key] = {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        }
                node = properties[key]["items"] if is_array else properties[key]
        return root

    # ------------------------------------------------------------------
    # 多协议工具定义
    # ------------------------------------------------------------------

    def mcp_tool_definitions(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for spec in self.specs(names):
            item: dict[str, Any] = {
                "name": spec.name,
                "description": spec.model_description,
                "inputSchema": self.input_schema_for(spec),
            }
            output_schema = self.output_schema_for(spec)
            if output_schema is not None:
                item["outputSchema"] = output_schema
            if spec.annotations:
                item["annotations"] = dict(spec.annotations)
            definitions.append(item)
        return definitions

    def validate_arguments(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """以 tools YAML 生成的 JSON Schema 为唯一参数契约并做轻量类型归一化。"""
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError(f"{tool_name}.arguments must be an object")

        spec = self.get(tool_name)
        normalized: Dict[str, Any] = dict(arguments)
        direct_fields = {field.field: field for field in spec.input if "[]." not in field.field}
        nested_fields: dict[str, dict[str, ToolField]] = {}
        for field in spec.input:
            if "[]." in field.field:
                parent, child = field.field.split("[].", 1)
                nested_fields.setdefault(parent, {})[child] = field

        unknown = sorted(set(normalized) - set(direct_fields))
        if unknown:
            raise ValueError(
                f"{tool_name} got unknown argument(s): {unknown}; allowed={sorted(direct_fields)}"
            )

        for name, field in direct_fields.items():
            if name not in normalized or normalized.get(name) is None:
                continue
            normalized[name] = self._coerce_basic(field, normalized[name])

        for parent, children in nested_fields.items():
            if parent not in normalized or normalized.get(parent) is None:
                continue
            items = normalized[parent]
            if not isinstance(items, list):
                raise ValueError(f"{tool_name}.{parent} must be array")
            normalized_items: list[Any] = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"{tool_name}.{parent}[{index}] must be object")
                unknown_child = sorted(set(item) - set(children))
                if unknown_child:
                    raise ValueError(
                        f"{tool_name}.{parent}[{index}] got unknown field(s): {unknown_child}; "
                        f"allowed={sorted(children)}"
                    )
                item_copy = dict(item)
                for child_name, child_field in children.items():
                    if child_name in item_copy and item_copy[child_name] is not None:
                        item_copy[child_name] = self._coerce_basic(child_field, item_copy[child_name])
                normalized_items.append(item_copy)
            normalized[parent] = normalized_items

        schema = self.input_schema_for(spec)
        errors = sorted(Draft202012Validator(schema).iter_errors(normalized), key=lambda exc: list(exc.path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path)
            where = f"{tool_name}.{path}" if path else tool_name
            raise ValueError(f"{where}: {error.message}")
        return normalized

    @staticmethod
    def _coerce_basic(field: ToolField, value: Any) -> Any:
        if value is None:
            return None
        if field.type == "integer":
            if isinstance(value, bool):
                raise ValueError(f"{field.field} must be integer, got bool")
            return int(value)
        if field.type in {"number", "float", "double"}:
            if isinstance(value, bool):
                raise ValueError(f"{field.field} must be number, got bool")
            return float(value)
        if field.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lower = value.strip().lower()
                if lower in {"true", "1", "yes", "y", "on"}:
                    return True
                if lower in {"false", "0", "no", "n", "off"}:
                    return False
            raise ValueError(f"{field.field} must be boolean, got {value!r}")
        if field.type == "string":
            if isinstance(value, (dict, list)):
                raise ValueError(f"{field.field} must be string, got {type(value).__name__}")
            return str(value)
        if field.type == "array":
            if not isinstance(value, list):
                raise ValueError(f"{field.field} must be array, got {type(value).__name__}")
            return value
        if field.type == "object":
            if not isinstance(value, dict):
                raise ValueError(f"{field.field} must be object, got {type(value).__name__}")
            return value
        return value

