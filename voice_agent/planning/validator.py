# =============================================================================
# 【维护注释】文件：voice_agent/planning/validator.py
# 所属层次：计划治理层
# 主要职责：负责计划模型、校验、持久化、策略衔接、等待条件和计划执行前治理。
# 项目内直接依赖：voice_agent.capabilities, .models
# 主要公开符号：ValidationIssue, PlanValidationError, PlanValidator
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from voice_agent.capabilities import CapabilityRegistry
from .models import OperationPlan, PlanStep

_REF_RE = re.compile(r"^\$\{([A-Za-z0-9_.-]+)\.([A-Za-z0-9_.-]+)\}$")
_FORBIDDEN = {"url", "uri", "endpoint", "host", "hostname", "script", "python", "code", "tool", "tool_name"}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    step_id: str | None = None
    path: str | None = None


class PlanValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(item.message for item in self.issues))


class PlanValidator:
    def __init__(self, registry: CapabilityRegistry, *, max_steps: int = 8, max_replans: int = 2, max_model_calls: int = 3, total_timeout_seconds: float = 120.0) -> None:
        self.registry = registry
        self.max_steps = max(1, int(max_steps))
        self.max_replans = max(0, int(max_replans))
        self.max_model_calls = max(0, int(max_model_calls))
        self.total_timeout_seconds = max(0.1, float(total_timeout_seconds))

    def validate(self, plan: OperationPlan) -> OperationPlan:
        issues: list[ValidationIssue] = []
        if len(plan.steps) > min(self.max_steps, plan.limits.max_steps):
            issues.append(ValidationIssue("PLAN_STEP_BUDGET", "plan exceeds step budget"))
        if plan.limits.timeout_seconds > self.total_timeout_seconds:
            issues.append(ValidationIssue("PLAN_TIMEOUT_BUDGET", "plan exceeds total timeout"))
        if plan.capability_catalog_hash and plan.capability_catalog_hash != self.registry.catalog_hash:
            issues.append(ValidationIssue("CATALOG_HASH_MISMATCH", "capability catalog changed"))
        step_map = {step.step_id: step for step in plan.steps}
        order = {step.step_id: index for index, step in enumerate(plan.steps)}
        for step in plan.steps:
            self._validate_step(step, step_map, order, issues)
        self._validate_dag(step_map, issues)
        if issues:
            raise PlanValidationError(issues)
        return plan.model_copy(update={"status": "VALIDATED"})

    def _validate_step(self, step: PlanStep, step_map: dict[str, PlanStep], order: dict[str, int], issues: list[ValidationIssue]) -> None:
        if not self.registry.contains(step.capability):
            issues.append(ValidationIssue("UNKNOWN_CAPABILITY", f"unknown capability: {step.capability}", step.step_id))
            return
        spec = self.registry.get(step.capability)
        if not spec.available:
            issues.append(ValidationIssue("CAPABILITY_UNAVAILABLE", f"capability unavailable: {step.capability}: {spec.unavailable_reason}", step.step_id))
        for dep in step.depends_on:
            if dep not in step_map:
                issues.append(ValidationIssue("UNKNOWN_DEPENDENCY", f"unknown dependency: {dep}", step.step_id))
            elif order[dep] >= order[step.step_id]:
                issues.append(ValidationIssue("FUTURE_DEPENDENCY", f"dependency must precede step: {dep}", step.step_id))
        self._validate_arguments(step, spec.planner_contract()["input_schema"], step_map, order, issues)

    def _validate_arguments(self, step: PlanStep, schema: dict[str, Any], step_map: dict[str, PlanStep], order: dict[str, int], issues: list[ValidationIssue]) -> None:
        def walk(value: Any, path: str, expected: dict[str, Any] | None = None) -> Any:
            expected = expected or {}
            if isinstance(value, dict):
                props = expected.get("properties") if isinstance(expected, dict) else {}
                result = {}
                for key, child in value.items():
                    normalized = str(key).lower()
                    if normalized in _FORBIDDEN or normalized.endswith("_url") or normalized.endswith("_uri"):
                        issues.append(ValidationIssue("FORBIDDEN_ARGUMENT", f"planner cannot supply {key}", step.step_id, path + "." + str(key)))
                    result[key] = walk(child, path + "." + str(key), (props or {}).get(key, {}))
                return result
            if isinstance(value, list):
                item_schema = expected.get("items") if isinstance(expected, dict) else {}
                return [walk(child, f"{path}[{i}]", item_schema or {}) for i, child in enumerate(value)]
            if isinstance(value, str) and value.startswith("${"):
                match = _REF_RE.fullmatch(value)
                if match is None:
                    issues.append(ValidationIssue("INVALID_REFERENCE", f"invalid reference: {value}", step.step_id, path))
                    return value
                ref_step = match.group(1)
                if ref_step not in step_map or order.get(ref_step, 10**9) >= order[step.step_id]:
                    issues.append(ValidationIssue("INVALID_REFERENCE", f"reference must point to earlier step: {value}", step.step_id, path))
                elif ref_step not in step.depends_on:
                    issues.append(ValidationIssue("UNDECLARED_REFERENCE_DEPENDENCY", f"referenced step must be in depends_on: {ref_step}", step.step_id, path))
                else:
                    source_step = step_map[ref_step]
                    if self.registry.contains(source_step.capability):
                        source_spec = self.registry.get(source_step.capability)
                        declared = source_spec.output_path_declared(match.group(2))
                        if declared is False:
                            issues.append(ValidationIssue(
                                "UNKNOWN_REFERENCE_OUTPUT",
                                f"referenced output is not declared by {source_step.capability}: {value}",
                                step.step_id,
                                path,
                            ))
                enum_values = expected.get("enum") if isinstance(expected, dict) else None
                if isinstance(enum_values, list) and enum_values:
                    return enum_values[0]
                typ = expected.get("type") if isinstance(expected, dict) else None
                if isinstance(typ, list):
                    typ = next((x for x in typ if x != "null"), None)
                return {"integer": 1, "number": 1.0, "boolean": True, "array": [], "object": {}}.get(typ, "ref")
            return value
        candidate = walk(step.arguments, "arguments", schema)
        for error in Draft202012Validator(schema).iter_errors(candidate):
            issues.append(ValidationIssue("ARGUMENT_SCHEMA", error.message, step.step_id, ".".join(str(x) for x in error.path)))

    @staticmethod
    def _validate_dag(step_map: dict[str, PlanStep], issues: list[ValidationIssue]) -> None:
        visiting, done = set(), set()
        def visit(step_id: str) -> None:
            if step_id in done:
                return
            if step_id in visiting:
                issues.append(ValidationIssue("CYCLE", f"dependency cycle at {step_id}", step_id)); return
            visiting.add(step_id)
            for dep in step_map[step_id].depends_on:
                if dep in step_map: visit(dep)
            visiting.remove(step_id); done.add(step_id)
        for step_id in step_map: visit(step_id)
