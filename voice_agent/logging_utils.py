# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/logging_utils.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_runtime.json_utils, shared_runtime.logging_config
# 主要公开符号：configure_console_logging, compact_json, log_event
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Voice-agent structured logging helpers."""

from __future__ import annotations

import logging
from typing import Any

from shared_runtime.json_utils import compact_json as _compact_json
from shared_runtime.logging_config import configure_project_logging


def configure_console_logging(level: str | int | None = None) -> None:
    """Backward-compatible entry point for project-wide persistent logging."""

    configure_project_logging(level)


def compact_json(value: Any) -> str:
    """Generate compact UTF-8 JSON suitable for structured log fields."""

    return _compact_json(value)


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """Record one structured business event, omitting empty fields."""

    cleaned = {key: value for key, value in fields.items() if value is not None}
    if cleaned:
        logger.info("%s | %s", message, compact_json(cleaned))
    else:
        logger.info("%s", message)
