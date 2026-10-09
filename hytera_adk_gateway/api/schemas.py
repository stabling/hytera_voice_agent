# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/schemas.py
# 所属层次：x86 网关 API 装配层
# 主要职责：负责 FastAPI 应用工厂、依赖注入、安全中间件、请求/响应模型和生命周期装配。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：PrivateSmsRequest, GroupSmsRequest, ApiResponse
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class PrivateSmsRequest(BaseModel):
    """私信短信请求体"""

    target_id: int = Field(..., ge=1, le=16_777_215, description="目标终端 Radio ID")
    text: str = Field(..., min_length=1, description="短信内容")
    require_ack: bool = Field(False, description="是否要求终端 ACK 确认")
    channel_id: Optional[int] = Field(None, description="可选 ADK runtime channel_id（十进制）")
    hex_encode: bool = Field(True, description="是否将正文转 hex 后发送（通知型设为 false 直接发原文）")


class GroupSmsRequest(BaseModel):
    """组呼短信请求体"""

    group_id: int = Field(..., ge=1, le=16_777_215, description="目标组 ID")
    text: str = Field(..., min_length=1, description="短信内容")
    channel_id: Optional[int] = Field(None, description="可选 ADK runtime channel_id（十进制）")


class ApiResponse(BaseModel):
    """统一 API 响应结构"""

    ok: bool
    data: Optional[Any] = None
    error: Optional[str] = None
