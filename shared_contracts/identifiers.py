# =============================================================================
# 【维护注释】文件：shared_contracts/identifiers.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：new_event_id, new_trace_id, new_correlation_id, new_causation_id, new_command_id, new_operation_id, new_call_id, new_span_id, normalize_identifier, normalize_trace_id
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Stable identifier helpers shared by gateway, agent and MCP processes."""

from __future__ import annotations

import re
import uuid
from typing import Final

MAX_IDENTIFIER_LENGTH: Final = 128
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _new_prefixed(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_event_id() -> str:
    return _new_prefixed("evt")


def new_trace_id() -> str:
    """Return a W3C Trace Context compatible, non-zero 128-bit trace id."""

    value = uuid.uuid4().hex
    return value if value != "0" * 32 else uuid.uuid4().hex


def new_correlation_id() -> str:
    return _new_prefixed("cor")


def new_causation_id() -> str:
    return _new_prefixed("cause")


def new_command_id() -> str:
    return _new_prefixed("cmd")


def new_operation_id() -> str:
    return _new_prefixed("op")


def new_call_id() -> str:
    return _new_prefixed("call")


def new_span_id() -> str:
    value = uuid.uuid4().hex[:16]
    return value if value != "0" * 16 else uuid.uuid4().hex[:16]


def normalize_identifier(value: object, *, max_length: int = MAX_IDENTIFIER_LENGTH) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[: max(1, int(max_length))]


def normalize_trace_id(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if _TRACE_ID_RE.fullmatch(text) and text != "0" * 32:
        return text
    return None


def normalize_span_id(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if _SPAN_ID_RE.fullmatch(text) and text != "0" * 16:
        return text
    return None


def is_valid_trace_id(value: object) -> bool:
    return normalize_trace_id(value) is not None
