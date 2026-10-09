# -*- coding: utf-8 -*-
"""ASR 上下文提示词。"""
from __future__ import annotations


def build_asr_context_prompt(
    *,
    private_names: list[str],
    group_names: list[str],
    zone_names: list[str],
    channel_names: list[str],
    domain_terms: tuple[str, ...] | list[str],
) -> str:
    sections = ["海能达数字对讲机语音控制场景。"]
    if private_names:
        sections.append("私人联系人：" + "、".join(private_names) + "。")
    if group_names:
        sections.append("群组：" + "、".join(group_names) + "。")
    if zone_names:
        sections.append("区域：" + "、".join(zone_names) + "。")
    if channel_names:
        sections.append("信道：" + "、".join(channel_names) + "。")
    if domain_terms:
        sections.append("业务术语：" + "、".join(domain_terms) + "。")
    sections.append("请忠实转写；联系人、区域和字母数字信道按权威目录原样输出，不要自行改写实体名称。")
    return "".join(sections)
