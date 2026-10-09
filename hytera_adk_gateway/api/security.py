# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/security.py
# 所属层次：x86 网关 API 装配层
# 主要职责：负责 FastAPI 应用工厂、依赖注入、安全中间件、请求/响应模型和生命周期装配。
# 项目内直接依赖：shared_contracts.routes, shared_contracts.security
# 主要公开符号：local_token_middleware
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Optional localhost shared-token middleware."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from shared_contracts.routes import LOCAL_API_TOKEN_HEADER
from shared_contracts.security import configured_token, token_is_valid


async def local_token_middleware(request: Request, call_next):
    """Require the configured shared token for non-health local API calls."""

    if configured_token() and request.url.path not in {"/", "/health"}:
        if not token_is_valid(request.headers.get(LOCAL_API_TOKEN_HEADER)):
            return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    return await call_next(request)
