# -*- coding: utf-8 -*-
"""通过真实 /text/input 链路驱动的隔离模拟运行时。

模拟边界：Planner、MCP/网关、终端投递、外部内容服务均由确定性夹具替代；
生产代码中的 CapabilityRegistry、Schema 校验、计划治理、OperationExecutor、
LangGraph 节点、上下文/记忆与 HTTP 接口保持真实执行。底层 ADK 可按配置切换
为 passthrough，但测试默认 mock，避免误操作真实终端。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


@dataclass(frozen=True)
class SimulationScenario:
    scenario_id: str
    text: str
    plan: dict[str, Any]
    tool_outputs: dict[str, dict[str, Any]]
    expected: dict[str, Any]
    tags: tuple[str, ...] = ()


class SimulationScenarioCatalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        raw = data.get("scenarios") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise ValueError("simulation scenarios must contain a scenarios list")
        self._by_text: dict[str, SimulationScenario] = {}
        self._by_id: dict[str, SimulationScenario] = {}
        self._local = threading.local()
        for item in raw:
            scenario = SimulationScenario(
                scenario_id=str(item["id"]),
                text=str(item["text"]),
                plan=dict(item["plan"]),
                tool_outputs={str(k): dict(v) for k, v in (item.get("tool_outputs") or {}).items()},
                expected=dict(item.get("expected") or {}),
                tags=tuple(str(x) for x in (item.get("tags") or [])),
            )
            if scenario.text in self._by_text or scenario.scenario_id in self._by_id:
                raise ValueError(f"duplicate simulation scenario: {scenario.scenario_id}")
            self._by_text[scenario.text] = scenario
            self._by_id[scenario.scenario_id] = scenario

    def by_text(self, text: str) -> SimulationScenario:
        try:
            return self._by_text[str(text).strip()]
        except KeyError as exc:
            raise ValueError(f"simulation scenario not found for text: {text!r}") from exc

    def by_id(self, scenario_id: str) -> SimulationScenario:
        return self._by_id[str(scenario_id)]

    def bind_text(self, text: str) -> SimulationScenario:
        scenario = self.by_text(text)
        self._local.current = scenario
        return scenario

    def current(self) -> SimulationScenario:
        scenario = getattr(self._local, "current", None)
        if scenario is None:
            raise RuntimeError("simulation scenario is not bound to current request")
        return scenario

    def all(self) -> list[SimulationScenario]:
        return list(self._by_id.values())


class SimulationPlannerModel:
    def __init__(self, catalog: SimulationScenarioCatalog) -> None:
        self.catalog = catalog

    def __call__(self, prompt: dict[str, Any]) -> dict[str, Any]:
        scenario = self.catalog.bind_text(str(prompt.get("request") or ""))
        return json.loads(json.dumps(scenario.plan, ensure_ascii=False))


class SimulatedToolExecutor:
    """保持 MCP 返回外形不变，只把终端/网关执行替换为确定性结果。"""

    def __init__(self, catalog: SimulationScenarioCatalog, registry: Any) -> None:
        self.catalog = catalog
        self.registry = registry
    def _scenario(self) -> SimulationScenario:
        return self.catalog.current()

    def execute_mcp(self, tool_name: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        spec = self.registry.get(tool_name)
        errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            return {"ok": False, "error": errors[0].message, "error_type": "validation_error"}
        scenario = self._scenario()
        output = dict(scenario.tool_outputs.get(tool_name) or _default_output(spec.output_schema))
        return {
            "ok": True,
            "status_code": 200,
            "mcp_response": {
                "jsonrpc": "2.0",
                "id": scenario.scenario_id,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "structuredContent": output,
                },
            },
            "result": output,
            "simulation": True,
        }

    def execute(self, capability_id: str, arguments: dict[str, Any], operation: Any, **kwargs: Any) -> dict[str, Any]:
        """兼容 LocalCapabilityExecutor.execute 的生产接口。"""
        return self.execute_local(capability_id, arguments, operation, **kwargs)

    def execute_local(self, capability_id: str, arguments: dict[str, Any], operation: Any, **_: Any) -> dict[str, Any]:
        spec = self.registry.get(capability_id)
        errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            return {"ok": False, "error": errors[0].message, "error_type": "validation_error"}
        scenario = self._scenario()
        output = dict(scenario.tool_outputs.get(capability_id) or _default_output(spec.output_schema))
        return {"ok": True, "result": output, "simulation": True}

    @staticmethod
    def deliver(*, terminal_id: int, content: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "simulation": True,
            "terminal_id": terminal_id,
            "content": content,
            "accepted_by_simulator": True,
            **{k: v for k, v in kwargs.items() if k in {"channel_id", "request_id"}},
        }


def _default_output(schema: dict[str, Any] | None) -> dict[str, Any]:
    schema = schema or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return {"code": 0}
    return {name: _sample_value(field, name=name) for name, field in properties.items()}


def _sample_value(schema: dict[str, Any], *, name: str = "value") -> Any:
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((x for x in kind if x != "null"), "string")
    if kind == "integer":
        if name == "code": return 0
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1.0))
    if kind == "boolean": return True
    if kind == "array": return []
    if kind == "object":
        props = schema.get("properties") or {}
        return {k: _sample_value(v, name=k) for k, v in props.items()}
    return f"sim_{name}"


class SimulationRuntime:
    def __init__(self, *, scenario_path: str | Path, registry: Any) -> None:
        self.catalog = SimulationScenarioCatalog(scenario_path)
        self.planner_model = SimulationPlannerModel(self.catalog)
        self.executor = SimulatedToolExecutor(self.catalog, registry)
