# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/routers/system.py
# 所属层次：x86 网关 HTTP 路由层
# 主要职责：定义 FastAPI 对外 HTTP 契约并把请求交给服务层。现有路径、方法、字段和状态语义由兼容契约测试保护。
# 项目内直接依赖：shared_runtime.architecture, ...callbacks, ...services, ..schemas
# 主要公开符号：create_system_router
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter

from shared_runtime.architecture import current_architecture

from ...callbacks import VoiceCallbackManager
from ...services import GatewayService
from ..schemas import ApiResponse


def create_system_router(
    get_gateway_service: Callable[[], GatewayService],
    callback_manager: VoiceCallbackManager,
) -> APIRouter:
    """创建系统状态路由；不直接依赖 FastAPI 应用对象。"""
    router = APIRouter(tags=["system"])

    @router.get("/health", response_model=ApiResponse, summary="服务健康状态")
    def health() -> ApiResponse:
        service = get_gateway_service()
        data = service.health()
        data["voice_auto_capture_start"] = getattr(service, "voice_auto_capture_start", None)
        data["voice_callback"] = callback_manager.status()
        data["runtime"] = current_architecture().as_dict()
        data["service_role"] = "adk_gateway_x86"
        return ApiResponse(ok=True, data=data)

    @router.get("/channels", response_model=ApiResponse, summary="ADK 通道列表")
    def channels() -> ApiResponse:
        backend = get_gateway_service().backend
        return ApiResponse(
            ok=True,
            data={
                "channels": backend.channels,
                "selected_channel": backend.selected_channel,
                "channel_mode": backend.channel_mode,
                "defaults": backend.configured_channel_defaults(),
            },
        )

    return router
