# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/zone_channel_directory.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.directory_support, .text_norm, .device_directory_adapter
# 主要公开符号：zone_channel_map_path, load_zone_channel_map, get_zone_channel_map, get_zone_channel_list, update_zone_channel_map, get_zone_channel_directory_generation, get_zone_channel_names, resolve_zone_channel, resolve_zone_channel_matches
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""区域/信道名称、设备 ID 与 1 起始索引的统一目录。"""

from __future__ import annotations

import json
import os
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .env import project_root
from .device_directory_adapter import (
    extract_zone_channel_patch as _extract_device_profile_patch,
    is_device_profile,
)
from .directory_support import (
    coerce_name_id_mapping,
    indexed_items,
    resolve_by_explicit_id,
    resolve_by_index,
)
from .text_norm import arabic_digit_text_to_cn, cn_digit_text_to_arabic, normalize_text

ZONE_CHANNEL_LOCK = threading.RLock()
ZONE_CHANNEL_MAP: dict[str, dict[str, str]] = {}
ZONE_CHANNEL_DIRECTORY_GENERATION = 0
# 设备档案（11.json 结构）是联系人+区域/信道的共享权威源：zone_channel 与
# contact 两个目录模块默认读取同一个档案文件，各自提取自己的视图。
# _ZONE_CHANNEL_RAW_PROFILE 缓存最近一次档案原文，update 收到档案时原样
# 持久化（档案自包含，两个模块重启后都能从共享文件还原）；
# _ZONE_CHANNEL_DEVICE_SOURCED 抑制手写默认目录回退。
_ZONE_CHANNEL_DEVICE_SOURCED = False
_ZONE_CHANNEL_RAW_PROFILE: dict[str, Any] | None = None
# 历史中间版本写入的派生快照标记（仅识别，不再生成）。
_DIRECTORY_SOURCE_MARKER = "directory_source"
_DIRECTORY_SOURCE_DEVICE = "device_profile"
# 联系人/区域信道共享的设备档案默认文件名；旧两文件部署回退旧默认名。
_SHARED_PROFILE_FILENAME = "voice_device_directory.json"
_LEGACY_DEFAULT_FILENAME = "voice_zone_channel_map.json"

DEFAULT_ZONE_CHANNEL_MAP = {
    "zones": {"区域1": "1", "区域2": "2"},
    "channels": {"D1": "1", "D2": "2", "D3": "3", "D4": "4", "D5": "5", "D6": "6"},
}

_KIND_ALIASES = {
    "zone": "zones",
    "zones": "zones",
    "area": "zones",
    "areas": "zones",
    "region": "zones",
    "regions": "zones",
    "区域": "zones",
    "区域列表": "zones",
    "channel": "channels",
    "channels": "channels",
    "信道": "channels",
    "频道": "channels",
    "信道列表": "channels",
    "频道列表": "channels",
}
_ENTITY_WORDS = {
    "zones": ("区域", "分区", "区域列表"),
    "channels": ("信道", "频道", "通道", "信道列表"),
}
_SWITCH_FILLERS = (
    "请",
    "帮我",
    "麻烦",
    "立即",
    "马上",
    "现在",
    "切换",
    "切到",
    "进入",
    "转到",
    "换到",
    "换成",
    "跳到",
    "拨到",
    "调到",
    "调至",
    "切回",
    "调整到",
    "改到",
    "改成",
    "转入",
    "切一下",
    "定位",
    "选择",
    "选中",
    "目标",
    "当前",
    "指定",
    "设置为",
    "设为",
    "把",
    "至",
    "到",
    "的",
)


def zone_channel_map_path() -> Path:
    raw = os.environ.get("VOICE_ZONE_CHANNEL_MAP_FILE", _SHARED_PROFILE_FILENAME).strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else project_root() / path


def _effective_path() -> Path:
    """实际加载/持久化路径：共享档案缺失时回退旧默认文件（兼容旧两文件部署）。"""

    configured = zone_channel_map_path()
    if os.environ.get("VOICE_ZONE_CHANNEL_MAP_FILE") or configured.is_file():
        return configured
    legacy = project_root() / _LEGACY_DEFAULT_FILENAME
    return legacy if legacy.is_file() else configured


def _uses_shared_profile_file() -> bool:
    """是否运行在共享档案模式（未显式指定专用文件且落在共享默认名上）。"""

    return (
        not os.environ.get("VOICE_ZONE_CHANNEL_MAP_FILE")
        and _effective_path().name == _SHARED_PROFILE_FILENAME
    )


def _refresh_sibling_directory() -> None:
    """共享档案更新后，同步刷新联系人目录模块的内存视图。

    必须在释放本模块锁之后调用（避免与 contact 模块的反向刷新互为
    ABBA 死锁）；刷新失败不影响本模块结果，兄弟模块重启/下次加载
    会自行从共享档案同步。
    """

    try:
        from . import contact_directory as sibling

        sibling.load_contact_map()
    except Exception:
        pass


def _normalize_kind(value: object) -> str:
    key = str(value or "").strip().lower()
    kind = _KIND_ALIASES.get(key)
    if not kind:
        raise ValueError("kind must be zone/area or channel")
    return kind


def _clean_mapping(value: object) -> dict[str, str]:
    return coerce_name_id_mapping(
        value,
        name_keys=("name", "zone_name", "channel_name", "label", "title"),
        id_keys=("id", "zone_id", "channel_id", "device_id", "value"),
    )


def _empty_map() -> dict[str, dict[str, str]]:
    return {"zones": {}, "channels": {}}


def _merge_maps(
    base: dict[str, dict[str, str]], patch: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    merged = {kind: dict(base.get(kind, {})) for kind in _empty_map()}
    for kind in merged:
        merged[kind].update(_clean_mapping(patch.get(kind, {})))
    return merged


def _extract_patch(data: object) -> dict[str, dict[str, str]]:
    if is_device_profile(data):
        return _extract_device_profile_patch(data)
    patch = _empty_map()
    if not isinstance(data, dict):
        return patch
    source = data.get("zone_channel_map", data)
    if isinstance(source, dict):
        for raw_kind, mapping in source.items():
            try:
                kind = _normalize_kind(raw_kind)
            except ValueError:
                continue
            patch[kind].update(_clean_mapping(mapping))

    for field, kind in (("zones", "zones"), ("channels", "channels")):
        if isinstance(data.get(field), list):
            patch[kind].update(_clean_mapping(data[field]))
    records = data.get("items")
    if isinstance(records, list):
        grouped = _empty_map()
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                kind = _normalize_kind(item.get("type") or item.get("kind"))
            except ValueError:
                continue
            grouped[kind][str(len(grouped[kind]) + 1)] = item  # 临时保持顺序
        for kind, indexed in grouped.items():
            patch[kind].update(_clean_mapping(list(indexed.values())))
    return patch


def _save_locked() -> None:
    if _ZONE_CHANNEL_RAW_PROFILE is not None:
        # 设备档案自包含：原样持久化，共享文件同时保留联系人/区域/信道数据。
        payload: Any = _ZONE_CHANNEL_RAW_PROFILE
    elif _ZONE_CHANNEL_DEVICE_SOURCED:
        # 档案派生快照（历史版本写入）无法还原完整档案：跳过写回，
        # 避免把区域/信道子集写入共享文件破坏其他模块的数据。
        return
    elif _uses_shared_profile_file():
        # 共享档案模式下没有档案原文：两个目录模块共享同名文件，各自写入
        # 领域子集会互相覆盖；目录数据应由设备档案提供，此处不落盘。
        return
    else:
        payload = ZONE_CHANNEL_MAP
    path = _effective_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _device_sourced(parsed: object) -> bool:
    """输入是原始设备档案，或由档案派生并带来源标记的快照。"""

    return is_device_profile(parsed) or (
        isinstance(parsed, dict)
        and parsed.get(_DIRECTORY_SOURCE_MARKER) == _DIRECTORY_SOURCE_DEVICE
    )


def load_zone_channel_map() -> dict[str, dict[str, str]]:
    global ZONE_CHANNEL_MAP, ZONE_CHANNEL_DIRECTORY_GENERATION, _ZONE_CHANNEL_DEVICE_SOURCED, _ZONE_CHANNEL_RAW_PROFILE
    with ZONE_CHANNEL_LOCK:
        loaded = _empty_map()
        profile: dict[str, Any] | None = None
        device_sourced = False
        path = _effective_path()
        if path.is_file():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                device_sourced = _device_sourced(parsed)
                if is_device_profile(parsed):
                    profile = parsed
                loaded = _extract_patch(parsed)
            except (OSError, json.JSONDecodeError, ValueError):
                loaded = _empty_map()
        if device_sourced:
            # 设备档案（或其派生快照）是完整权威目录：不回退手写默认值。
            ZONE_CHANNEL_MAP = {kind: dict(loaded[kind]) for kind in _empty_map()}
        else:
            # 文件中显式提供的列表为权威顺序；缺失类型才使用默认值。
            ZONE_CHANNEL_MAP = {
                kind: dict(loaded[kind] or DEFAULT_ZONE_CHANNEL_MAP.get(kind, {}))
                for kind in _empty_map()
            }
        _ZONE_CHANNEL_DEVICE_SOURCED = device_sourced
        _ZONE_CHANNEL_RAW_PROFILE = profile
        ZONE_CHANNEL_DIRECTORY_GENERATION += 1
        if profile is None and not device_sourced:
            # 仅旧格式归一化回写；档案原文已在共享文件中，无需写回。
            _save_locked()
        return get_zone_channel_map()


def get_zone_channel_map() -> dict[str, dict[str, str]]:
    with ZONE_CHANNEL_LOCK:
        if not ZONE_CHANNEL_MAP:
            return load_zone_channel_map()
        return {kind: dict(mapping) for kind, mapping in ZONE_CHANNEL_MAP.items()}


def get_zone_channel_list() -> dict[str, list[dict[str, str | int]]]:
    return {kind: indexed_items(mapping) for kind, mapping in get_zone_channel_map().items()}


def update_zone_channel_map(patch: dict[str, Any]) -> dict[str, dict[str, str]]:
    global ZONE_CHANNEL_MAP, ZONE_CHANNEL_DIRECTORY_GENERATION, _ZONE_CHANNEL_DEVICE_SOURCED, _ZONE_CHANNEL_RAW_PROFILE
    profile = is_device_profile(patch)
    with ZONE_CHANNEL_LOCK:
        if profile:
            # 设备档案是完整权威快照：整体替换而不是合并（设备侧已删除的
            # 区域/信道不残留），并原样持久化——档案自包含，共享文件重启
            # 后联系人/区域/信道两个模块都能还原。
            incoming = _extract_device_profile_patch(patch)
            ZONE_CHANNEL_MAP = {kind: dict(incoming[kind]) for kind in _empty_map()}
            _ZONE_CHANNEL_DEVICE_SOURCED = True
            _ZONE_CHANNEL_RAW_PROFILE = patch
            ZONE_CHANNEL_DIRECTORY_GENERATION += 1
            _save_locked()
        else:
            incoming = _extract_patch(patch)
            if not any(incoming.values()):
                raise ValueError("no valid zones or channels supplied")
            ZONE_CHANNEL_MAP = _merge_maps(get_zone_channel_map(), incoming)
            ZONE_CHANNEL_DIRECTORY_GENERATION += 1
            # 档案为权威源时手工合并只影响当前进程：_save_locked 会跳过或
            # 回写档案原文，绝不把区域/信道子集写入共享档案文件。
            _save_locked()
        result = get_zone_channel_map()
    if profile:
        # 释放本模块锁后刷新兄弟模块，避免与反向刷新互为 ABBA 死锁。
        _refresh_sibling_directory()
    return result



def get_zone_channel_directory_generation() -> int:
    """Return the in-memory directory generation without filesystem access."""
    with ZONE_CHANNEL_LOCK:
        return int(ZONE_CHANNEL_DIRECTORY_GENERATION)

def get_zone_channel_names(kind: str) -> list[str]:
    return list(get_zone_channel_map().get(_normalize_kind(kind), {}))


def _aliases_for_zone(name: str) -> set[str]:
    base = normalize_text(name)
    aliases = {base, arabic_digit_text_to_cn(base), cn_digit_text_to_arabic(base)}
    match = re.fullmatch(r"(?:区域|分区)?([0-9一二三四五六七八九十]+)", base)
    if match:
        number = match.group(1)
        aliases.update(
            {
                normalize_text("区域" + number),
                normalize_text("分区" + number),
                normalize_text("第" + number + "区域"),
                normalize_text(number + "区"),
                arabic_digit_text_to_cn(normalize_text("区域" + number)),
                cn_digit_text_to_arabic(normalize_text("区域" + number)),
            }
        )
    return {alias for alias in aliases if alias}


def _aliases_for_channel(name: str) -> set[str]:
    base = normalize_text(name)
    aliases = {base, arabic_digit_text_to_cn(base), cn_digit_text_to_arabic(base)}
    if re.fullmatch(r"[A-Z][0-9]+", base):
        letter, number = base[0], base[1:]
        aliases.update(
            {
                normalize_text(f"{letter}{number}信道"),
                normalize_text(f"{letter}{number}频道"),
                normalize_text(f"{letter} {number}"),
                normalize_text(f"{letter}杠{number}"),
                arabic_digit_text_to_cn(base),
            }
        )
    return {alias for alias in aliases if alias}


def _aliases_for_channel_item(item: dict[str, str | int]) -> set[str]:
    """返回信道名称别名，并补充稳定的 ``D<设备ID/索引>`` 简写。

    权威目录的展示名可以是“消防组”等业务名称，但设备操作中仍常使用 D2 这类
    信道编号。别名只用于解析，不修改目录名称，也不改变最终返回的权威 name/id。
    """

    aliases = _aliases_for_channel(str(item["name"]))
    numeric_tokens = {str(item.get("id") or "").strip(), str(item.get("index") or "").strip()}
    for token in numeric_tokens:
        if not token.isdigit():
            continue
        aliases.update(_aliases_for_channel(f"D{token}"))
    return {alias for alias in aliases if alias}


def _strip_switch_fillers(text: str) -> str:
    result = text
    for word in _SWITCH_FILLERS:
        result = result.replace(word, "")
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", result)


def _best_similarity(query: str, alias: str) -> float:
    if alias == query:
        return 1.0
    if alias and alias in query:
        return 0.995
    cleaned = _strip_switch_fillers(query)
    if alias == cleaned:
        return 0.99
    if alias and alias in cleaned:
        return 0.98
    best = SequenceMatcher(None, cleaned, alias).ratio() if alias else 0.0
    for width in range(max(1, len(alias) - 1), min(len(cleaned), len(alias) + 1) + 1):
        for start in range(0, max(0, len(cleaned) - width) + 1):
            best = max(best, SequenceMatcher(None, cleaned[start : start + width], alias).ratio())
    return best


def _result(
    normalized_kind: str,
    item: dict[str, str | int],
    *,
    mode: str,
    alias: str = "",
    score: float = 1.0,
) -> dict[str, str | float | int]:
    return {
        "type": "zone" if normalized_kind == "zones" else "channel",
        "name": str(item["name"]),
        "id": str(item["id"]),
        "index": int(item["index"]),
        "match_mode": mode,
        "matched_alias": alias or str(item["name"]),
        "score": round(score, 4),
    }


def resolve_zone_channel(name_or_text: object, kind: str) -> dict[str, str | float | int] | None:
    """按名称、设备 ID、1 起始索引或保守模糊匹配解析区域/信道。"""

    normalized_kind = _normalize_kind(kind)
    query = normalize_text(name_or_text)
    if not query:
        return None
    mapping = get_zone_channel_map().get(normalized_kind, {})
    alias_builder = _aliases_for_zone if normalized_kind == "zones" else _aliases_for_channel

    def aliases_for_item(item: dict[str, str | int]) -> set[str]:
        if normalized_kind == "channels":
            return _aliases_for_channel_item(item)
        return alias_builder(str(item["name"]))
    has_zone_word = any(word in query for word in ("区域", "分区", "区列表"))
    has_channel_word = any(word in query for word in ("信道", "频道", "通道"))

    # Named directory entities are authoritative even when the same utterance also
    # contains an explicit word for the other entity type (for example
    # ``区域1交通组``).  Apply type-word guards only after direct name scanning so a
    # single natural-language phrase can legitimately resolve both entities.
    for item in indexed_items(mapping):
        for alias in aliases_for_item(item):
            if query == alias or alias in query:
                return _result(normalized_kind, item, mode="name", alias=alias)

    if normalized_kind == "zones" and has_channel_word and not has_zone_word:
        return None
    if normalized_kind == "channels" and has_zone_word and not has_channel_word:
        return None

    explicit_id = resolve_by_explicit_id(
        mapping, name_or_text, entity_words=_ENTITY_WORDS[normalized_kind]
    )
    if explicit_id:
        return _result(normalized_kind, explicit_id, mode="id")

    indexed = resolve_by_index(mapping, name_or_text, entity_words=_ENTITY_WORDS[normalized_kind])
    if indexed:
        return _result(normalized_kind, indexed, mode="index")

    candidates: list[tuple[float, int, dict[str, str | int], str]] = []
    for item in indexed_items(mapping):
        for alias in aliases_for_item(item):
            candidates.append((_best_similarity(query, alias), len(alias), item, alias))
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    score, _, item, alias = candidates[0]
    threshold = 0.99 if len(alias) <= 2 else 0.90
    return (
        _result(normalized_kind, item, mode="fuzzy", alias=alias, score=score)
        if score >= threshold
        else None
    )


def resolve_zone_channel_matches(
    name_or_text: object,
    *,
    hinted_kind: str | None = None,
) -> dict[str, dict[str, str | float | int]]:
    """Scan both authoritative directories and return every applicable entity hit.

    ``hinted_kind`` is intentionally only a tie-breaker for bare numeric ID/index
    inputs, because the same number can legitimately exist in both directories.
    For named and fuzzy matches the directory hit is authoritative, so a planner
    putting ``交通组`` in a ``zone`` field still resolves as a channel.
    """

    matches: dict[str, dict[str, str | float | int]] = {}
    for actual_kind in ("zone", "channel"):
        match = resolve_zone_channel(name_or_text, actual_kind)
        if match is not None:
            matches[actual_kind] = match

    if len(matches) <= 1:
        return matches

    try:
        hint = (
            "zone"
            if _normalize_kind(hinted_kind) == "zones"
            else "channel"
        ) if hinted_kind else None
    except ValueError:
        hint = None

    # A bare numeric value can be both zone #1 and channel #1.  Retain the
    # caller's field hint only for that genuinely ambiguous representation.
    if hint in matches and all(
        str(match.get("match_mode") or "") in {"id", "index"}
        for match in matches.values()
    ):
        return {hint: matches[hint]}

    return matches
