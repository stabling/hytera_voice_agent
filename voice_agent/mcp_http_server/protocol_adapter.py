# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/protocol_adapter.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：ToolInvocation, normalize_tool_invocation, jsonrpc_error, mcp_tool_result
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolInvocation:
    tool_name: str
    arguments: dict[str, Any]
    request_id: Any = None


def normalize_tool_invocation(payload: dict[str, Any]) -> ToolInvocation:
    """Normalize one standard MCP JSON-RPC tools/call request."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise ValueError("tool invocation must be a JSON-RPC 2.0 object")
    if payload.get("method") != "tools/call":
        raise ValueError("tool invocation method must be tools/call")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ValueError("tools/call params must be an object")
    name = str(params.get("name") or "").strip()
    arguments = params.get("arguments", {})
    if not name:
        raise ValueError("tools/call requires params.name")
    if not isinstance(arguments, dict):
        raise ValueError("tools/call params.arguments must be an object")
    return ToolInvocation(tool_name=name, arguments=dict(arguments), request_id=payload.get("id"))


def jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def mcp_tool_result(request_id: Any, result: Any, *, is_error: bool = False) -> dict[str, Any]:
    structured = result if isinstance(result, dict) else {"result": result}
    text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": bool(is_error),
        },
    }
