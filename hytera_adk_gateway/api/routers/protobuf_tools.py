# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/routers/protobuf_tools.py
# 所属层次：x86 网关 HTTP 路由层
# 主要职责：定义 FastAPI 对外 HTTP 契约并把请求交给服务层。现有路径、方法、字段和状态语义由兼容契约测试保护。
# 项目内直接依赖：shared_runtime.diagnostics, shared_runtime.logging_config, ...services
# 主要公开符号：ProtobufToolsSettings, create_protobuf_tools_router
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""原始二进制请求到 ADK 文本短信的兼容路由。"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, status

from shared_runtime.diagnostics import bytes_snapshot, log_trace
from shared_runtime.latency import log_latency_tree
from shared_runtime.logging_config import get_logger

from ...services import GatewayService

logger = get_logger(__name__)

_ALLOWED_CONTENT_TYPES = {
    "application/x-protobuf",
    "application/octet-stream",
}
_TARGET_HEADER = "x-hytera-target-id"
_CHANNEL_HEADER = "x-hytera-channel-id"
_ENCODE_MODE_HEADER = "x-hytera-encode-mode"


@dataclass(frozen=True)
class ProtobufToolsSettings:
    """该兼容接口自身的大小、目标与超时边界。"""

    max_request_bytes: int
    private_target_id: int
    send_timeout_seconds: float


def _result_code(adk_result: dict[str, Any]) -> int:
    if bool(adk_result.get("accepted")):
        return 0
    error_code = adk_result.get("error_code")
    try:
        return int(error_code if error_code is not None else -1)
    except (TypeError, ValueError):
        return -1


def _parse_header_int(
    value: str | None,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text or not text.isascii() or not text.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be an integer",
        )
    parsed = int(text, 10)
    if not minimum <= parsed <= maximum:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be {minimum}..{maximum}",
        )
    return parsed


def _is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    host = request.client.host.strip()
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_protobuf_tools_router(
    get_gateway_service: Callable[[], GatewayService],
    settings: ProtobufToolsSettings,
) -> APIRouter:
    """创建二进制工具请求路由，并以 ADK 接受提交作为当前成功边界。"""
    router = APIRouter(prefix="/adk/protobuf", tags=["adk-protobuf"])

    @router.post(
        "/result-sms",
        summary="将原始请求字节通过底层 ADK 私信发送到兼容或动态终端",
    )
    async def send_result_sms(request: Request) -> dict[str, Any]:
        gateway_started = time.perf_counter()
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=("Content-Type must be application/x-protobuf or application/octet-stream"),
            )

        read_started = time.perf_counter()
        wire_bytes = await request.body()
        gateway_request_read_ms = round((time.perf_counter() - read_started) * 1000.0, 3)
        if not wire_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="request body is empty",
            )
        if len(wire_bytes) > settings.max_request_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="request body is too large",
            )

        target_header = request.headers.get(_TARGET_HEADER)
        channel_header = request.headers.get(_CHANNEL_HEADER)
        if (target_header is not None or channel_header is not None) and not _is_loopback_request(
            request
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="dynamic terminal routing is only allowed from loopback clients",
            )
        dynamic_target_id = _parse_header_int(
            target_header,
            field_name="X-Hytera-Target-Id",
            minimum=1,
            maximum=16_777_215,
        )
        channel_id = _parse_header_int(
            channel_header,
            field_name="X-Hytera-Channel-Id",
            minimum=1,
            maximum=0xFFFFFFFF,
        )
        actual_target_id = (
            dynamic_target_id if dynamic_target_id is not None else settings.private_target_id
        )

        # 通知型短信不做 hex 编码，控制型 protobuf 载荷使用十六进制文本交给 ADK。
        encode_mode = (request.headers.get(_ENCODE_MODE_HEADER) or "").strip().lower()
        hex_encode = encode_mode != "text"

        log_trace(
            logger,
            "Protobuf网关请求接收",
            terminal_id=actual_target_id,
            dynamic_target_id=dynamic_target_id,
            channel_id=channel_id,
            adk_service="TMP",
            adk_method="SendCommand",
            hex_encode=hex_encode,
            protobuf_payload=bytes_snapshot(wire_bytes),
        )

        dispatch_started = time.perf_counter()
        adk_result = await asyncio.wait_for(
            asyncio.to_thread(
                get_gateway_service().send_private_sms,
                actual_target_id,
                wire_bytes,
                channel_id=channel_id,
                hex_encode=hex_encode,
            ),
            timeout=settings.send_timeout_seconds,
        )
        gateway_dispatch_ms = round((time.perf_counter() - dispatch_started) * 1000.0, 3)
        if not isinstance(adk_result, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="ADK send_private_sms returned a non-object result",
            )

        accepted = bool(adk_result.get("accepted"))
        device_confirmed = bool(adk_result.get("device_confirmed") is True)
        log_trace(
            logger,
            "Protobuf网关ADK返回",
            terminal_id=actual_target_id,
            channel_id=adk_result.get("channel_id", channel_id),
            adk_service="TMP",
            adk_method="SendCommand",
            accepted=accepted,
            adk_result=adk_result,
        )
        backend_timing = dict(adk_result.get("timing_ms") or {})
        timing_ms = {
            "gateway_processing_ms": round((time.perf_counter() - gateway_started) * 1000.0, 3),
            "gateway_request_read_ms": gateway_request_read_ms,
            "gateway_dispatch_ms": gateway_dispatch_ms,
            "gateway_service_ms": backend_timing.get("gateway_service_ms"),
            "adk_send_command_ms": backend_timing.get("adk_send_command_ms"),
        }
        log_latency_tree(
            logger,
            "GATEWAY_DELIVERY_TIME_TREE",
            timing_ms,
            terminal_id=actual_target_id,
            channel_id=adk_result.get("channel_id", channel_id),
            accepted=accepted,
        )
        return {
            "ok": accepted,
            "code": _result_code(adk_result),
            "target_type": "private",
            "target_id": str(actual_target_id),
            "request_bytes": len(wire_bytes),
            "protobuf_request_id": None,
            "protobuf_tool_id": None,
            "accepted": accepted,
            "dispatched": accepted,
            "device_confirmed": device_confirmed,
            "confirmation_status": "confirmed" if device_confirmed else "not_returned",
            "delivery_status": "binary_dispatched" if accepted else "failed",
            "success_basis": "protobuf_binary_dispatched" if accepted else None,
            "adk_result": adk_result,
            "timing_ms": timing_ms,
        }

    return router
