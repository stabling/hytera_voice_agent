# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/text_norm.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：normalize_text, arabic_digit_text_to_cn, cn_digit_text_to_arabic, add_alias, build_aliases_for_value
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Chinese ASR text normalization and entity alias generation."""

from __future__ import annotations

import re

CN_DIGIT_TO_ARABIC = {
    "零": "0",
    "〇": "0",
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}
ARABIC_TO_CN_DIGIT = {
    "0": "零",
    "1": "一",
    "2": "二",
    "3": "三",
    "4": "四",
    "5": "五",
    "6": "六",
    "7": "七",
    "8": "八",
    "9": "九",
}


def normalize_text(text: object) -> str:
    text = "" if text is None else str(text)
    text = text.strip().upper().replace("臺", "台")
    text = re.sub(r"[\s，。！？；：,.!?;:\"'“”‘’（）()\[\]{}<>《》、\-_/\\|~`]+", "", text)
    replace_pairs = [
        ("写到", "切到"),
        ("切道", "切到"),
        ("去到", "切到"),
        ("截到", "切到"),
        ("立接到", "切到"),
        ("立即到", "切到"),
        ("接到", "切到"),
        ("换道", "换到"),
        ("调道", "调到"),
        ("调制", "调至"),
        ("族群", "组群"),
        ("主群", "组群"),
        ("租群", "组群"),
        ("组琴", "组群"),
        ("消防2组", "消防二组"),
        ("消防二阻", "消防二组"),
        ("消防二祖", "消防二组"),
        ("信到", "信道"),
        ("频道道", "频道"),
        ("频到", "频道"),
        ("短信", "短消息"),
        ("连系", "联系"),
        ("联戏", "联系"),
        ("呼交", "呼叫"),
        ("胡叫", "呼叫"),
        ("波打", "拨打"),
        ("波给", "拨给"),
        ("信号", "信道"),
        ("频道号", "频道"),
        ("第俩个", "第二个"),
        ("第二各", "第二个"),
        ("序列号", "序号"),
        ("索引号", "索引"),
        ("区域一", "区域1"),
        ("区域二", "区域2"),
    ]
    for wrong, right in replace_pairs:
        text = text.replace(wrong, right)
    return text


def arabic_digit_text_to_cn(s: object) -> str:
    return "".join(ARABIC_TO_CN_DIGIT.get(ch, ch) for ch in str(s))


def cn_digit_text_to_arabic(s: object) -> str:
    return "".join(CN_DIGIT_TO_ARABIC.get(ch, ch) for ch in str(s))


def add_alias(alias_map: dict[str, str], alias: object, canonical: object) -> None:
    alias_norm = normalize_text(alias)
    canonical_s = str(canonical or "").strip()
    if alias_norm and canonical_s:
        alias_map[alias_norm] = canonical_s


def build_aliases_for_value(
    entity_type: str, value: str, alias_text: str | None = None
) -> set[str]:
    aliases: set[str] = set()
    value = str(value or "").strip()
    alias_text = str(alias_text or "").strip()
    if value:
        aliases.add(value)
    if alias_text:
        for part in re.split(r"[/|,，、;；]+", alias_text):
            part = part.strip()
            if part:
                aliases.add(part)

    if entity_type == "channel":
        for x in list(aliases):
            if "信道" in x:
                aliases.add(x.replace("信道", "频道"))
            if "频道" in x:
                aliases.add(x.replace("频道", "信道"))
        if value.startswith("CH"):
            no_suffix = re.sub(r"(信道|频道)$", "", value)
            digit_part = no_suffix[2:]
            arabic = cn_digit_text_to_arabic(digit_part)
            cn = arabic_digit_text_to_cn(arabic)
            for suffix in ("信道", "频道", ""):
                aliases.add("CH" + digit_part + suffix)
                aliases.add("CH" + arabic + suffix)
                aliases.add("CH" + cn + suffix)
        if value.endswith("频道"):
            aliases.add(value.replace("频道", "信道"))

    if entity_type == "person_contact":
        for x in list(aliases):
            aliases.add(arabic_digit_text_to_cn(x))
            aliases.add(cn_digit_text_to_arabic(x))
        aliases.update(
            {
                value.replace("一一九", "119"),
                value.replace("二零七", "207"),
                value.replace("三零一", "301"),
                value.replace("三零二", "302"),
                value.replace("零一", "01"),
                value.replace("零二", "02"),
            }
        )

    if entity_type == "group_contact":
        if value.endswith("组群"):
            aliases.add(value[:-1])
        if value.endswith("组"):
            aliases.add(value + "群")

    if entity_type == "message_type":
        if "短消息" in value:
            aliases.add(value.replace("短消息", "短信"))
        if value == "新消息":
            aliases.add("新短消息")
            aliases.add("新短信")

    return aliases
