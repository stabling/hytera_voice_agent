# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/reasoning/shortcut.py
# 所属层次：语义规划层 / 高频指令快捷路径
# 主要职责：对高频、无歧义的指令（切换区域/信道、呼叫联系人）用确定性规则
#           直接构造能力计划，跳过 LLM 语义规划调用，把 planner 延迟从秒级降到亚毫秒级。
# 项目内直接依赖：命名组切换规则在命中时惰性查询权威目录
#           （zone_channel_directory / contact_directory，与 canonicalizer 的
#           folder 纠偏同判据），其他规则保持纯函数。
# 主要公开符号：match_shortcut_plan
# 兼容性：规则未命中或命中后校验失败时返回 None，调用方回退原 LLM 规划路径；
#         快捷计划仍完整经过 canonicalizer + schema 校验 + policy，不绕过任何治理。
# 修改原则：只收录 fullmatch 的高频模式；名称解析交给权威目录 resolver；
#           不在规则里猜测实体 ID/类型。
# 排障顺序：先看 shortcut 是否命中（日志 rule 字段），再看 canonicalizer/validator
#           是否接受构造的计划，最后确认回退 LLM 路径正常。
# =============================================================================
"""Deterministic shortcut rules for high-frequency voice commands.

The semantic planner spends several seconds on an LLM round trip even for the
most common commands (switch zone/channel, call a contact).  These rules
pattern-match a small closed set of phrasings and build the exact same
two-step capability plan the LLM would emit.  Everything downstream
(canonicalization, JSON-Schema validation, policy, approvals) is unchanged.

Any input that does not fully match one of the patterns returns ``None`` and
the caller falls back to the LLM planner, so ambiguous commands keep their
original semantics.
"""
from __future__ import annotations

import re
from typing import Any

# 口语修饰前缀（可叠加出现一次），如“请帮我切换到…”“麻烦呼叫一下…”
_PREFIX = r"(?:请|麻烦|帮我|帮忙|给我)?(?:帮我)?"
# 语气词结尾
_TAIL = r"[吧啊呗呀]?"

_ZONE_CHANNEL_RE = re.compile(
    _PREFIX
    + r"(?:切换|切|换)(?:到|至)?"
    + r"(?P<zone>[^，。,！？!?;；区]{1,16}?)区"
    + r"(?P<channel>[^，。,！？!?;；]{1,16}?)信道"
    + _TAIL
)
_CHANNEL_ONLY_RE = re.compile(
    _PREFIX
    + r"(?:切换|切|换)(?:到|至)?"
    + r"(?P<channel>[^，。,！？!?;；区]{1,16}?)信道"
    + _TAIL
)
_ZONE_ONLY_RE = re.compile(
    _PREFIX
    + r"(?:切换|切|换)(?:到|至)?"
    + r"(?P<zone>[^，。,！？!?;；]{1,16}?)区"
    + _TAIL
)
# “切换到X组”：X 不含“区”（含区复合指令交给 LLM 理解）；是否为信道
# 由权威目录证明，目录无法证明时回退 LLM。
_GROUP_SWITCH_RE = re.compile(
    _PREFIX
    + r"(?:切换|切|换)(?:到|至)?"
    + r"(?P<name>[^，。,！？!?;；区]{1,24}?)组"
    + _TAIL
)
_CALL_RE = re.compile(
    _PREFIX
    + r"(?:呼叫|拨打|打给)(?:一下|下)?"
    + r"(?P<name>[^，。,！？!?;；]{1,24})"
    + _TAIL
)
_GROUP_CALL_RE = re.compile(
    _PREFIX + r"组呼" + r"(?P<name>[^，。,！？!?;；]{1,24})" + _TAIL
)
_CALL_PHONE_RE = re.compile(
    _PREFIX + r"给" + r"(?P<name>[^，。,！？!?;；]{1,24}?)" + r"(?:打|拨打)(?:个)?电话" + _TAIL
)

_TRAILING_PUNCT_RE = re.compile(r"[，。,！？!?;；、\s]+$")


def _clean_text(text: str) -> str:
    """Strip surrounding whitespace/punctuation and inner ASCII spaces."""
    value = str(text or "").strip()
    value = _TRAILING_PUNCT_RE.sub("", value).strip()
    return value.replace(" ", "")


def _radio_mode_and_cleaned(cleaned: str) -> tuple[str, str]:
    """Trunking hint detection.

    ``集群模式`` is the explicit mode wording and is stripped before name
    matching so ``切换到集群模式北信道`` yields mode=trunking/channel=北.
    A bare ``集群`` inside a name (e.g. the channel “集群北信道”) stays part of
    the name and keeps conventional mode — entity typing is the directory's
    job, not the shortcut's.
    """
    if "集群模式" in cleaned:
        return "trunking", cleaned.replace("集群模式", "")
    return "conventional", cleaned


def _zone_channel_plan(zone: str | None, channel: str | None, mode: str) -> dict[str, Any]:
    resolve_args: dict[str, Any] = {"mode": mode}
    set_args: dict[str, Any] = {"mode": mode}
    goal_parts: list[str] = []
    if zone is not None:
        resolve_args["zone"] = zone
        set_args["zone_id"] = "${step_1.zone_id}"
        goal_parts.append(f"区域{zone}")
    if channel is not None:
        resolve_args["channel"] = channel
        set_args["channel_id"] = "${step_1.channel_id}"
        goal_parts.append(f"信道{channel}")
    return {
        "decision": "EXECUTE",
        "goal": "切换" + "".join(goal_parts),
        "steps": [
            {
                "id": "step_1",
                "capability": "radio_resolve_zone_channel",
                "arguments": resolve_args,
                "depends_on": [],
            },
            {
                "id": "step_2",
                "capability": "zone_channel_active_set",
                "arguments": set_args,
                "depends_on": ["step_1"],
            },
        ],
    }


def _call_plan(name: str) -> dict[str, Any]:
    # 与 LLM 契约一致：call_start 用旧名 call_type/callee_id 引用 resolver
    # 输出，由 PlanCanonicalizer 保障引用、wire_compat 在校验前归一为
    # target_type/target_id。名称解析与类型由权威目录决定。
    return {
        "decision": "EXECUTE",
        "goal": f"呼叫{name}",
        "steps": [
            {
                "id": "step_1",
                "capability": "contact_resolve",
                "arguments": {"name": name},
                "depends_on": [],
            },
            {
                "id": "step_2",
                "capability": "call_start",
                "arguments": {
                    "callee_id": "${step_1.target_id}",
                    "call_type": "${step_1.contact_type}",
                },
                "depends_on": ["step_1"],
            },
        ],
    }


def _named_group_switch_plan(full_name: str, mode: str) -> dict[str, Any] | None:
    """“切换到X组”：权威目录能证明 X 是信道而非联系人时才构造计划。

    判据与 PlanCanonicalizer._canonicalize_channel_named_group_switch 的
    folder 纠偏一致：区域/信道目录命中 + 联系人目录无同名 → 信道切换；
    否则返回 None 回退 LLM，由模型按语义选择 folder 或 radio 链路。
    实体解析仍由 radio_resolve_zone_channel 运行时权威目录完成。
    """

    from voice_agent.contact_directory import get_contact_map
    from voice_agent.zone_channel_directory import resolve_zone_channel_matches

    matches = resolve_zone_channel_matches(full_name)
    if not matches:
        return None
    try:
        contact_map = get_contact_map() or {}
    except Exception:
        contact_map = {}
    if any(
        full_name in (mapping or {})
        for mapping in (
            contact_map.get("group"),
            contact_map.get("private"),
            contact_map.get("standby"),
        )
    ):
        return None
    return _zone_channel_plan(
        full_name if "zone" in matches else None,
        full_name if "channel" in matches else None,
        mode,
    )


def match_shortcut_plan(text: str) -> tuple[str, dict[str, Any]] | None:
    """Try the deterministic high-frequency shortcut rules.

    Returns ``(rule_name, proposal_payload)`` when one rule fully matches, or
    ``None`` to keep the LLM planner path.  Name resolution, entity typing and
    every validation/policy gate remain server-side and unchanged.
    """
    cleaned = _clean_text(text)
    if not cleaned:
        return None

    mode, cleaned = _radio_mode_and_cleaned(cleaned)
    match = _ZONE_CHANNEL_RE.fullmatch(cleaned)
    if match:
        return "zone_channel_switch", _zone_channel_plan(
            match.group("zone"), match.group("channel"), mode
        )
    match = _ZONE_ONLY_RE.fullmatch(cleaned)
    if match:
        return "zone_switch", _zone_channel_plan(match.group("zone"), None, mode)
    match = _CHANNEL_ONLY_RE.fullmatch(cleaned)
    if match:
        return "channel_switch", _zone_channel_plan(None, match.group("channel"), mode)

    match = _GROUP_SWITCH_RE.fullmatch(cleaned)
    if match:
        plan = _named_group_switch_plan(match.group("name") + "组", mode)
        if plan is not None:
            return "named_group_switch", plan
        # 目录无法证明“X组”是信道（未知名称或命中联系人）：回退 LLM。

    match = _GROUP_CALL_RE.fullmatch(cleaned)
    if match:
        return "group_call", _call_plan(match.group("name"))
    match = _CALL_PHONE_RE.fullmatch(cleaned)
    if match:
        return "call_contact", _call_plan(match.group("name"))
    match = _CALL_RE.fullmatch(cleaned)
    if match:
        return "call_contact", _call_plan(match.group("name"))
    return None
