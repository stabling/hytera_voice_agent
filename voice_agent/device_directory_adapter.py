# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/device_directory_adapter.py
# 所属层次：语音智能体核心 / 设备目录适配
# 主要职责：把终端导出的原始设备档案（BasicInfo/Convention/DigitalTrunking，
#           样例见仓库根 11.json）转换为联系人目录与区域/信道目录的内部权威格式。
# 项目内直接依赖：无（纯函数；加载/合并/持久化由 contact_directory 与
#           zone_channel_directory 复用现有链路）
# 主要公开符号：is_device_profile, extract_contact_patch, extract_zone_channel_patch
# 兼容性：只做格式转换，不改目录解析/别名/校验逻辑；旧手写
#         {"zones"/"channels"} 与 {"group"/"private"/"standby"} 格式继续原路径加载。
# 修改原则：实体类型只依据设备自身声明（DigitalService.Contact.CallType、
#           trunking GroupCall/PrivateCall 列表归属）；无法识别的类型跳过，不猜测。
# 排障顺序：先确认输入是设备档案（is_device_profile），再对照 11.json 字段核对
#           提取结果，最后看下游目录解析（resolve_*）日志。
# =============================================================================
"""Terminal device profile (11.json) → agent directory format adapter.

设备档案结构（终端导出）::

    BasicInfo / Convention / DigitalTrunking
    Convention.Channel.{DigitalChannels,AnalogChannels}   信道列表（Alias/Index）
    Convention.DigitalService.Contact                     常规数字联系人（CallType）
    Convention.Zone[]                                     区域及其成员信道
    DigitalTrunking.Contact.{GroupCall,PrivateCall}       集群联系人

ID 语义（对齐 zone_tools.yaml 契约“ID 优先，无 ID 填 1 起始序号”）：

- 区域：档案未提供区域 ID，使用 1 起始 ``Index`` 作为 ``zone_id``；
- 信道：``Zone.Members[].ChannelID``（如 1573040）是设备自己引用信道的
  唯一 ID，作为权威 ``channel_id``。仅当没有任何区域成员引用时，才回退
  信道列表的 1 起始 ``Index``。若真机验证发现终端只接受序号，只需调整
  ``_member_channel_id`` 的取值来源，其余链路不变。
"""
from __future__ import annotations

import re
from typing import Any

# 设备默认英文区域别名 "Zone 1" → 语音用户所说的 "区域1"。
_ZONE_ALIAS_RE = re.compile(r"^zone\s*(\d+)$", re.IGNORECASE)

# Convention.DigitalService.Contact 的 CallType：0=个呼联系人，1=组呼联系人。
# 其余取值（样例中 CallType=17 的 "Call1"）语义未知，跳过不猜测。
_CONV_CONTACT_CALL_TYPE: dict[int, str] = {0: "private", 1: "group"}


def is_device_profile(data: object) -> bool:
    """判定输入是否为终端导出的原始设备档案。"""

    return isinstance(data, dict) and isinstance(data.get("Convention"), dict)


def _clean_alias(alias: object) -> str:
    return str(alias or "").strip()


def _zone_alias(alias: object) -> str:
    """区域名转写：设备默认 "Zone N" → 中文 "区域N"，其余原样保留。

    目录别名生成只认中文数字习惯（区域一/一区/第1区域），"ZONE1" 无法
    被中文口语命中；旧手写目录正是使用 区域1/区域2 命名，转写保持 UX 连续。
    """

    value = _clean_alias(alias)
    match = _ZONE_ALIAS_RE.match(value)
    if match:
        return f"区域{match.group(1)}"
    return value


def _member_channel_id(member: dict[str, Any]) -> str:
    """区域成员的权威信道 ID：ChannelID 优先，缺失时回退成员序号。"""

    channel_id = str(member.get("ChannelID") or "").strip()
    if channel_id:
        return channel_id
    return str(member.get("Index") or "").strip()


def extract_contact_patch(profile: dict[str, Any]) -> dict[str, dict[str, str]]:
    """从设备档案提取联系人目录（group/private/standby）。

    常规数字联系人按 ``CallType`` 分类；集群联系人按列表归属分类
    （GroupCall → group，PrivateCall → private）。设备档案没有待机联系人
    概念，``standby`` 保持为空，不回退手写默认目录。
    """

    patch: dict[str, dict[str, str]] = {"group": {}, "private": {}, "standby": {}}

    def _record(kind: str, alias: object, call_id: object) -> None:
        name = _clean_alias(alias)
        identity = str(call_id or "").strip()
        if name and identity:
            patch[kind].setdefault(name, identity)

    convention = profile.get("Convention") or {}
    digital_service = convention.get("DigitalService") or {}
    for item in digital_service.get("Contact") or []:
        if not isinstance(item, dict):
            continue
        kind = _CONV_CONTACT_CALL_TYPE.get(item.get("CallType"))
        if kind is None:
            continue
        _record(kind, item.get("CallAlias"), item.get("CallID"))

    trunking = profile.get("DigitalTrunking") or {}
    trunking_contacts = trunking.get("Contact") or {}
    for item in trunking_contacts.get("GroupCall") or []:
        if isinstance(item, dict):
            _record("group", item.get("Alias"), item.get("CallID"))
    for item in trunking_contacts.get("PrivateCall") or []:
        if isinstance(item, dict):
            _record("private", item.get("Alias"), item.get("CallID"))
    return patch


def extract_zone_channel_patch(profile: dict[str, Any]) -> dict[str, dict[str, str]]:
    """从设备档案提取区域/信道目录（zones/channels）。

    区域 ID 使用 1 起始 Index（档案无区域 ID 字段）；信道 ID 使用区域成员
    的 ChannelID。未被任何区域引用的信道（如模拟信道 CH A1）无法通过
    zone_channel_active_set 激活，不进入目录。仅当档案没有任何区域成员时，
    才按契约回退信道列表 Index。
    """

    patch: dict[str, dict[str, str]] = {"zones": {}, "channels": {}}
    convention = profile.get("Convention") or {}
    zones = convention.get("Zone") or []

    for position, zone in enumerate(zones, start=1):
        if not isinstance(zone, dict):
            continue
        zone_name = _zone_alias(zone.get("Alias"))
        # 档案无区域 ID：契约规定无 ID 时使用 1 起始序号。
        zone_id = str(zone.get("Index") or "").strip() or str(position)
        if zone_name:
            patch["zones"].setdefault(zone_name, zone_id)
        for member in zone.get("Members") or []:
            if not isinstance(member, dict):
                continue
            channel_name = _clean_alias(member.get("Alias"))
            channel_id = _member_channel_id(member)
            if channel_name and channel_id:
                patch["channels"].setdefault(channel_name, channel_id)

    if not patch["channels"]:
        channel_container = convention.get("Channel") or {}
        for field in ("DigitalChannels", "AnalogChannels"):
            for item in channel_container.get(field) or []:
                if not isinstance(item, dict):
                    continue
                channel_name = _clean_alias(item.get("Alias"))
                channel_id = str(item.get("Index") or "").strip()
                if channel_name and channel_id:
                    patch["channels"].setdefault(channel_name, channel_id)
    return patch
