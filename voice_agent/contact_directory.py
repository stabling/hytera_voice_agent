# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/contact_directory.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.directory_support, .text_norm, .device_directory_adapter
# 主要公开符号：contact_map_path, load_contact_map, get_contact_map, get_contact_list, update_contact_map, get_contact_directory_generation, get_contact_names, resolve_contact, get_standby_contact
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""联系人白名单、设备 ID 和 1 起始索引的统一目录。

配置兼容 ``dict`` 与 ``list``：

- ``{"黄工": "7"}``
- ``["黄工", "赵工"]``（ID 默认分别为 ``1``、``2``）
- ``[{"name": "黄工", "id": "7"}]``
- ``[["黄工", "7"], ["赵工", "4"]]``

解析优先级为：规范名称/别名 → 显式设备 ID → 显式列表索引 → 保守模糊匹配。
"""

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
    extract_contact_patch as _extract_device_profile_patch,
    is_device_profile,
)
from .directory_support import (
    coerce_name_id_mapping,
    indexed_items,
    resolve_by_explicit_id,
    resolve_by_index,
)
from .text_norm import build_aliases_for_value, normalize_text

CONTACT_LOCK = threading.RLock()
CONTACT_MAP: dict[str, dict[str, str]] = {}
CONTACT_DIRECTORY_GENERATION = 0
# 设备档案（11.json 结构）是联系人+区域/信道的共享权威源：contact 与
# zone_channel 两个目录模块默认读取同一个档案文件，各自提取自己的视图。
# _CONTACT_RAW_PROFILE 缓存最近一次档案原文，update 收到档案时原样持久化
# （档案自包含，两个模块重启后都能从共享文件还原）；_CONTACT_DEVICE_SOURCED
# 抑制手写默认目录回退。
_CONTACT_DEVICE_SOURCED = False
_CONTACT_RAW_PROFILE: dict[str, Any] | None = None
# 历史中间版本写入的派生快照标记（仅识别，不再生成）。
_DIRECTORY_SOURCE_MARKER = "directory_source"
_DIRECTORY_SOURCE_DEVICE = "device_profile"
# 联系人/区域信道共享的设备档案默认文件名；旧两文件部署回退旧默认名。
_SHARED_PROFILE_FILENAME = "voice_device_directory.json"
_LEGACY_DEFAULT_FILENAME = "voice_contact_map.json"

DEFAULT_CONTACT_MAP: dict[str, dict[str, str]] = {
    "group": {"安保组": "5", "保障一组": "6", "消防二组": "3", "巡检组": "4"},
    "private": {"黄工": "7", "刘班长": "6", "赵工": "4", "周队长": "5"},
    "standby": {"赵工": "4"},
}

_KIND_ALIASES = {
    "group": "group",
    "group_contact": "group",
    "group_contacts": "group",
    "组呼": "group",
    "组呼联系人": "group",
    "private": "private",
    "private_contact": "private",
    "private_contacts": "private",
    "individual": "private",
    "个呼": "private",
    "个呼联系人": "private",
    "联系人": "private",
    "standby": "standby",
    "standby_contact": "standby",
    "待机": "standby",
    "待机联系人": "standby",
}

_ENTITY_WORDS = {
    "group": ("组呼联系人", "组呼", "群组", "小组", "组"),
    "private": ("个呼联系人", "私人联系人", "个人联系人", "联系人", "人员", "同事", "个呼"),
    "standby": ("待机联系人", "待机人员", "待机"),
}

_CALL_FILLERS = (
    "请",
    "帮我",
    "麻烦",
    "立即",
    "马上",
    "现在",
    "呼叫",
    "联系",
    "接通",
    "打给",
    "打过去",
    "拨给",
    "拨打",
    "连一下",
    "发起",
    "呼出",
    "呼一下",
    "叫一下",
    "找一下",
    "连接",
    "叫过来",
    "开个通话",
    "接进来",
    "通联",
    "通话",
    "个呼",
    "组呼",
    "联系人",
    "一下",
    "给",
    "和",
    "与",
    "对",
    "进行",
    "开始",
)


def contact_map_path() -> Path:
    raw = os.environ.get("VOICE_CONTACT_MAP_FILE", _SHARED_PROFILE_FILENAME).strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else project_root() / path


def _effective_path() -> Path:
    """实际加载/持久化路径：共享档案缺失时回退旧默认文件（兼容旧两文件部署）。"""

    configured = contact_map_path()
    if os.environ.get("VOICE_CONTACT_MAP_FILE") or configured.is_file():
        return configured
    legacy = project_root() / _LEGACY_DEFAULT_FILENAME
    return legacy if legacy.is_file() else configured


def _uses_shared_profile_file() -> bool:
    """是否运行在共享档案模式（未显式指定专用文件且落在共享默认名上）。"""

    return (
        not os.environ.get("VOICE_CONTACT_MAP_FILE")
        and _effective_path().name == _SHARED_PROFILE_FILENAME
    )


def _refresh_sibling_directory() -> None:
    """共享档案更新后，同步刷新区域/信道目录模块的内存视图。

    必须在释放本模块锁之后调用（避免与 zone_channel 模块的反向刷新
    互为 ABBA 死锁）；刷新失败不影响本模块结果，兄弟模块重启/下次
    加载会自行从共享档案同步。
    """

    try:
        from . import zone_channel_directory as sibling

        sibling.load_zone_channel_map()
    except Exception:
        pass


def _normalize_kind(value: object) -> str:
    key = str(value or "").strip().lower()
    kind = _KIND_ALIASES.get(key)
    if not kind:
        raise ValueError("contact type must be group, private or standby")
    return kind


def _clean_mapping(value: object) -> dict[str, str]:
    return coerce_name_id_mapping(
        value,
        name_keys=("name", "contact_name", "label", "title"),
        id_keys=("id", "callee_id", "contact_id", "device_id", "value"),
    )


def _empty_map() -> dict[str, dict[str, str]]:
    return {"group": {}, "private": {}, "standby": {}}


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

    source = data.get("contact_map", data)
    if isinstance(source, dict):
        for raw_kind, mapping in source.items():
            try:
                kind = _normalize_kind(raw_kind)
            except ValueError:
                continue
            patch[kind].update(_clean_mapping(mapping))

    records = data.get("contacts")
    if isinstance(records, list):
        # 扁平对象列表可通过 type/contact_type 指定联系人类型。
        grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in patch}
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                kind = _normalize_kind(item.get("type") or item.get("contact_type"))
            except ValueError:
                continue
            grouped[kind].append(item)
        for kind, items in grouped.items():
            patch[kind].update(_clean_mapping(items))
    return patch


def _save_locked() -> None:
    if _CONTACT_RAW_PROFILE is not None:
        # 设备档案自包含：原样持久化，共享文件同时保留联系人/区域/信道数据。
        payload: Any = _CONTACT_RAW_PROFILE
    elif _CONTACT_DEVICE_SOURCED:
        # 档案派生快照（历史版本写入）无法还原完整档案：跳过写回，
        # 避免把联系人子集写入共享文件破坏其他模块的数据。
        return
    elif _uses_shared_profile_file():
        # 共享档案模式下没有档案原文：两个目录模块共享同名文件，各自写入
        # 领域子集会互相覆盖；目录数据应由设备档案提供，此处不落盘。
        return
    else:
        payload = CONTACT_MAP
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


def load_contact_map() -> dict[str, dict[str, str]]:
    global CONTACT_MAP, CONTACT_DIRECTORY_GENERATION, _CONTACT_DEVICE_SOURCED, _CONTACT_RAW_PROFILE
    with CONTACT_LOCK:
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
            CONTACT_MAP = {kind: dict(loaded[kind]) for kind in _empty_map()}
        else:
            # 文件中显式提供的类型为权威列表；未提供的类型才回退默认值。
            CONTACT_MAP = {
                kind: dict(loaded[kind] or DEFAULT_CONTACT_MAP.get(kind, {})) for kind in _empty_map()
            }
        _CONTACT_DEVICE_SOURCED = device_sourced
        _CONTACT_RAW_PROFILE = profile
        CONTACT_DIRECTORY_GENERATION += 1
        if profile is None and not device_sourced:
            # 仅旧格式归一化回写；档案原文已在共享文件中，无需写回。
            _save_locked()
        return get_contact_map()


def get_contact_map() -> dict[str, dict[str, str]]:
    with CONTACT_LOCK:
        if not CONTACT_MAP:
            return load_contact_map()
        return {kind: dict(mapping) for kind, mapping in CONTACT_MAP.items()}


def get_contact_list() -> dict[str, list[dict[str, str | int]]]:
    """返回包含 1 起始 ``index`` 的列表视图，不破坏原映射接口。"""

    return {kind: indexed_items(mapping) for kind, mapping in get_contact_map().items()}


def update_contact_map(patch: dict[str, Any]) -> dict[str, dict[str, str]]:
    global CONTACT_MAP, CONTACT_DIRECTORY_GENERATION, _CONTACT_DEVICE_SOURCED, _CONTACT_RAW_PROFILE
    profile = is_device_profile(patch)
    with CONTACT_LOCK:
        if profile:
            # 设备档案是完整权威快照：整体替换而不是合并（设备侧已删除的
            # 联系人不残留；档案无 standby 概念，置空），并原样持久化——
            # 档案自包含，共享文件重启后联系人/区域/信道两个模块都能还原。
            incoming = _extract_device_profile_patch(patch)
            CONTACT_MAP = {kind: dict(incoming[kind]) for kind in _empty_map()}
            _CONTACT_DEVICE_SOURCED = True
            _CONTACT_RAW_PROFILE = patch
            CONTACT_DIRECTORY_GENERATION += 1
            _save_locked()
        else:
            incoming = _extract_patch(patch)
            if not any(incoming.values()):
                raise ValueError("no valid contacts supplied")
            CONTACT_MAP = _merge_maps(get_contact_map(), incoming)
            CONTACT_DIRECTORY_GENERATION += 1
            # 档案为权威源时手工合并只影响当前进程：_save_locked 会跳过或
            # 回写档案原文，绝不把联系人子集写入共享档案文件。
            _save_locked()
        result = get_contact_map()
    if profile:
        # 释放本模块锁后刷新兄弟模块，避免与反向刷新互为 ABBA 死锁。
        _refresh_sibling_directory()
    return result



def get_contact_directory_generation() -> int:
    """Return the in-memory directory generation without filesystem access."""
    with CONTACT_LOCK:
        return int(CONTACT_DIRECTORY_GENERATION)

def get_contact_names(kind: str) -> list[str]:
    return list(get_contact_map().get(_normalize_kind(kind), {}))


def _aliases_for_contact(kind: str, name: str) -> set[str]:
    entity_type = "group_contact" if kind == "group" else "person_contact"
    aliases = {normalize_text(x) for x in build_aliases_for_value(entity_type, name)}
    aliases.add(normalize_text(name))
    if kind in {"private", "standby"} and name.endswith("工") and len(name) >= 2:
        aliases.add(normalize_text(name[:-1] + "工程师"))
    if kind == "group" and name.endswith("组"):
        aliases.update({normalize_text(name + "群"), normalize_text(name[:-1] + "小组")})
    return {alias for alias in aliases if alias}


def _strip_call_fillers(text: str) -> str:
    result = text
    for word in _CALL_FILLERS:
        result = result.replace(word, "")
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", result)


def _best_similarity(query: str, alias: str) -> float:
    if not query or not alias:
        return 0.0
    if alias == query:
        return 1.0
    if alias in query:
        return 0.995
    cleaned = _strip_call_fillers(query)
    if alias == cleaned:
        return 0.99
    if alias in cleaned:
        return 0.98
    best = SequenceMatcher(None, cleaned, alias).ratio()
    for width in range(max(1, len(alias) - 1), min(len(cleaned), len(alias) + 1) + 1):
        for start in range(0, max(0, len(cleaned) - width) + 1):
            best = max(best, SequenceMatcher(None, cleaned[start : start + width], alias).ratio())
    return best


def _resolved_result(
    kind: str,
    item: dict[str, str | int],
    *,
    match_mode: str,
    matched_alias: str = "",
    score: float = 1.0,
) -> dict[str, str | float | int]:
    return {
        "type": kind,
        "name": str(item["name"]),
        "callee_id": str(item["id"]),
        "index": int(item["index"]),
        "match_mode": match_mode,
        "matched_alias": matched_alias or str(item["name"]),
        "score": round(score, 4),
    }


def resolve_contact(
    name_or_text: object, contact_type: str | None = None
) -> dict[str, str | float | int] | None:
    """按名称、ID、索引或 ASR 模糊文本解析联系人。"""

    query = normalize_text(name_or_text)
    if not query:
        return None
    kinds = ("group", "private") if contact_type is None else (_normalize_kind(contact_type),)
    # 明确说“个呼/个人联系人”时不解析组呼索引，反之亦然。
    if any(word in query for word in ("组呼", "群组", "小组", "组联系人")):
        kinds = tuple(kind for kind in kinds if kind == "group")
    elif any(word in query for word in ("个呼", "个人联系人", "私人联系人")):
        kinds = tuple(kind for kind in kinds if kind in {"private", "standby"})
    directory = get_contact_map()

    # 1) 规范名称/别名精确命中，避免 D3、数字姓名等被误当索引。
    for kind in kinds:
        for item in indexed_items(directory.get(kind, {})):
            for alias in _aliases_for_contact(kind, str(item["name"])):
                if query == alias or alias in query:
                    return _resolved_result(kind, item, match_mode="name", matched_alias=alias)

    # 2) 只有带 ID/编号/号码提示时才按设备 ID 路由。
    for kind in kinds:
        match = resolve_by_explicit_id(
            directory.get(kind, {}), name_or_text, entity_words=_ENTITY_WORDS[kind]
        )
        if match:
            return _resolved_result(kind, match, match_mode="id")

    # 3) “第二个联系人/序号2”等按该类型列表的 1 起始索引解析。
    for kind in kinds:
        match = resolve_by_index(
            directory.get(kind, {}), name_or_text, entity_words=_ENTITY_WORDS[kind]
        )
        if match:
            return _resolved_result(kind, match, match_mode="index")

    # 4) 最后才进行保守模糊匹配，防止未知人名被错误路由。
    candidates: list[tuple[float, int, str, dict[str, str | int], str]] = []
    for kind in kinds:
        for item in indexed_items(directory.get(kind, {})):
            for alias in _aliases_for_contact(kind, str(item["name"])):
                candidates.append((_best_similarity(query, alias), len(alias), kind, item, alias))
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    score, _, kind, item, alias = candidates[0]
    threshold = 0.96 if len(alias) <= 2 else 0.89
    return (
        _resolved_result(kind, item, match_mode="fuzzy", matched_alias=alias, score=score)
        if score >= threshold
        else None
    )


def get_standby_contact() -> dict[str, str | int] | None:
    items = indexed_items(get_contact_map().get("standby", {}))
    if not items:
        return None
    item = items[0]
    return {
        "type": "standby",
        "name": str(item["name"]),
        "callee_id": str(item["id"]),
        "index": int(item["index"]),
    }
