# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/routers/sms.py
# 所属层次：x86 网关 HTTP 路由层
# 主要职责：定义 FastAPI 对外 HTTP 契约并把请求交给服务层。现有路径、方法、字段和状态语义由兼容契约测试保护。
# 项目内直接依赖：...services, ..schemas
# 主要公开符号：create_sms_router
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import time
from typing import Callable

from fastapi import APIRouter, HTTPException

from ...services import GatewayService
from ..schemas import ApiResponse, GroupSmsRequest, PrivateSmsRequest


def create_sms_router(get_gateway_service: Callable[[], GatewayService]) -> APIRouter:
    """创建短信路由；服务获取函数由应用装配层注入。"""
    router = APIRouter(prefix="/sms", tags=["sms"])

    @router.post("/private", response_model=ApiResponse, summary="发送私信短信")
    def sms_private(payload: PrivateSmsRequest) -> ApiResponse:
        route_started = time.perf_counter()
        try:
            result = get_gateway_service().send_private_sms(
                payload.target_id,
                payload.text,
                require_ack=payload.require_ack,
                channel_id=payload.channel_id,
                hex_encode=payload.hex_encode,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        timing_ms = dict(result.get("timing_ms") or {})
        timing_ms["gateway_processing_ms"] = round((time.perf_counter() - route_started) * 1000.0, 3)
        result["timing_ms"] = timing_ms
        return ApiResponse(
            ok=bool(result.get("accepted")),
            data=result,
            error=(
                None if result.get("accepted") else result.get("error_name") or result.get("note")
            ),
        )

    @router.post("/group", response_model=ApiResponse, summary="发送组呼短信")
    def sms_group(payload: GroupSmsRequest) -> ApiResponse:
        try:
            result = get_gateway_service().send_group_sms(
                payload.group_id,
                payload.text,
                channel_id=payload.channel_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ApiResponse(
            ok=bool(result.get("accepted")),
            data=result,
            error=(
                None if result.get("accepted") else result.get("error_name") or result.get("note")
            ),
        )

    return router
