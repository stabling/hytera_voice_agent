# =============================================================================
# 【维护注释】文件：voice_agent/reasoning/planner.py
# 所属层次：语义规划层
# 主要职责：把自然语言请求转换为受 CapabilityRegistry 约束的能力计划；模型只负责选择和填参，最终校验始终由服务端执行。
# 项目内直接依赖：voice_agent, voice_agent.capabilities, voice_agent.dashscope_client, .models
# 主要公开符号：SemanticPlanner
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import json
import logging
import re
import time
import threading
import urllib.error
from typing import Any, Callable

from jsonschema import Draft202012Validator

from voice_agent import config
from voice_agent.capabilities import CapabilityRegistry
from voice_agent.dashscope_client import http_post_json
from voice_agent.logging_utils import log_event
from voice_agent.radio_mode import normalize_radio_mode
from voice_agent.mcp_http_server.wire_compat import canonical_wire_invocation
from voice_agent.prompts import PLANNER_SYSTEM_PROMPT, build_planner_prompt
from .canonicalizer import PlanCanonicalizer
from .models import CapabilityPlanProposal
from .shortcut import match_shortcut_plan

ModelCallable = Callable[[dict[str, Any]], dict[str, Any]]
_REF_PREFIX = "${"
_BARE_REF_RE = re.compile(r"^\$([A-Za-z0-9_.-]+)\.([A-Za-z0-9_.-]+)$")

logger = logging.getLogger(__name__)
_PLANNER_TIMING = threading.local()


class SemanticPlanner:
    """把自然语言目标转换为受能力注册表约束的执行计划。

    规划模型不会接收任意执行器名称或网络端点，只能选择服务端注册表
    提供的能力 ID，并按对应输入结构生成参数。
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        model: ModelCallable | None = None,
        max_steps: int = 8,
        shortcut_enabled: bool | None = None,
    ) -> None:
        self.registry = registry
        self.model = model or self._call_qwen
        self.max_steps = max(1, int(max_steps))
        self.canonicalizer = PlanCanonicalizer()
        self.last_timing_ms: dict[str, Any] = {}
        # 能力目录静态不变（YAML 加载），构建一次后复用同一对象，
        # 避免每次 build_prompt 重复构建 63 条契约（也是 Context Cache
        # 前缀稳定的前提）；PLANNER_PROMPT_MODE 变化时重建。
        self._prompt_capabilities: list[dict[str, Any]] | None = None
        self._prompt_capabilities_mode: str | None = None
        # 高频指令快捷路径开关：None 跟随全局 config；测试可用 False 锁定 LLM 路径。
        self.shortcut_enabled = (
            config.PLANNER_SHORTCUT_ENABLE if shortcut_enabled is None else bool(shortcut_enabled)
        )

    def build_prompt(self, text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        mode = str(config.PLANNER_PROMPT_MODE)
        if self._prompt_capabilities is None or self._prompt_capabilities_mode != mode:
            self._prompt_capabilities = (
                self.registry.planner_prompt_contracts()
                if mode == "compact"
                else self.registry.planner_contracts()
            )
            self._prompt_capabilities_mode = mode
        return build_planner_prompt(
            request=str(text or "").strip(),
            context=context or {},
            capabilities=self._prompt_capabilities,
            max_steps=self.max_steps,
        )

    def plan(self, text: str, *, context: dict[str, Any] | None = None) -> CapabilityPlanProposal:
        plan_started = time.perf_counter()
        # ------------------------------------------------------------
        # 高频指令快捷路径：确定性规则直接构造计划，跳过 LLM 调用。
        # 构造结果仍走下方同一套 canonicalizer + Schema 校验；
        # 校验异常时回退 LLM 路径，语义不受影响。
        # ------------------------------------------------------------
        if self.shortcut_enabled:
            shortcut = match_shortcut_plan(str(text or ""))
            if shortcut is not None:
                rule_name, shortcut_payload = shortcut
                try:
                    proposal = CapabilityPlanProposal.model_validate(shortcut_payload)
                    if len(proposal.steps) > self.max_steps:
                        raise ValueError("shortcut exceeded max_steps")
                    if proposal.decision == "EXECUTE":
                        self.canonicalizer.canonicalize(proposal)
                        self._validate_steps(proposal)
                    self.last_timing_ms = {
                        "shortcut": True,
                        "shortcut_rule": rule_name,
                        "planner_total_ms": round(
                            (time.perf_counter() - plan_started) * 1000.0, 3
                        ),
                    }
                    log_event(
                        logger,
                        "Planner 快捷路径命中",
                        rule=rule_name,
                        request=str(text or "")[:64],
                        steps=[step.capability for step in proposal.steps],
                        planner_total_ms=self.last_timing_ms["planner_total_ms"],
                    )
                    return proposal
                except Exception as exc:
                    log_event(
                        logger,
                        "Planner 快捷路径校验失败回退LLM",
                        rule=rule_name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
        prompt = self.build_prompt(text, context)
        raw = self.model(prompt)
        self.last_timing_ms = dict(getattr(_PLANNER_TIMING, "value", {}) or {})
        self.last_timing_ms["planner_total_ms"] = round((time.perf_counter() - plan_started) * 1000.0, 3)
        proposal = CapabilityPlanProposal.model_validate(raw)
        if len(proposal.steps) > self.max_steps:
            raise ValueError("planner exceeded max_steps")
        if proposal.decision == "EXECUTE":
            # JSON mode guarantees parseable JSON, not business-level argument typing.
            # Normalize only narrowly-recognized malformed cross-step reference shapes
            # before canonicalization/Schema validation; all references are still checked
            # against depends_on and the upstream capability output contract afterwards.
            self._normalize_reference_shapes(proposal)
            # LLM 负责语义；实体类型和标准跨步骤绑定属于服务端权威契约。
            # 在 Schema 校验前统一编译，确保 OperationPlan 本身就是稳定计划。
            self.canonicalizer.canonicalize(proposal)
            self._validate_steps(proposal)
        return proposal


    def _normalize_reference_shapes(self, proposal: CapabilityPlanProposal) -> None:
        """Repair a tiny set of model-only reference shape deviations.

        The canonical plan reference is always the scalar string ``${step.field}``.
        Qwen occasionally mirrors ``output_fields`` metadata and emits a one-entry
        object such as ``{"$step_1.channel_id": "integer"}`` instead.  Treat that as
        a formatting deviation only when the destination field expects a scalar and
        the marker value is clearly schema metadata.  Unknown objects remain untouched
        and will fail the normal JSON Schema validation.
        """

        for step in proposal.steps:
            try:
                schema = self.registry.get(step.capability).planner_contract()["input_schema"]
            except Exception:
                # Keep unknown capability handling in the existing validation path.
                continue
            step.arguments = self._normalize_reference_value(step.arguments, schema)

    @classmethod
    def _normalize_reference_value(cls, value: Any, schema: dict[str, Any]) -> Any:
        if isinstance(value, list):
            item_schema = schema.get("items") if isinstance(schema, dict) else {}
            return [cls._normalize_reference_value(item, item_schema or {}) for item in value]

        if isinstance(value, dict):
            expected = schema.get("type") if isinstance(schema, dict) else None
            if isinstance(expected, list):
                expected = next((item for item in expected if item != "null"), None)

            # Only scalar destination fields are eligible for the one-entry repair.
            if expected in {"string", "integer", "number", "boolean"} and len(value) == 1:
                raw_key, marker = next(iter(value.items()))
                match = _BARE_REF_RE.fullmatch(str(raw_key or ""))
                if match:
                    field = match.group(2)
                    allowed_markers = {
                        "ref",
                        "reference",
                        "string",
                        "integer",
                        "number",
                        "boolean",
                        field,
                    }
                    marker_text = str(marker or "").strip().lower()
                    if marker is None or marker_text in allowed_markers:
                        return f"${{{match.group(1)}.{field}}}"

            properties = schema.get("properties") if isinstance(schema, dict) else {}
            properties = properties if isinstance(properties, dict) else {}
            return {
                key: cls._normalize_reference_value(child, properties.get(key, {}))
                for key, child in value.items()
            }

        return value

    @classmethod
    def _canonicalize_radio_directory_handoffs(
        cls, proposal: CapabilityPlanProposal
    ) -> None:
        """兼容旧内部入口；实际逻辑已迁移到通用 PlanCanonicalizer。"""

        PlanCanonicalizer.canonicalize_radio_directory_handoffs(proposal)

    def _validate_steps(self, proposal: CapabilityPlanProposal) -> None:
        seen: set[str] = set()
        seen_capabilities: dict[str, str] = {}
        for step in proposal.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id}")
            spec = self.registry.get(step.capability)
            if not spec.available:
                raise ValueError(f"capability unavailable: {step.capability}: {spec.unavailable_reason}")
            for dependency in step.depends_on:
                if dependency not in seen:
                    raise ValueError(f"dependency must reference an earlier step: {dependency}")
            schema = spec.planner_contract()["input_schema"]

            # ------------------------------------------------------------
            # v0.2 兼容：旧计划/模型输出可能仍携带 call_type/callee_id 等
            # 旧字段名；在 schema 校验前统一归一为附件契约字段名，
            # 保证旧调用链在新目录下继续可用。
            # ------------------------------------------------------------
            _, canonical_arguments = canonical_wire_invocation(step.capability, step.arguments)
            if canonical_arguments != step.arguments:
                step.arguments = canonical_arguments

            # ------------------------------------------------------------
            # Radio mode 确定性补全
            #
            # mode 对最终 Capability / Tool 是必传字段，
            # 但不能要求用户必须在自然语言中明确说出来。
            #
            # 用户没有说明：
            #     -> conventional
            #
            # 用户明确：
            #     “集群模式”
            #     -> trunking
            #
            # 如果 mode 是前一步结果引用：
            #     ${step_1.mode}
            # 则不能在这里改写。
            # ------------------------------------------------------------
            if step.capability in {
                "radio_resolve_zone_channel",
                "zone_channel_active_set",
            }:
                mode_value = step.arguments.get("mode")

                if not (
                    isinstance(mode_value, str)
                    and mode_value.startswith(_REF_PREFIX)
                ):
                    step.arguments["mode"] = normalize_radio_mode(
                        mode_value
                    )


            arguments_for_schema = self._references_as_schema_placeholders(
                step.arguments,
                schema,
            )

            errors = list(
                Draft202012Validator(schema).iter_errors(
                    arguments_for_schema
                )
            )

            if errors:
                raise ValueError(
                    f"invalid arguments for {step.capability}: "
                    f"{errors[0].message}"
                )
            self._validate_references(step.arguments, seen, set(step.depends_on))
            self._validate_reference_outputs(
                step.arguments,
                seen_capabilities,
                set(step.depends_on),
            )
            seen.add(step.id)
            seen_capabilities[step.id] = step.capability

    @classmethod
    def _validate_references(cls, value: Any, seen: set[str], declared: set[str]) -> None:
        if isinstance(value, dict):
            for child in value.values():
                cls._validate_references(child, seen, declared)
            return
        if isinstance(value, list):
            for child in value:
                cls._validate_references(child, seen, declared)
            return
        if not isinstance(value, str) or not value.startswith(_REF_PREFIX):
            return
        if not (value.endswith("}") and "." in value[2:-1]):
            raise ValueError(f"invalid step reference: {value}")
        step_id = value[2:-1].split(".", 1)[0]
        if step_id not in seen:
            raise ValueError(f"reference must point to an earlier step: {value}")
        if step_id not in declared:
            raise ValueError(f"reference step must be declared in depends_on: {value}")

    def _validate_reference_outputs(
        self,
        value: Any,
        seen_capabilities: dict[str, str],
        declared: set[str],
    ) -> None:
        if isinstance(value, dict):
            for child in value.values():
                self._validate_reference_outputs(child, seen_capabilities, declared)
            return
        if isinstance(value, list):
            for child in value:
                self._validate_reference_outputs(child, seen_capabilities, declared)
            return
        if not isinstance(value, str) or not value.startswith(_REF_PREFIX):
            return
        if not (value.endswith("}") and "." in value[2:-1]):
            return
        step_id, output_path = value[2:-1].split(".", 1)
        if step_id not in declared or step_id not in seen_capabilities:
            return
        spec = self.registry.get(seen_capabilities[step_id])
        if spec.output_path_declared(output_path) is False:
            raise ValueError(
                f"reference output is not declared by {spec.id}: {value}"
            )

    @classmethod
    def _references_as_schema_placeholders(cls, value: Any, schema: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            properties = schema.get("properties") if isinstance(schema, dict) else {}
            return {
                key: cls._references_as_schema_placeholders(child, (properties or {}).get(key, {}))
                for key, child in value.items()
            }
        if isinstance(value, list):
            item_schema = schema.get("items") if isinstance(schema, dict) else {}
            return [cls._references_as_schema_placeholders(child, item_schema or {}) for child in value]
        if isinstance(value, str) and value.startswith(_REF_PREFIX):
            enum_values = schema.get("enum") if isinstance(schema, dict) else None
            if isinstance(enum_values, list) and enum_values:
                return enum_values[0]
            expected = schema.get("type") if isinstance(schema, dict) else None
            if isinstance(expected, list):
                expected = next((item for item in expected if item != "null"), None)
            return {"string": "ref", "integer": 1, "number": 1.0, "boolean": True, "array": [], "object": {}}.get(expected, "ref")
        return value

    # 本地 Qwen3 系列模型可能把思考段（形如 <think>...</think>）混入
    # content，解析 JSON 前防御性剔除；未闭合的思考段则剔到结尾。
    _THINK_BLOCK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)

    @staticmethod
    def _strip_think_blocks(content: str) -> str:
        """Drop residual thinking blocks before JSON parsing (defensive)."""

        return SemanticPlanner._THINK_BLOCK_RE.sub("", content).strip()

    @staticmethod
    def _extract_json(response: dict[str, Any]) -> dict[str, Any]:
        content = (((response.get("choices") or [{}])[0].get("message") or {}).get("content"))
        if not isinstance(content, str) or not content.strip():
            raise ValueError("planner model returned empty content")
        content = SemanticPlanner._strip_think_blocks(content)
        if not content:
            raise ValueError("planner model returned empty content after stripping think blocks")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("planner model output must be a JSON object")
        return parsed

    @staticmethod
    def _usage_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _planner_usage_metrics(cls, response: dict[str, Any]) -> dict[str, Any]:
        """Normalize Qwen/OpenAI-compatible cache usage fields for logging only."""

        usage = response.get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        prompt_details = usage.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        cache_creation = prompt_details.get("cache_creation")
        cache_creation = cache_creation if isinstance(cache_creation, dict) else {}

        prompt_tokens = cls._usage_int(
            usage.get("prompt_tokens", usage.get("input_tokens"))
        )
        completion_tokens = cls._usage_int(
            usage.get("completion_tokens", usage.get("output_tokens"))
        )
        cached_tokens = cls._usage_int(
            prompt_details.get(
                "cached_tokens",
                usage.get("cached_tokens", usage.get("cache_read_input_tokens")),
            )
        )
        cache_creation_input_tokens = cls._usage_int(
            prompt_details.get(
                "cache_creation_input_tokens",
                cache_creation.get(
                    "ephemeral_5m_input_tokens",
                    usage.get("cache_creation_input_tokens"),
                ),
            )
        )
        total_tokens = cls._usage_int(usage.get("total_tokens"))
        if total_tokens <= 0 and (prompt_tokens or completion_tokens):
            total_tokens = prompt_tokens + completion_tokens

        uncached_prompt_tokens = max(
            0,
            prompt_tokens - cached_tokens - cache_creation_input_tokens,
        )
        cache_hit_ratio = (
            round(cached_tokens / prompt_tokens, 4)
            if prompt_tokens > 0
            else 0.0
        )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "uncached_prompt_tokens": uncached_prompt_tokens,
            "cache_hit": cached_tokens > 0,
            "cache_hit_ratio": cache_hit_ratio,
        }

    @staticmethod
    def _qwen_messages(prompt: dict[str, Any]) -> list[dict[str, Any]]:
        """Build Planner messages with an optional stable Qwen cache prefix.

        Context Cache is deliberately applied only at the transport prompt boundary:
        capability definitions, output contract and rules form the stable prefix, while
        the current request and context remain uncached dynamic input. The semantic
        planner contract and all server-side validation therefore remain unchanged.
        """

        # Context Cache（cache_control 前缀标记）是 DashScope 专属特性；
        # local provider（自部署 OpenAI 兼容服务）不支持，一律用普通消息。
        if config.PLANNER_PROVIDER == "local" or not config.PLANNER_CONTEXT_CACHE_ENABLE:
            return [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        prompt,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]

        dynamic_context = dict(prompt.get("context") or {})

        # ``available_capabilities`` and ``current_request`` are authoritative
        # duplicates of data already present in the static capability catalog and
        # top-level request.  Pop them defensively as well as omitting them from
        # DecisionContext.planner_payload(), so older/custom callers cannot inflate
        # the uncached suffix.
        dynamic_context.pop("available_capabilities", None)
        dynamic_context.pop("current_request", None)

        # Policy constraints are stable service-side rules.  ContextItem wrappers
        # contain volatile metadata (for example observed_at), which would invalidate
        # prefix caching.  Only their semantic payload belongs in the static prefix.
        static_policy_constraints: list[dict[str, Any]] = []
        for item in dynamic_context.pop("policy_constraints", []) or []:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload")
            if isinstance(payload, dict) and payload:
                static_policy_constraints.append(payload)

        # Context build/budget metadata is retained in snapshots and logs, but does
        # not help the model choose or parameterize capabilities.
        for key in (
            "schema_version",
            "budget",
            "used_items",
            "used_tokens",
            "truncated",
            "built_at",
        ):
            dynamic_context.pop(key, None)

        static_contract = {
            "capabilities": prompt.get("capabilities") or [],
            "output_contract": prompt.get("output_contract") or {},
            "rules": prompt.get("rules") or [],
            "policy_constraints": static_policy_constraints,
        }
        dynamic_input = {
            "request": str(prompt.get("request") or ""),
            "context": dynamic_context,
        }
        static_text = (
            PLANNER_SYSTEM_PROMPT
            + "\n【服务端固定规划契约】\n"
            + json.dumps(
                static_contract,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        dynamic_text = json.dumps(
            dynamic_input,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": static_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": dynamic_text},
        ]

    @classmethod
    def _planner_endpoint(cls) -> tuple[str, str, str]:
        """Return (url, model, api_key) for the configured planner provider.

        api_key 为空字符串时 http_post_json 不发送 Authorization 头，适配
        无鉴权的自部署 OpenAI 兼容服务（如 vLLM qwen3.5-4b）。
        """

        if config.PLANNER_PROVIDER == "local":
            return (
                config.LOCAL_PLANNER_CHAT_COMPLETIONS_URL,
                config.LOCAL_PLANNER_MODEL,
                config.LOCAL_PLANNER_API_KEY,
            )
        return (
            config.DASHSCOPE_CHAT_COMPLETIONS_URL,
            config.PLANNER_MODEL,
            config.DASHSCOPE_API_KEY,
        )

    @classmethod
    def _call_qwen(cls, prompt: dict[str, Any]) -> dict[str, Any]:
        endpoint_url, model_name, api_key = cls._planner_endpoint()
        if config.PLANNER_PROVIDER != "local" and not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for semantic planning")
        payload = {
            "model": model_name,
            "messages": cls._qwen_messages(prompt),
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if config.PLANNER_PROVIDER == "local":
            # vLLM 的 OpenAI 兼容层通过 chat_template_kwargs 控制模板变量；
            # Qwen3 系列 thinking 默认关闭（QWEN_LOCAL_PLANNER_ENABLE_THINKING）。
            payload["chat_template_kwargs"] = {
                "enable_thinking": bool(config.LOCAL_PLANNER_ENABLE_THINKING)
            }
        attempt = 0
        last_error: Exception | None = None
        aggregate: dict[str, Any] = {
            "rate_limit_wait_ms": 0.0, "request_serialize_ms": 0.0, "http_ms": 0.0,
            "response_json_ms": 0.0, "retry_wait_ms": 0.0, "attempt_ms": [], "attempts": 0,
        }
        model_started = time.perf_counter()
        while attempt <= max(0, int(config.PLANNER_MAX_RETRIES)):
            attempt += 1
            request_started = time.perf_counter()
            try:
                request_timing: dict[str, float] = {}
                status, response, request_bytes, response_bytes = http_post_json(
                    endpoint_url, payload, config.PLANNER_TIMEOUT_SECONDS,
                    request_kind="planner", wait_timeout_seconds=config.DASHSCOPE_WAIT_TIMEOUT_SECONDS,
                    timing=request_timing, api_key=api_key,
                )
                planner_call_ms = round((time.perf_counter() - request_started) * 1000.0, 3)
                aggregate["attempt_ms"].append(planner_call_ms)
                aggregate["attempts"] = attempt
                for source, target in (("rate_limit_wait_ms", "rate_limit_wait_ms"),
                                       ("request_serialize_ms", "request_serialize_ms"),
                                       ("http_roundtrip_ms", "http_ms"),
                                       ("response_json_ms", "response_json_ms")):
                    aggregate[target] = round(float(aggregate[target]) + float(request_timing.get(source) or 0.0), 3)
                usage_metrics = cls._planner_usage_metrics(response)
                log_event(
                    logger, "Planner Context Cache 指标",
                    explicit_cache_enabled=bool(config.PLANNER_CONTEXT_CACHE_ENABLE),
                    cache_hit=usage_metrics["cache_hit"], cache_hit_ratio=usage_metrics["cache_hit_ratio"],
                    prompt_tokens=usage_metrics["prompt_tokens"], cached_tokens=usage_metrics["cached_tokens"],
                    cache_creation_input_tokens=usage_metrics["cache_creation_input_tokens"],
                    uncached_prompt_tokens=usage_metrics["uncached_prompt_tokens"], completion_tokens=usage_metrics["completion_tokens"],
                    total_tokens=usage_metrics["total_tokens"], planner_call_ms=planner_call_ms,
                    planner_rate_limit_wait_ms=request_timing.get("rate_limit_wait_ms", 0.0),
                    planner_request_serialize_ms=request_timing.get("request_serialize_ms", 0.0),
                    planner_http_ms=request_timing.get("http_roundtrip_ms", planner_call_ms),
                    planner_response_json_ms=request_timing.get("response_json_ms", 0.0),
                    planner_retry_wait_ms=aggregate["retry_wait_ms"], request_bytes=request_bytes,
                    response_bytes=response_bytes, http_status=status, attempt=attempt,
                )
                if not 200 <= status < 300:
                    raise RuntimeError(f"planner http status {status}")
                aggregate["model_call_total_ms"] = round((time.perf_counter() - model_started) * 1000.0, 3)
                _PLANNER_TIMING.value = aggregate
                return cls._extract_json(response)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, ValueError) as exc:
                last_error = exc
                aggregate["attempts"] = attempt
                if not aggregate["attempt_ms"] or len(aggregate["attempt_ms"]) < attempt:
                    aggregate["attempt_ms"].append(round((time.perf_counter() - request_started) * 1000.0, 3))
                if attempt > max(0, int(config.PLANNER_MAX_RETRIES)):
                    break
                delay = min(8.0, 2.0 ** attempt)
                aggregate["retry_wait_ms"] = round(float(aggregate["retry_wait_ms"]) + delay * 1000.0, 3)
                time.sleep(delay)
        aggregate["model_call_total_ms"] = round((time.perf_counter() - model_started) * 1000.0, 3)
        _PLANNER_TIMING.value = aggregate
        raise RuntimeError(f"semantic planner failed: {last_error}")
