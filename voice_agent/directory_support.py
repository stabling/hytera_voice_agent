# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/directory_support.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.text_norm
# 主要公开符号：coerce_name_id_mapping, indexed_items, chinese_number_to_int, extract_explicit_index, extract_explicit_id, resolve_by_index, resolve_by_explicit_id
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""联系人、区域和信道目录的通用解析能力。

兼容两类配置：
1. 映射：``{"名称": "设备ID"}``；
2. 列表：字符串列表、对象列表或 ``[名称, ID]`` 二元列表。

列表顺序同时构成从 1 开始的业务索引。名称、显式 ID 和显式序号采用不同
语义，降低 ASR 把设备 ID 误识别成“第几个”的风险。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Iterable

from .text_norm import normalize_text

_NAME_KEYS = ("name", "label", "title", "display_name", "contact_name", "zone_name", "channel_name")
_ID_KEYS = ("id", "value", "device_id", "callee_id", "contact_id", "zone_id", "channel_id")
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _first_text(record: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = record.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def coerce_name_id_mapping(
    value: object,
    *,
    name_keys: Sequence[str] = _NAME_KEYS,
    id_keys: Sequence[str] = _ID_KEYS,
) -> dict[str, str]:
    """把映射或列表统一转换为保持顺序的 ``名称 -> ID`` 字典。

    字符串列表未提供设备 ID 时，以其 1 起始位置作为 ID；对象列表未提供 ID
    时也采用该规则。该行为让简单配置立即可用，同时仍支持显式真实 ID。
    """

    result: dict[str, str] = {}
    if isinstance(value, Mapping):
        for raw_name, raw_value in value.items():
            name = str(raw_name or "").strip()
            if isinstance(raw_value, Mapping):
                embedded_name = _first_text(raw_value, name_keys)
                embedded_id = _first_text(raw_value, id_keys)
                name = embedded_name or name
                item_id = embedded_id
            else:
                item_id = str(raw_value or "").strip()
            if name and item_id:
                result[name] = item_id
        return result

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return result

    for index, raw_item in enumerate(value, start=1):
        name = ""
        item_id = ""
        if isinstance(raw_item, Mapping):
            name = _first_text(raw_item, name_keys)
            item_id = _first_text(raw_item, id_keys)
        elif (
            isinstance(raw_item, Sequence)
            and not isinstance(raw_item, (str, bytes, bytearray))
            and len(raw_item) >= 1
        ):
            name = str(raw_item[0] or "").strip()
            item_id = str(raw_item[1] or "").strip() if len(raw_item) > 1 else ""
        else:
            name = str(raw_item or "").strip()
        if name:
            result[name] = item_id or str(index)
    return result


def indexed_items(mapping: Mapping[str, object]) -> list[dict[str, str | int]]:
    """导出包含 1 起始索引的稳定列表视图。"""

    return [
        {"index": index, "name": str(name), "id": str(item_id)}
        for index, (name, item_id) in enumerate(mapping.items(), start=1)
    ]


def chinese_number_to_int(text: object) -> int | None:
    """解析常见阿拉伯数字或中文整数（支持到千位）。"""

    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if not all(char in _CN_DIGITS or char in _CN_UNITS for char in raw):
        return None
    if not any(char in _CN_UNITS for char in raw):
        digits = "".join(str(_CN_DIGITS[char]) for char in raw)
        return int(digits) if digits else None

    total = 0
    current = 0
    for char in raw:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
            continue
        unit = _CN_UNITS[char]
        total += (current or 1) * unit
        current = 0
    return total + current


def extract_explicit_index(value: object, *, entity_words: Sequence[str] = ()) -> int | None:
    """从“第二个/序号3/索引2/列表第4项”等表达中提取 1 起始索引。

    纯整数对象也视为索引；纯数字字符串只有在没有 ``ID/编号`` 标记时才
    作为索引。显式 ID 由 :func:`extract_explicit_id` 处理。
    """

    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 1 else None
    text = normalize_text(value)
    if not text or re.search(r"(?:设备|联系人|区域|信道|频道)?(?:ID|编号|号码)", text):
        return None

    numeral = r"([0-9零〇一二两三四五六七八九十百千]+)"
    patterns = [
        rf"第{numeral}(?:个|位|项|条)?",
        rf"(?:序号|索引|列表位置|列表第){numeral}(?:个|位|项|条)?",
    ]
    if entity_words:
        words = "|".join(re.escape(normalize_text(word)) for word in entity_words if word)
        if words:
            patterns.extend(
                [
                    rf"(?:{words})第?{numeral}(?:个|位|项|条)?",
                    rf"第{numeral}(?:个|位|项|条)?(?:{words})",
                ]
            )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            index = chinese_number_to_int(match.group(1))
            return index if index and index >= 1 else None
    if re.fullmatch(numeral, text):
        index = chinese_number_to_int(text)
        return index if index and index >= 1 else None
    return None


def extract_explicit_id(value: object, *, entity_words: Sequence[str] = ()) -> str | None:
    """提取“联系人ID7/信道编号D3/设备号12”中的显式设备 ID。"""

    text = normalize_text(value)
    if not text:
        return None
    prefix = ""
    if entity_words:
        prefix = "(?:" + "|".join(re.escape(normalize_text(word)) for word in entity_words) + ")?"
    match = re.search(rf"{prefix}(?:设备)?(?:ID|编号|号码|号)[:：]?([A-Z0-9_-]+)", text)
    return match.group(1) if match else None


def resolve_by_index(
    mapping: Mapping[str, object],
    value: object,
    *,
    entity_words: Sequence[str] = (),
) -> dict[str, str | int] | None:
    index = extract_explicit_index(value, entity_words=entity_words)
    items = indexed_items(mapping)
    if index is None or index > len(items):
        return None
    return dict(items[index - 1])


def resolve_by_explicit_id(
    mapping: Mapping[str, object],
    value: object,
    *,
    entity_words: Sequence[str] = (),
) -> dict[str, str | int] | None:
    item_id = extract_explicit_id(value, entity_words=entity_words)
    if not item_id:
        return None
    normalized_id = normalize_text(item_id)
    for item in indexed_items(mapping):
        if normalize_text(item["id"]) == normalized_id:
            return item
    return None
