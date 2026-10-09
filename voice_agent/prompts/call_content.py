# -*- coding: utf-8 -*-
"""通话摘要与翻译提示词。"""
from __future__ import annotations

import json
from typing import Any


def build_call_summary_messages(
    turns: list[dict[str, Any]],
    limit: int,
    *,
    call_context: dict[str, Any],
) -> tuple[str, str]:
    system = (
        "你是通话摘要器。只依据提供的通话转写生成简洁、准确的中文摘要；"
        "不得补充转写中不存在的人名、时间、地点、决定或结论；优先保留行动事项和关键信息。"
        f"输出不超过{limit}个字符，不要添加标题、Markdown或解释。"
    )
    user = json.dumps(
        {"call_context": call_context, "turns": turns},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return system, user


def build_call_translation_messages(
    turns: list[dict[str, Any]],
    language: str,
    limit: int,
    *,
    call_context: dict[str, Any],
) -> tuple[str, str]:
    target = "英文" if language == "en" else "中文"
    system = (
        f"你是通话翻译器。只依据提供的转写翻译为{target}，保留说话人和原意；"
        "不得总结、补充或解释。"
        f"输出不超过{limit}个字符，不要添加 Markdown 代码块。"
    )
    user = json.dumps(
        {"target_language": language, "call_context": call_context, "turns": turns},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return system, user
