# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/settings/parsing.py
# 所属层次：网关配置层
# 主要职责：负责 gateway.ini 与环境配置的解析、校验和只读配置模型。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：ConfigError
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""INI 基础读取、类型转换与边界校验工具。"""

from __future__ import annotations

import configparser
import ipaddress
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """网关配置缺失、格式错误或违反运行约束。"""


def _optional_text(
    parser: configparser.ConfigParser, section: str, key: str, fallback: str = ""
) -> str | None:
    if not parser.has_section(section) or not parser.has_option(section, key):
        value = fallback
    else:
        value = parser.get(section, key, fallback=fallback)
    text = str(value or "").strip()
    if text.lower() in {"", "none", "null", "undefined", "string"}:
        return None
    return text


def _optional_ipv4(parser: configparser.ConfigParser, section: str, key: str) -> str | None:
    value = _optional_text(parser, section, key)
    if value is None:
        return None
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ConfigError(f"{section}.{key} must be IPv4, got {value!r}") from exc
    return value


def _optional_url(parser: configparser.ConfigParser, section: str, key: str) -> str | None:
    value = _optional_text(parser, section, key)
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{section}.{key} must be an http(s) callback URL, got {value!r}")
    return value


def _port_list(raw_value: str | None, fallback: list[int]) -> list[int]:
    text = str(raw_value or "").strip()
    if not text:
        return list(fallback)
    try:
        values = [int(item.strip()) for item in text.replace(";", ",").split(",") if item.strip()]
    except ValueError as exc:
        raise ConfigError("voice_capture.ports must be a comma-separated port list") from exc
    if not values:
        return list(fallback)
    if any(not 1 <= port <= 65535 for port in values):
        raise ConfigError("voice_capture.ports contains an invalid UDP port")
    return values


def _section_getboolean(
    parser: configparser.ConfigParser, section: str, key: str, fallback: bool
) -> bool:
    if not parser.has_section(section) or not parser.has_option(section, key):
        return fallback
    return parser.getboolean(section, key, fallback=fallback)


def _section_getfloat(
    parser: configparser.ConfigParser, section: str, key: str, fallback: float
) -> float:
    if not parser.has_section(section) or not parser.has_option(section, key):
        return fallback
    return parser.getfloat(section, key, fallback=fallback)


def _section_getint(
    parser: configparser.ConfigParser, section: str, key: str, fallback: int
) -> int:
    if not parser.has_section(section) or not parser.has_option(section, key):
        return fallback
    return parser.getint(section, key, fallback=fallback)


def _optional_positive_int(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
) -> int | None:
    value = _optional_text(parser, section, key)
    if value is None:
        return None
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise ConfigError(f"{section}.{key} must be a decimal or 0x-prefixed integer") from exc
    if not 1 <= parsed <= 0xFFFFFFFF:
        raise ConfigError(f"{section}.{key} must be 1..0xFFFFFFFF")
    return parsed


def _resolve_runtime(path: Path, raw_value: str) -> Path:
    raw_path = Path(raw_value.strip()).expanduser()
    return raw_path.resolve() if raw_path.is_absolute() else (path.parent / raw_path).resolve()


def _read_ipv4(
    parser: configparser.ConfigParser, section: str, key: str, *, allow_zero: bool = False
) -> str:
    value = parser.get(section, key).strip()
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ConfigError(f"{section}.{key} must be an IPv4 address, got {value!r}") from exc
    if value == "0.0.0.0" and not allow_zero:
        raise ConfigError(f"{section}.{key} must be this PC's wired IPv4, not 0.0.0.0")
    return value
