# =============================================================================
# 【维护注释】文件：voice_agent/calls/api.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：.models
# 主要公开符号：query_scope, parse_call_selector
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Read-only CallStore query helpers used by the local Agent HTTP server."""

from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import parse_qs

from .models import CallSelector

CALL_QUERY_TERMINAL_HEADER = "X-Hytera-Terminal-ID"


def query_scope(headers: Mapping[str, object]) -> tuple[bool, str | None]:
    terminal_id = str(headers.get(CALL_QUERY_TERMINAL_HEADER) or "").strip() or None
    require_scope = os.environ.get("CALL_QUERY_REQUIRE_TERMINAL_SCOPE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return (not require_scope or terminal_id is not None), terminal_id


def parse_call_selector(
    raw_query: str,
    *,
    max_limit: int,
    authorized_terminal_id: str | None = None,
) -> CallSelector:
    values = parse_qs(raw_query, keep_blank_values=False)
    requested_limit = int((values.get("limit") or [100])[0])
    return CallSelector(
        call_id=(values.get("call_id") or [None])[0],
        source_id=(values.get("source_id") or [None])[0],
        target_id=(values.get("target_id") or [None])[0],
        group_id=(values.get("group_id") or [None])[0],
        channel_id=(values.get("channel_id") or [None])[0],
        status=(values.get("status") or [None])[0],
        direction=(values.get("direction") or [None])[0],
        started_after=(values.get("started_after") or [None])[0],
        started_before=(values.get("started_before") or [None])[0],
        authorized_terminal_id=authorized_terminal_id,
        limit=min(max(1, requested_limit), max(1, int(max_limit))),
        offset=max(0, int((values.get("offset") or [0])[0])),
    )
