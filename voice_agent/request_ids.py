# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/request_ids.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：next_voice_request_id, normalize_request_id, reset_request_ids_for_test
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""语音请求 ID 的进程级递增序列。

每当收到一条新的语音回调，入口层调用 :func:`next_voice_request_id`。同一条
语音在 ASR、意图识别、MCP JSON-RPC、Protobuf 与设备回传链路中复用该整数
ID，从而替代原先不可读、难关联的 UUID。
"""

from __future__ import annotations

import itertools
import threading

# Protobuf ``ToolRequest.id`` 为 int32；保持在正整数范围内。
_MAX_REQUEST_ID = 2_147_483_647
_LOCK = threading.Lock()
_COUNTER = itertools.count(1)


def next_voice_request_id() -> int:
    """返回下一个从 1 开始的线程安全整数 ID。"""

    global _COUNTER
    with _LOCK:
        value = next(_COUNTER)
        if value > _MAX_REQUEST_ID:
            _COUNTER = itertools.count(2)
            value = 1
        return value


def normalize_request_id(value: object | None) -> int:
    """校验外部传入 ID；未提供时自动分配。"""

    if value is None:
        return next_voice_request_id()
    if isinstance(value, bool):
        raise ValueError("request id must be a positive integer")
    try:
        request_id = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("request id must be a positive integer") from exc
    if not 1 <= request_id <= _MAX_REQUEST_ID:
        raise ValueError(f"request id must be between 1 and {_MAX_REQUEST_ID}")
    return request_id


def reset_request_ids_for_test(start: int = 1) -> None:
    """仅供测试使用：重置进程级序列。"""

    global _COUNTER
    if not 1 <= int(start) <= _MAX_REQUEST_ID:
        raise ValueError("start must be a positive int32 value")
    with _LOCK:
        _COUNTER = itertools.count(int(start))
