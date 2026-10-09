# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/app.py
# 所属层次：x86 网关 API 装配层
# 主要职责：负责 FastAPI 应用工厂、依赖注入、安全中间件、请求/响应模型和生命周期装配。
# 项目内直接依赖：.dependencies, .routers.protobuf_tools, .routers.sms, .routers.system, .routers.voice, .security, .runtime
# 主要公开符号：create_app
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""FastAPI 应用工厂与路由装配。"""

from __future__ import annotations

import os

from fastapi import FastAPI

from .dependencies import get_service
from .routers.protobuf_tools import ProtobufToolsSettings, create_protobuf_tools_router
from .routers.sms import create_sms_router
from .routers.system import create_system_router
from .routers.voice import create_voice_router
from .security import local_token_middleware
from .runtime import (
    GatewayApplicationRuntime,
    callback_settings_from_config,
    start_auto_voice_capture,
)


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app(config_path: str | None = None, *, debug_fields: bool | None = None) -> FastAPI:
    """创建可测试的单进程网关应用，不在导入阶段启动任何外部资源。"""
    resolved_config_path = config_path or os.environ.get(
        "HYTERA_GATEWAY_CONFIG", "config/gateway.ini"
    )
    resolved_debug_fields = (
        bool(debug_fields) if debug_fields is not None else _environment_flag("HYTERA_DEBUG_FIELDS")
    )
    runtime = GatewayApplicationRuntime(
        resolved_config_path,
        debug_fields=resolved_debug_fields,
    )

    app = FastAPI(
        title="Hytera ADK HTTP 网关",
        version="1.0.0",
        description="Hytera H Series ADK 的短信、语音抓包与智能语音 HTTP 网关。",
        lifespan=runtime.lifespan,
    )
    app.state.gateway_runtime = runtime
    app.middleware("http")(local_token_middleware)

    def get_gateway_service():
        """返回当前应用生命周期内唯一的网关服务实例。"""
        return get_service(app)

    app.include_router(create_system_router(get_gateway_service, runtime.callback_manager))
    app.include_router(create_sms_router(get_gateway_service))
    app.include_router(
        create_protobuf_tools_router(
            get_gateway_service,
            ProtobufToolsSettings(
                max_request_bytes=runtime.initial_config.protobuf_tools_max_request_bytes,
                private_target_id=runtime.initial_config.protobuf_tools_private_target_id,
                send_timeout_seconds=runtime.initial_config.protobuf_tools_send_timeout_seconds,
            ),
        )
    )
    app.include_router(
        create_voice_router(
            lambda: get_service(app).backend,
            capture_root=runtime.initial_config.voice_capture_root,
            capture_manager=runtime.capture_manager,
            callback_manager=runtime.callback_manager,
            default_stop_timeout=runtime.initial_config.thread_join_timeout_seconds,
        )
    )
    return app


__all__ = [
    "GatewayApplicationRuntime",
    "callback_settings_from_config",
    "create_app",
    "start_auto_voice_capture",
]
