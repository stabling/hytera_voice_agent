# =============================================================================
# 【维护注释】文件：voice_agent/gateway_client.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.http_client, shared_runtime.diagnostics, shared_runtime.logging_config, shared_contracts.routes
# 主要公开符号：gateway_base_url, health, send_private_sms, send_group_sms
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""64 位语音代理调用 32 位 ADK 网关的 HTTP 客户端。"""

from __future__ import annotations

import os
import time
from typing import Any

from shared_contracts.http_client import get_json, post_json
from shared_runtime.diagnostics import log_trace
from shared_runtime.logging_config import get_logger

from shared_contracts.routes import (
    DEFAULT_GATEWAY_BASE_URL,
    GATEWAY_GROUP_SMS_PATH,
    GATEWAY_HEALTH_PATH,
    GATEWAY_PRIVATE_SMS_PATH,
)

logger = get_logger(__name__)


def gateway_base_url() -> str:
    """返回 32 位 ADK 网关基础地址。

    优先读取 ``HYTERA_ADK_BASE_URL``，未配置时使用共享路由中的默认地址。
    """
    return os.environ.get("HYTERA_ADK_BASE_URL", DEFAULT_GATEWAY_BASE_URL).rstrip("/")


def health(*, timeout: float = 1.0) -> dict[str, Any]:
    """查询 ADK 网关健康状态。

    参数:
        timeout: HTTP 请求超时时间，单位为秒。

    返回:
        网关健康检查返回的 JSON 对象。
    """
    return get_json(gateway_base_url() + GATEWAY_HEALTH_PATH, timeout=timeout)


def send_private_sms(
    target_id: int,
    text: str,
    *,
    channel_id: int | None = None,
    require_ack: bool = False,
    hex_encode: bool = True,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """向指定终端发送私人短信。

    参数:
        target_id: 接收短信的终端 ID。
        text: 要发送的文本；控制链路也可传入十六进制字符串。
        channel_id: 指定下发通道；为空时由底层 ADK 路由规则解析。
        require_ack: 是否要求终端确认。
        hex_encode: 是否由网关把文本按十六进制方式转换后再交给 ADK。
        timeout: HTTP 请求超时时间，单位为秒。

    返回:
        32 位 ADK 网关返回的 JSON 对象。
    """
    payload = {
        "target_id": target_id,
        "text": text,
        "require_ack": require_ack,
        "channel_id": channel_id,
        "hex_encode": hex_encode,
    }
    url = gateway_base_url() + GATEWAY_PRIVATE_SMS_PATH
    log_trace(
        logger,
        "私人短信HTTP请求",
        terminal_id=target_id,
        channel_id=channel_id,
        require_ack=require_ack,
        hex_encode=hex_encode,
        adk_method="TMP.SendCommand",
        url=url,
        payload=payload,
    )
    http_started = time.perf_counter()
    try:
        result = post_json(url, payload, timeout=timeout)
    except Exception as exc:
        log_trace(
            logger,
            "私人短信HTTP请求异常",
            terminal_id=target_id,
            channel_id=channel_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
    agent_to_gateway_http_ms = round((time.perf_counter() - http_started) * 1000.0, 3)
    data = result.get("data") if isinstance(result, dict) else None
    gateway_timing = dict(data.get("timing_ms") or {}) if isinstance(data, dict) else {}
    if isinstance(result, dict):
        result["timing_ms"] = {
            "protobuf_encode_ms": 0.0,
            "agent_to_gateway_http_ms": agent_to_gateway_http_ms,
            "gateway_processing_ms": gateway_timing.get("gateway_processing_ms"),
            "adk_send_command_ms": gateway_timing.get("adk_send_command_ms"),
            "response_decode_ms": 0.0,
            "total_ms": agent_to_gateway_http_ms,
        }
    log_trace(
        logger,
        "私人短信HTTP响应",
        terminal_id=target_id,
        channel_id=channel_id,
        response=result,
        timing_ms=result.get("timing_ms") if isinstance(result, dict) else None,
    )
    return result


def send_group_sms(
    group_id: int,
    text: str,
    *,
    channel_id: int | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """向指定组发送组短信。

    参数:
        group_id: 目标组 ID。
        text: 短信文本。
        channel_id: 指定下发通道；为空时由底层路由规则解析。
        timeout: HTTP 请求超时时间，单位为秒。

    返回:
        32 位 ADK 网关返回的 JSON 对象。
    """
    payload = {"group_id": group_id, "text": text, "channel_id": channel_id}
    return post_json(gateway_base_url() + GATEWAY_GROUP_SMS_PATH, payload, timeout=timeout)
