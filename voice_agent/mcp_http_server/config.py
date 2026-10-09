# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/config.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：voice_agent.env
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""MCP 工具服务运行时配置。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from voice_agent.env import env_bool, env_float, env_int, load_dotenv

load_dotenv()

# ============================================================
# 路径
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[0]
# 原子能力唯一默认入口：voice_agent/mcp_http_server/tools。
# 不再读取历史 TOOL_DEFS_DIR，避免 .env 中的旧 tool_defs 路径把运行时切回旧契约。
# ToolCatalog(path) 仍保留显式路径注入能力，供隔离测试或迁移诊断使用。
TOOL_DEFS_DIR = (PROJECT_ROOT / "tools").resolve()


def _load_yaml_tool_names() -> tuple[str, ...]:
    """从 YAML 动态生成工具白名单，避免代码内硬编码工具名。"""
    names: list[str] = []
    seen: set[str] = set()
    if not TOOL_DEFS_DIR.exists():
        return ()
    for path in sorted(TOOL_DEFS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in data.get("tools") or []:
            name = str((item or {}).get("name", "") or "").strip()
            if not name:
                continue
            if name in seen:
                raise ValueError(f"Duplicate tool name {name!r} in YAML definitions")
            seen.add(name)
            names.append(name)
    return tuple(names)


ALL_TOOL_NAMES = _load_yaml_tool_names()
EXECUTABLE_TOOL_NAMES = frozenset(ALL_TOOL_NAMES)

# ============================================================
# HTTP / MCP 配置
# ============================================================

HOST = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
PORT = env_int("MCP_HTTP_PORT", 18083)
HTTP_REQUEST_QUEUE_SIZE = env_int("HTTP_REQUEST_QUEUE_SIZE", 512)
MCP_PROTOCOL_VERSION = os.environ.get("MCP_PROTOCOL_VERSION", "2025-11-25")
MCP_SUPPORTED_PROTOCOL_VERSIONS = tuple(
    x.strip()
    for x in os.environ.get(
        "MCP_SUPPORTED_PROTOCOL_VERSIONS",
        "2025-11-25,2025-06-18,2025-03-26",
    ).split(",")
    if x.strip()
)
MCP_ALLOWED_ORIGINS = tuple(
    x.strip() for x in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if x.strip()
)


# ============================================================
# 设备执行器配置
# ============================================================

DEVICE_EXECUTOR_MODE = os.getenv("DEVICE_EXECUTOR_MODE", "gateway_terminal")
DEVICE_HTTP_BASE_URL = os.getenv("DEVICE_HTTP_BASE_URL", "").rstrip("/")
DEVICE_HTTP_TIMEOUT = env_float("DEVICE_HTTP_TIMEOUT", 10.0)
