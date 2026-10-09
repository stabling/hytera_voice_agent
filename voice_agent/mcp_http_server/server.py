# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/server.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：shared_contracts.trace_context, shared_runtime.observability, ., .execution, .execution_context, .protocol_adapter, .tool_service
# 主要公开符号：MCPHTTPServer, MCPHandler, run_server
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from shared_contracts.trace_context import extract_trace_context
from shared_runtime.observability import traced_http_handler
from shared_runtime.latency import log_latency_tree

from . import config
from .execution import execute_tool_with_events, result_is_error
from .execution_context import use_execution_context
from .protocol_adapter import jsonrpc_error, mcp_tool_result
from .tool_service import RadioToolService

logger = logging.getLogger(__name__)
_service = RadioToolService()


class MCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = config.HTTP_REQUEST_QUEUE_SIZE


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send_json(self, status: int, data: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON-RPC payload must be an object")
        return value

    @staticmethod
    def _origin_allowed(origin: str | None) -> bool:
        if not origin:
            return True
        if "*" in config.MCP_ALLOWED_ORIGINS or origin in config.MCP_ALLOWED_ORIGINS:
            return True
        try:
            host = (urlparse(origin).hostname or "").lower()
        except Exception:
            return False
        return host in {"localhost", "127.0.0.1", "::1"}

    def _protocol_error(self, method: str) -> str | None:
        if method == "initialize":
            return None
        value = self.headers.get("MCP-Protocol-Version")
        if value is None:
            return "missing"
        return None if value in config.MCP_SUPPORTED_PROTOCOL_VERSIONS else value

    @traced_http_handler("mcp-server.http")
    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in {"/", "/health"}:
            self._send_json(200, {
                "ok": True,
                "service": "mcp-tool-server",
                "tools_count": len(_service.catalog.names()),
                "executor_mode": config.DEVICE_EXECUTOR_MODE,
            })
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    @traced_http_handler("mcp-server.http")
    def do_POST(self) -> None:
        if (urlparse(self.path).path.rstrip("/") or "/") != "/mcp":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        self._handle_mcp()

    def _handle_mcp(self) -> None:
        if not self._origin_allowed(self.headers.get("Origin")):
            self._send_json(403, jsonrpc_error(None, -32000, "Origin is not allowed"))
            return
        try:
            data = self._read_json()
        except Exception as exc:
            self._send_json(400, jsonrpc_error(None, -32700, "Parse error", str(exc)))
            return
        request_id = data.get("id")
        if data.get("jsonrpc") != "2.0" or not isinstance(data.get("method"), str):
            self._send_json(400, jsonrpc_error(request_id, -32600, "Invalid Request"))
            return

        method = data["method"]
        invalid_protocol = self._protocol_error(method)
        if invalid_protocol is not None:
            self._send_json(400, jsonrpc_error(
                request_id,
                -32600,
                f"Unsupported MCP-Protocol-Version: {invalid_protocol}",
                {"supported": list(config.MCP_SUPPORTED_PROTOCOL_VERSIONS)},
            ))
            return

        if request_id is None:
            self._send_empty(202)
            return

        if method == "initialize":
            params = data.get("params") if isinstance(data.get("params"), dict) else {}
            requested = str(params.get("protocolVersion") or "")
            negotiated = requested if requested in config.MCP_SUPPORTED_PROTOCOL_VERSIONS else config.MCP_PROTOCOL_VERSION
            self._send_json(200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hytera-tool-server", "version": ""},
                    "instructions": "Use tools/list metadata and schemas as the executable capability contract.",
                },
            }, {"MCP-Protocol-Version": negotiated})
            return

        if method == "ping":
            self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": {}})
            return

        if method == "tools/list":
            self._send_json(200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": _service.mcp_tools()},
            })
            return

        if method == "tools/call":
            self._handle_tool_call(data)
            return

        self._send_json(200, jsonrpc_error(request_id, -32601, f"Method not found: {method}"))

    def _handle_tool_call(self, data: dict) -> None:
        request_id = data.get("id")
        params = data.get("params")
        if not isinstance(params, dict):
            self._send_json(200, jsonrpc_error(request_id, -32602, "Invalid params"))
            return
        name = str(params.get("name") or "").strip()
        arguments = params.get("arguments", {})
        if not name or not isinstance(arguments, dict):
            self._send_json(200, jsonrpc_error(request_id, -32602, "tools/call requires name and object arguments"))
            return
        if not _service.catalog.contains(name):
            self._send_json(200, jsonrpc_error(
                request_id,
                -32602,
                f"Unknown tool: {name}",
                {"available_tools": _service.catalog.names()},
            ))
            return

        server_started = time.perf_counter()
        try:
            trace = extract_trace_context(self.headers, data)
            with use_execution_context({
                "actor_terminal_id": self.headers.get("X-Hytera-Actor-Terminal-Id"),
                "channel_id": self.headers.get("X-Hytera-Channel-Id"),
                "request_id": request_id,
            }):
                execution_started = time.perf_counter()
                result = execute_tool_with_events(
                    _service,
                    name,
                    arguments,
                    trace_context=trace,
                    request_id=request_id,
                )
                tool_execution_ms = round((time.perf_counter() - execution_started) * 1000.0, 3)
            if not isinstance(result, dict):
                result = {"result": result}
            result = dict(result)
            timing_ms = dict(result.get("timing_ms") or {})
            timing_ms["mcp_server_tool_execution_ms"] = tool_execution_ms
            timing_ms["mcp_server_processing_ms"] = round((time.perf_counter() - server_started) * 1000.0, 3)
            result["timing_ms"] = timing_ms
            log_latency_tree(logger, "MCP_SERVER_TIME_TREE", timing_ms, request_id=request_id, tool_name=name)
            self._send_json(200, mcp_tool_result(request_id, result, is_error=result_is_error(result)))
        except (ValueError, PermissionError) as exc:
            self._send_json(200, mcp_tool_result(
                request_id,
                {"ok": False, "code": -32602, "error": str(exc)},
                is_error=True,
            ))
        except Exception as exc:
            logger.exception("MCP tool execution failed")
            self._send_json(200, mcp_tool_result(
                request_id,
                {"ok": False, "code": -32603, "error": str(exc)},
                is_error=True,
            ))


def run_server() -> None:
    server = MCPHTTPServer((config.HOST, config.PORT), MCPHandler)
    logger.info("MCP server listening on http://%s:%s/mcp", config.HOST, config.PORT)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
