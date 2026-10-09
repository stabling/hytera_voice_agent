# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/voice_schemas.py
# 所属层次：x86 网关 API 装配层
# 主要职责：负责 FastAPI 应用工厂、依赖注入、安全中间件、请求/响应模型和生命周期装配。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：clean_optional_string, validate_ip_filter, VoiceCaptureStartRequest, VoiceCaptureStopRequest, VoiceInfoRequest
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""语音 HTTP API 的请求模型与输入清洗规则。"""

from __future__ import annotations

import ipaddress
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# Swagger UI 可能生成这些占位字符串，不能把它们传入 TShark 或 URL 层。
_PLACEHOLDER_STRINGS = {"", "string", "null", "none", "undefined"}


def clean_optional_string(value: Any) -> str | None:
    """将空值和 Swagger 占位字符串归一化为 ``None``。"""

    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _PLACEHOLDER_STRINGS else text


def validate_ip_filter(value: Any, field_name: str) -> str | None:
    """校验抓包过滤条件中的 IPv4/IPv6 地址。"""

    text = clean_optional_string(value)
    if text is None:
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是有效的 IPv4/IPv6 地址，当前值: {text!r}") from exc
    return text


class VoiceCaptureStartRequest(BaseModel):
    """启动 TShark 抓包的请求体。"""

    iface: str = Field(
        ...,
        description="TShark 抓包网卡名称或编号（使用 tshark -D 查看）",
        json_schema_extra={"example": "5"},
    )
    ports: list[int] = Field(
        default_factory=lambda: [30012, 30014],
        description="需要抓取的 UDP/RTP 端口",
        json_schema_extra={"example": [30012, 30014]},
    )
    src_ip: Optional[str] = Field(None, description="可选源 IP（通常为中继台 IP）")
    dst_ip: Optional[str] = Field(None, description="可选目的 IP（通常为网关电脑 IP）")
    host: Optional[str] = Field(None, description="可选、不区分方向的主机 IP")
    strict_direction: bool = Field(False, description="是否严格匹配源和目的方向")
    out_dir: Optional[str] = Field(None, description="可选输出目录")
    tshark_path: Optional[str] = Field(None, description="可选 tshark.exe 完整路径")

    @field_validator("iface")
    @classmethod
    def _iface_must_be_real(cls, value: str) -> str:
        text = str(value or "").strip()
        if text.lower() in _PLACEHOLDER_STRINGS:
            raise ValueError("iface 是必填参数，请使用 tshark -D 获取真实网卡编号或名称")
        return text

    @field_validator("ports", mode="before")
    @classmethod
    def _ports_default_and_validate(cls, value: Any) -> list[int]:
        if value in (None, "", []):
            return [30012, 30014]
        ports = [int(port) for port in value]
        invalid = [port for port in ports if not 1 <= port <= 65535]
        if invalid:
            raise ValueError(f"端口必须在 1..65535 范围内，无效值: {invalid}")
        return ports

    @field_validator("src_ip", mode="before")
    @classmethod
    def _src_ip(cls, value: Any) -> str | None:
        return validate_ip_filter(value, "src_ip")

    @field_validator("dst_ip", mode="before")
    @classmethod
    def _dst_ip(cls, value: Any) -> str | None:
        return validate_ip_filter(value, "dst_ip")

    @field_validator("host", mode="before")
    @classmethod
    def _host(cls, value: Any) -> str | None:
        return validate_ip_filter(value, "host")

    @field_validator("out_dir", "tshark_path", mode="before")
    @classmethod
    def _optional_paths(cls, value: Any) -> str | None:
        return clean_optional_string(value)


class VoiceCaptureStopRequest(BaseModel):
    """停止 TShark 抓包的请求体。"""

    timeout: float = Field(3.0, ge=0.1, le=30.0, description="等待抓包进程退出的秒数")


class VoiceInfoRequest(BaseModel):
    """查询抓包信息，并可动态更新回调基础参数。"""

    limit: int = Field(20, ge=1, le=500, description="返回最近多少包的摘要")
    refresh_wav: bool = Field(True, description="是否重新生成 WAV")
    callback_url: Optional[str] = Field(None, description="可选语音包回调地址")
    callback_enabled: Optional[bool] = Field(None, description="是否启用回调")
    callback_interval_seconds: Optional[float] = Field(None, ge=0.2, le=3600)
    callback_timeout_seconds: Optional[float] = Field(None, ge=0.2, le=120)
    callback_max_packets_per_tick: Optional[int] = Field(None, ge=1, le=10000)

    @field_validator("callback_url", mode="before")
    @classmethod
    def _callback_url(cls, value: Any) -> str | None:
        text = clean_optional_string(value)
        if text is None:
            return None
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("callback_url 必须是有效的 http(s) 地址")
        return text
