# =============================================================================
# 【维护注释】文件：voice_agent/reasoning/canonicalizer.py
# 所属层次：语义规划层 / 确定性计划编译
# 主要职责：在 LLM 生成结构化计划后、Schema 校验前，把“服务端权威事实”的跨步骤参数绑定规范化。
# 设计原则：只修正能够由既有 resolver/query 输出确定的连接关系，不猜测用户语义，不改写用户业务 payload。
# =============================================================================
from __future__ import annotations

from typing import Any

from voice_agent.zone_channel_directory import resolve_zone_channel_matches

from .models import CapabilityPlanProposal


class PlanCanonicalizer:
    """把模型计划编译成稳定、可验证的原子能力连接。

    LLM 负责理解“要做什么”；目录实体类型、上游输出字段以及能力之间的标准投影
    属于服务端契约，不应该继续由模型猜测。这里仅执行可证明正确的确定性改写。
    """

    CONTACT_RESOLVERS = frozenset({"contact_resolve"})
    RECENT_CALL_RESOLVERS = frozenset({"history_recent_calls", "call_query", "history_call_by_position"})

    def canonicalize(self, proposal: CapabilityPlanProposal) -> CapabilityPlanProposal:
        if proposal.decision != "EXECUTE":
            return proposal

        self._canonicalize_reference_dependencies(proposal)
        self._canonicalize_channel_named_group_switch(proposal)
        self.canonicalize_radio_directory_handoffs(proposal)
        self._canonicalize_contact_handoffs(proposal)
        self._canonicalize_call_summary_handoffs(proposal)
        return proposal

    @staticmethod
    def _reference(step_id: str, field: str) -> str:
        return f"${{{step_id}.{field}}}"

    @staticmethod
    def _step_capabilities(proposal: CapabilityPlanProposal) -> dict[str, str]:
        return {step.id: step.capability for step in proposal.steps}

    def _canonicalize_reference_dependencies(self, proposal: CapabilityPlanProposal) -> None:
        """引用即数据依赖：补全模型遗漏的 depends_on 声明。

        模型偶发在 arguments 中输出 ``${step_1.zone_id}`` 引用却忘记把
        step_1 写入 depends_on（2026-10-08 真机复现：“切换到交通组”
        PLAN_FAILED: reference step must be declared in depends_on，
        整次 LLM 往返被浪费）。引用本身就证明了数据依赖，服务端补全
        只影响执行顺序（引用的上游本就必须先执行），不改变语义；指向
        不存在或更晚步骤的引用不在此修复，仍交给校验层报错澄清。
        """

        earlier_ids: set[str] = set()
        for step in proposal.steps:
            referenced: set[str] = set()
            self._collect_argument_references(step.arguments, referenced)
            missing = sorted(
                step_id
                for step_id in referenced
                if step_id in earlier_ids and step_id not in step.depends_on
            )
            if missing:
                step.depends_on = [*step.depends_on, *missing]
            earlier_ids.add(step.id)

    @classmethod
    def _collect_argument_references(cls, value: Any, referenced: set[str]) -> None:
        if isinstance(value, dict):
            for child in value.values():
                cls._collect_argument_references(child, referenced)
            return
        if isinstance(value, list):
            for child in value:
                cls._collect_argument_references(child, referenced)
            return
        if isinstance(value, str) and value.startswith("${"):
            if value.endswith("}") and "." in value[2:-1]:
                referenced.add(value[2:-1].split(".", 1)[0])

    def _single_dependency_with_capability(
        self,
        step: Any,
        capabilities_by_step: dict[str, str],
        allowed: frozenset[str],
    ) -> str | None:
        matches = [
            dependency
            for dependency in step.depends_on
            if capabilities_by_step.get(dependency) in allowed
        ]
        # 多个同类 resolver 时无法证明哪个实体对应当前动作，保留模型原值交给后续校验/澄清。
        return matches[0] if len(matches) == 1 else None

    def _canonicalize_channel_named_group_switch(self, proposal: CapabilityPlanProposal) -> None:
        """权威目录证伪“联系人组切换”时，把 folder 链确定性纠偏为 radio 链。

        模型偶尔把以信道名命名的组（如“公安组”）误规划为
        contact_resolve → folder_group_active_set（2026-08-31 真机复现：
        contact_not_resolved，folder 接口永不可能成功）。名称在区域/信道
        目录可解析、且联系人目录无同名实体时，服务端可证明该名称是
        信道而非联系人：保留步骤 ID/依赖，改写为
        radio_resolve_zone_channel → zone_channel_active_set 标准链，
        实体参数交由 radio 目录投影接管。名称命中联系人或两目录均未
        命中时无法证伪模型语义，保持原计划交给解析失败澄清。
        """

        from voice_agent.contact_directory import get_contact_map

        capabilities_by_step = self._step_capabilities(proposal)
        for step in proposal.steps:
            if step.capability != "folder_group_active_set":
                continue
            resolver_id = self._single_dependency_with_capability(
                step, capabilities_by_step, self.CONTACT_RESOLVERS
            )
            if resolver_id is None:
                continue
            resolver_step = next(
                (item for item in proposal.steps if item.id == resolver_id), None
            )
            name = str(
                (resolver_step.arguments if resolver_step is not None else {}).get("name") or ""
            ).strip()
            if not name:
                continue
            matches = resolve_zone_channel_matches(name)
            if not matches:
                continue
            try:
                contact_map = get_contact_map() or {}
            except Exception:
                contact_map = {}
            if any(
                name in (mapping or {})
                for mapping in (
                    contact_map.get("group"),
                    contact_map.get("private"),
                    contact_map.get("standby"),
                )
            ):
                continue
            if resolver_step is not None:
                resolver_step.capability = "radio_resolve_zone_channel"
                resolver_step.arguments = {kind: name for kind in matches}
            # folder 的 mode（tmo/dmo）与 radio 的 mode（active/conventional/
            # trunking）语义域不同，跨接口保留必然校验失败；丢弃后由
            # _validate_steps 按用户语义确定性补全（未提“集群”即 conventional）。
            step.capability = "zone_channel_active_set"
            step.arguments = {}

    def _canonicalize_contact_handoffs(self, proposal: CapabilityPlanProposal) -> None:
        """联系人 ID / 类型永远从 contact_resolve 输出投影，不让 LLM 猜。"""

        capabilities_by_step = self._step_capabilities(proposal)
        for step in proposal.steps:
            if step.capability not in {"sms_send", "call_start"}:
                continue
            resolver_id = self._single_dependency_with_capability(
                step,
                capabilities_by_step,
                self.CONTACT_RESOLVERS,
            )
            if resolver_id is None:
                continue

            args = dict(step.arguments)
            if step.capability == "sms_send":
                args["target_id"] = self._reference(resolver_id, "target_id")
                args["target_type"] = self._reference(resolver_id, "target_type")
            else:
                # call_start 的 planner 契约是旧名 call_type/callee_id，校验前由
                # wire_compat 归一为 v0.2 新名（target_type/target_id）。模型可能
                # 直接按能力 schema 输出新名引用：此处统一编译为旧名投影，并
                # 删除模型给的同义新名字段——否则 wire_compat 检测到新名已存在
                # 会跳过重命名，旧名残留将触发 additionalProperties 校验失败。
                args.pop("target_type", None)
                args.pop("target_id", None)
                args["callee_id"] = self._reference(resolver_id, "target_id")
                args["call_type"] = self._reference(resolver_id, "contact_type")
            step.arguments = args

    def _canonicalize_call_summary_handoffs(self, proposal: CapabilityPlanProposal) -> None:
        """通话摘要使用查询/历史能力返回的真实 call_id，不接受模型凭空构造。"""

        capabilities_by_step = self._step_capabilities(proposal)
        for step in proposal.steps:
            if step.capability != "call_summarize":
                continue
            resolver_id = self._single_dependency_with_capability(
                step,
                capabilities_by_step,
                self.RECENT_CALL_RESOLVERS,
            )
            if resolver_id is None:
                continue
            args = dict(step.arguments)
            args["call_id"] = self._reference(resolver_id, "call_id")
            step.arguments = args

    @classmethod
    def canonicalize_radio_directory_handoffs(
        cls,
        proposal: CapabilityPlanProposal,
    ) -> None:
        """让权威区域/信道目录决定实体类型及下游 ``*_id`` 投影。"""

        resolved_kinds_by_step: dict[str, set[str]] = {}

        for step in proposal.steps:
            if step.capability != "radio_resolve_zone_channel":
                continue

            preserved = {
                key: value
                for key, value in step.arguments.items()
                if key not in {"zone", "channel"}
            }
            unresolved: dict[str, Any] = {}
            canonical_names: dict[str, str] = {}
            canonical_ids: dict[str, str] = {}
            conflict = False

            for hinted_kind in ("zone", "channel"):
                raw_value = step.arguments.get(hinted_kind)
                if raw_value is None or not str(raw_value).strip():
                    continue
                matches = resolve_zone_channel_matches(
                    raw_value,
                    hinted_kind=hinted_kind,
                )
                if not matches:
                    # 无法唯一解析时保留原输入，让确定性 resolver 返回澄清，而不是静默猜测。
                    unresolved[hinted_kind] = raw_value
                    continue

                for actual_kind, match in matches.items():
                    entity_id = str(match.get("id") or "")
                    previous_id = canonical_ids.get(actual_kind)
                    if previous_id is not None and previous_id != entity_id:
                        conflict = True
                        break
                    canonical_ids[actual_kind] = entity_id
                    canonical_names[actual_kind] = str(match.get("name") or raw_value)
                if conflict:
                    break

            if conflict:
                continue

            step.arguments = {**preserved, **unresolved, **canonical_names}
            if canonical_names:
                resolved_kinds_by_step[step.id] = set(canonical_names)

        if not resolved_kinds_by_step:
            return

        for step in proposal.steps:
            if step.capability != "zone_channel_active_set":
                continue

            args = dict(step.arguments)
            for resolver_id in step.depends_on:
                kinds = resolved_kinds_by_step.get(resolver_id)
                if not kinds:
                    continue

                projected_keys: list[str] = []
                for key in ("zone_id", "channel_id"):
                    value = args.get(key)
                    if (
                        isinstance(value, str)
                        and value.startswith(f"${{{resolver_id}.")
                        and value.endswith("}")
                    ):
                        projected_keys.append(key)
                for key in projected_keys:
                    args.pop(key, None)

                # 模型偶发把条件必选 ID 输出为 null（simulation 真实模型
                # 复现）：null 既非合法 id 也非引用，目录已证明实体类型时
                # 直接丢弃，交由下方引用投影接管，避免 ``None is not of
                # type 'integer'`` 校验失败导致整计划 PLAN_FAILED。
                for id_key in ("zone_id", "channel_id"):
                    if id_key in args and args.get(id_key) is None:
                        args.pop(id_key, None)

                for kind in ("zone", "channel"):
                    if kind not in kinds:
                        continue
                    key = f"{kind}_id"
                    args.setdefault(key, cls._reference(resolver_id, key))

            step.arguments = args
