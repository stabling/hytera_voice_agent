# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/mcp_result.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.mcp_http_server.tool_catalog, .mcp_http_server.tool_service, .request_ids
# 主要公开符号：MCPResult, get_default_tool_catalog, build_mcp_result, validate_mcp_result
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""MCP ``tools/call`` JSON-RPC 请求构建与严格校验。

请求 ID 改为进程级递增正整数。新语音在回调入口分配一次 ID，并在 ASR、
意图识别、MCP、Protobuf 和设备回传链路中复用，便于控制台与日志关联。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mcp_http_server.tool_catalog import ToolCatalog
from .request_ids import normalize_request_id

MCP_JSONRPC_VERSION = "2.0"
MCP_TOOLS_CALL_METHOD = "tools/call"


@dataclass(frozen=True)
class MCPResult:
    """Normalized MCP execution outcome used by the local execution boundary."""

    ok: bool
    mcp_result: dict[str, Any] | None
    status_code: int | None
    mcp_response: dict[str, Any] | None
    error: str | None
    error_type: str | None
    timing_ms: dict[str, float]
    execution_plan: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mcp_result": self.mcp_result,
            "status_code": self.status_code,
            "mcp_response": self.mcp_response,
            "error": self.error,
            "error_type": self.error_type,
            "timing_ms": dict(self.timing_ms),
            "execution_plan": self.execution_plan,
        }


_DEFAULT_TOOL_CATALOG = ToolCatalog()


def get_default_tool_catalog() -> ToolCatalog:
    """Return the startup-loaded tool catalog; request paths perform no YAML I/O."""

    return _DEFAULT_TOOL_CATALOG


def build_mcp_result(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    catalog: ToolCatalog | None = None,
    request_id: int | str | None = None,
) -> dict[str, Any]:
    """构建一个严格、可编码为 Protobuf 的 ``tools/call`` 请求。"""

    resolved_catalog = catalog or get_default_tool_catalog()
    canonical_name = str(tool_name or "").strip()
    if not resolved_catalog.contains(canonical_name):
        raise ValueError(
            f"unsupported implemented tool {canonical_name!r}; "
            f"available={resolved_catalog.names()}"
        )
    spec = resolved_catalog.get(canonical_name)
    if spec.executor_kind == "local":
        raise ValueError(f"local capability {canonical_name!r} has no MCP wire request")
    normalized_arguments = resolved_catalog.validate_arguments(
        canonical_name, dict(arguments or {})
    )

    return {
        "jsonrpc": MCP_JSONRPC_VERSION,
        "id": normalize_request_id(request_id),
        "method": MCP_TOOLS_CALL_METHOD,
        "params": {
            "name": canonical_name,
            "arguments": normalized_arguments,
        },
    }


def validate_mcp_result(
    value: dict[str, Any],
    *,
    catalog: ToolCatalog | None = None,
) -> dict[str, Any]:
    """严格校验 ``mcp_result`` 结构并返回原对象。"""

    if not isinstance(value, dict):
        raise ValueError("mcp_result must be a JSON object")
    if set(value) != {"jsonrpc", "id", "method", "params"}:
        raise ValueError("mcp_result must contain only jsonrpc, id, method, params")
    if value.get("jsonrpc") != MCP_JSONRPC_VERSION:
        raise ValueError('mcp_result.jsonrpc must be exactly "2.0"')
    if value.get("method") != MCP_TOOLS_CALL_METHOD:
        raise ValueError('mcp_result.method must be exactly "tools/call"')

    request_id = value.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise ValueError("mcp_result.id must be a positive integer")
    if not 1 <= request_id <= 2_147_483_647:
        raise ValueError("mcp_result.id must fit positive int32")

    params = value.get("params")
    if not isinstance(params, dict) or set(params) != {"name", "arguments"}:
        raise ValueError("mcp_result.params must contain only name and arguments")
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("mcp_result.params.name must be a non-empty string")
    if not isinstance(arguments, dict):
        raise ValueError("mcp_result.params.arguments must be a JSON object")

    resolved_catalog = catalog or get_default_tool_catalog()
    if not resolved_catalog.contains(name):
        raise ValueError(f"unsupported implemented tool {name!r}")
    spec = resolved_catalog.get(name)
    if spec.executor_kind == "local":
        raise ValueError(f"local capability {name!r} has no MCP wire request")
    normalized = resolved_catalog.validate_arguments(name, arguments)
    if normalized != arguments:
        raise ValueError("mcp_result arguments are not tools-schema-normalized")
    return value
