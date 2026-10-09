# -*- coding: utf-8 -*-
"""语义规划器提示词。

定位规则：凡是决定“自然语言如何映射为 CapabilityPlanProposal”的 LLM 指令，
只允许放在本文件。执行校验、目录事实和风险策略仍由服务端代码负责。
"""
from __future__ import annotations

from typing import Any


PLANNER_SYSTEM_PROMPT = (
    "你是终端智能体的语义能力规划器。"
    "你的任务是理解完整用户目标，把语义等价、口语化、倒装、省略、礼貌表达或含有人称代词的请求，"
    "映射为服务端提供的原子能力计划。"
    "只返回一个 JSON 对象，不要输出解释、Markdown 或额外文本。"
    "不得臆造设备ID、联系人ID、通话ID、能力ID、工具、URL或其他服务端事实；"
    "存在解析能力时，实体身份和实体类型必须由解析能力产生。"
)


def _rules(max_steps: int) -> list[str]:
    return [
        "只输出符合 output_contract 的 JSON 对象。",
        "根据整句语义和上下文理解目标，不按字面关键词做硬匹配；语义等价表达必须尽量得到相同的能力序列。",
        "只能使用 capabilities 中由服务端提供的能力 ID，不得创造新能力。",
        "goal 只写精炼动作与必要业务内容，去掉面向助手的礼貌词、人称代词和无意义语言外壳；联系人姓名等执行实体放入 arguments，不写入 goal。",
        "不得删除或改写短信正文、待办内容、提醒事项等用户实际要保存或发送的 payload 中具有业务含义的人称、时间、地点或措辞。",
        "用户给出联系人称呼而不是稳定 ID 时，先使用 contact_resolve；下游不得猜测联系人类型。",
        "contact_resolve 后发送短信时，sms_send.target_id 必须引用其 target_id，sms_send.target_type 必须引用其 target_type。",
        "contact_resolve 后发起呼叫时，call_start.callee_id 必须引用其 target_id，call_start.call_type 必须引用其 contact_type。",
        "用户给出区域或信道名称时，先使用 radio_resolve_zone_channel，再使用 zone_channel_active_set；实体类型必须以权威目录为准，不根据“组”“区”等后缀猜测。",
        "radio_resolve_zone_channel 和 zone_channel_active_set 的 mode 必须存在；允许 active、conventional、trunking，用户未明确说明时使用 conventional。",
        "请求最近、刚才、上一通电话的摘要时，先用 history_recent_calls 或 call_query 得到真实 call_id，再调用 call_summarize；禁止自行生成 call_id。",
        "创建持久待办使用 utility_todo_add；仅仅出现时间不代表提醒，用户明确要求记录到待办时仍属于待办。",
        "到时间主动提示使用 reminder_schedule；time_desc 优先保留用户原始时间表达，不要由模型自行计算绝对日期时间。",
        "用户明确同时要求记录待办并主动提醒时，可以同时规划 utility_todo_add 与 reminder_schedule。",
        "跨步骤引用必须是 JSON 字符串，精确格式为 ${step_id.field}；禁止输出 {\"$step_id.field\":\"integer\"}、{\"$step_id.field\":\"field\"} 或其他对象形式；引用步骤必须写入 depends_on。",
        "如果目标属于现有能力但必需实体无法唯一确定，返回 NEEDS_CLARIFICATION。",
        "如果现有能力无法完成目标，返回 NO_CAPABILITY。",
        "输出只保留当前 decision 必需字段：EXECUTE 输出 decision/goal/steps；NO_CAPABILITY 输出 decision/goal；NEEDS_CLARIFICATION 输出 decision/goal/clarification。不要输出空字段或任何服务端生成字段。",
        f"步骤数量不得超过 {max_steps}。",
    ]


def build_planner_prompt(
    *,
    request: str,
    context: dict[str, Any],
    capabilities: list[dict[str, Any]],
    max_steps: int,
) -> dict[str, Any]:
    """构造发送给语义规划模型的结构化中文提示。"""

    return {
        "request": str(request or "").strip(),
        "context": context or {},
        "capabilities": capabilities,
        "output_contract": {
            "decision": "EXECUTE|NO_CAPABILITY|NEEDS_CLARIFICATION",
            "goal": "简短中文目标",
            "steps": [{
                "id": "step_1",
                "capability": "capability_id",
                "arguments": {},
                "depends_on": [],
            }],
            "clarification": "仅 NEEDS_CLARIFICATION 时输出",
        },
        "rules": _rules(max_steps),
    }
