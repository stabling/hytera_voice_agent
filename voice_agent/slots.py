# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/slots.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.contact_directory, .zone_channel_directory
# 主要公开符号：load_business_slots, get_business_slots, refresh_contact_slots, refresh_zone_channel_slots
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Read-only business entities derived from authoritative directories."""

from __future__ import annotations

from .contact_directory import get_contact_names, load_contact_map
from .zone_channel_directory import get_zone_channel_names, load_zone_channel_map


def load_business_slots() -> dict[str, list[str]]:
    """Load authoritative entity directories and return planner/ASR hints."""
    load_contact_map()
    load_zone_channel_map()
    return get_business_slots()


def get_business_slots() -> dict[str, list[str]]:
    """Return display names only; IDs remain owned by deterministic resolvers."""
    return {
        "private_contacts": get_contact_names("private"),
        "group_contacts": get_contact_names("group"),
        "areas": get_zone_channel_names("zone"),
        "channels": get_zone_channel_names("channel"),
    }


def refresh_contact_slots() -> dict[str, list[str]]:
    return get_business_slots()


def refresh_zone_channel_slots() -> dict[str, list[str]]:
    return get_business_slots()
