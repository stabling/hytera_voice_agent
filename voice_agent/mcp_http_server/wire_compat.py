# -*- coding: utf-8 -*-
"""MCP 逻辑契约到附件 wire-tool 契约的最小兼容映射。

仅处理协议升级造成的名称/枚举/基础类型差异；Planner、Runtime 和业务执行语义不在此修改。
"""
from __future__ import annotations

from typing import Any


TOOL_NAME_ALIASES: dict[str, str] = {
    # 旧智能体/MCP 计划保持 app_todo_add；附件工具表已改名为 utility_todo_add。
    "app_todo_add": "utility_todo_add",
}


def canonical_tool_name(tool_name: str) -> str:
    return TOOL_NAME_ALIASES.get(str(tool_name), str(tool_name))


def canonical_wire_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """把旧逻辑 MCP 参数转换为附件 tools/tool_registry 能编码的参数。

    返回新 dict，不修改调用方对象，以保证原 MCP 请求/日志内容保持不变。
    """

    name = canonical_tool_name(tool_name)
    result = dict(arguments or {})

    # 旧协议使用 individual；附件 tools 统一使用 private。
    if name.startswith("sms_") and result.get("target_type") == "individual":
        result["target_type"] = "private"
    if name in {"contact_add", "contact_delete"} and result.get("type") == "individual":
        result["type"] = "private"

    # 旧版 call_start 参数名 call_type/callee_id -> 新版 target_type/target_id。
    # 新名缺失时重命名；新旧并存（如模型按能力 schema 输出新名、服务端又追加了
    # 旧名投影）时以新名为准并剥离旧名，避免残留字段触发
    # additionalProperties 校验失败。
    if "call_type" in result:
        if "target_type" not in result:
            result["target_type"] = result["call_type"]
        del result["call_type"]
    if "callee_id" in result:
        if "target_id" not in result:
            result["target_id"] = result["callee_id"]
        del result["callee_id"]
    if result.get("target_type") == "individual":
        result["target_type"] = "private"

    # 附件 zone 工具把 ID 定义为 integer。旧 MCP/Planner 为兼容一直使用字符串 ID。
    if name.startswith("zone_"):
        for field in ("zone_id", "channel_id"):
            value = result.get(field)
            if isinstance(value, str) and value.strip().isdigit():
                result[field] = int(value.strip())

    return result


def canonical_wire_invocation(
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    return canonical_tool_name(tool_name), canonical_wire_arguments(tool_name, arguments)
