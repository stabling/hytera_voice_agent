# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：shared_runtime/diagnostics.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：compact_json, bytes_snapshot, log_trace
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""跨进程调用链诊断日志工具。

该模块只负责把关键运行时对象转换为稳定、可检索的结构化日志字段，
不承担业务决策、路由选择或设备执行职责，从而保持诊断逻辑与业务逻辑解耦。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any


def compact_json(value: Any) -> str:
    """将对象序列化为紧凑 UTF-8 JSON 字符串。

    参数：
        value: 任意可序列化对象；无法直接序列化的对象退化为字符串。

    返回：
        适合写入单行日志的 JSON 字符串。
    """

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def bytes_snapshot(data: bytes | bytearray | memoryview | str | None) -> dict[str, Any]:
    """生成二进制或文本载荷的完整诊断快照。

    参数：
        data: 待记录的数据，可为 bytes、bytearray、memoryview、字符串或 None。

    返回：
        包含长度、十六进制、Base64 与可解码文本的字典。二进制无法按 UTF-8
        解码时，``utf8`` 字段为 None。
    """

    if data is None:
        raw = b""
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = bytes(data)
    try:
        utf8 = raw.decode("utf-8")
    except UnicodeDecodeError:
        utf8 = None
    return {
        "bytes": len(raw),
        "hex": raw.hex(),
        "base64": base64.b64encode(raw).decode("ascii"),
        "utf8": utf8,
    }


def log_trace(logger: logging.Logger, event: str, **fields: Any) -> None:
    """写入一条结构化调用链日志。

    参数：
        logger: Python Logger 实例。
        event: 中文事件名称。
        **fields: 需要附带的上下文字段；值为 None 的字段会被省略。
    """

    cleaned = {key: value for key, value in fields.items() if value is not None}
    if cleaned:
        logger.info("%s | %s", event, compact_json(cleaned))
    else:
        logger.info("%s", event)
