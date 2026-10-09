# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/agent_client.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：shared_contracts.http_client, shared_contracts.routes
# 主要公开符号：agent_base_url, health
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""HTTP client for the 32-bit gateway to inspect the 64-bit agent."""

from __future__ import annotations

import os
from typing import Any

from shared_contracts.http_client import get_json, post_json
from shared_contracts.routes import AGENT_HEALTH_PATH, DEFAULT_AGENT_BASE_URL


def agent_base_url() -> str:
    return os.environ.get("HYTERA_AGENT_BASE_URL", DEFAULT_AGENT_BASE_URL).rstrip("/")


def health(*, timeout: float = 1.0) -> dict[str, Any]:
    return get_json(agent_base_url() + AGENT_HEALTH_PATH, timeout=timeout)


def forward_sms_to_agent(
    payload: dict[str, Any], *, timeout: float = 2.0
) -> dict[str, Any]:
    """把入站 TMP 短信转发给智能体 ``/sms/callback`` 做分类与归档。

    需求 §2.2/§35：短信 source/target 由智能体统一按 CONTROL_IDS 分类。
    """
    return post_json(
        agent_base_url() + "/sms/callback", payload, timeout=timeout
    )


def sms_forward_enabled() -> bool:
    """入站短信转发开关（默认开启；置 HYTERA_FORWARD_SMS_TO_AGENT=0 关闭）。"""
    return os.environ.get("HYTERA_FORWARD_SMS_TO_AGENT", "1").strip() not in {
        "0",
        "false",
        "False",
        "FALSE",
    }
