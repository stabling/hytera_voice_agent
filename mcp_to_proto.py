# -*- coding: utf-8 -*-
"""兼容入口：标准 MCP ``tools/call`` -> 附件 mcp_bridge 二进制帧。

公开函数名保持不变，避免修改现有 Agent/Runtime/TerminalDelivery 调用链；真正的
二进制编码改为附件 ``mcp_bridge.encodeToolCall``（CBOR + L2 大端帧）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from mcp_wire import mcp_bridge
from mcp_wire.tool_registry import TOOL_NAME_TO_ID
from voice_agent.mcp_http_server.protocol_adapter import normalize_tool_invocation
from voice_agent.mcp_http_server.tool_catalog import ToolCatalog
from voice_agent.request_ids import normalize_request_id
from voice_agent.mcp_http_server.wire_compat import canonical_wire_invocation


class UnknownToolError(Exception):
    """工具名未出现在附件 tools/tool_registry 中。"""


class InvalidArgumentsError(Exception):
    """参数不符合附件 tools/tool_registry 定义。"""


class ProtoRuntimeError(RuntimeError):
    """保留旧公开异常名；新桥不再依赖 generated protobuf runtime。"""


def _request_id_to_int(value: Any) -> int:
    """把 JSON-RPC ID 稳定映射为附件 L2 帧所需的 u32。

    v0.2 线协议保留 seq_id=0，因此未提供 ID 时自动分配进程级递增 ID，
    而不是像旧协议那样回退到 0。
    """
    if value is None or value == "":
        return normalize_request_id(None)
    if isinstance(value, bool):
        raise InvalidArgumentsError("request id must not be boolean")
    if isinstance(value, int):
        if not 1 <= value <= 0xFFFFFFFF:
            raise InvalidArgumentsError("request id must be 1..4294967295")
        return value
    if isinstance(value, float) and value.is_integer() and 1 <= value <= 0xFFFFFFFF:
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        numeric = int(text)
        if not 1 <= numeric <= 0xFFFFFFFF:
            raise InvalidArgumentsError("request id must be 1..4294967295")
        return numeric
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") or 1


def encode_tool_call_with_context(
    tool_call: dict[str, Any],
    *,
    catalog: ToolCatalog | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """使用附件 mcp_bridge 编码，并保留旧调用方需要的诊断上下文。"""
    try:
        invocation = normalize_tool_invocation(tool_call)
    except Exception as exc:
        raise InvalidArgumentsError(str(exc)) from exc

    wire_tool_name, wire_arguments = canonical_wire_invocation(
        invocation.tool_name,
        invocation.arguments,
    )
    if wire_tool_name not in TOOL_NAME_TO_ID:
        raise UnknownToolError(
            f"Unknown wire tool: {wire_tool_name!r}; logical={invocation.tool_name!r}"
        )

    # 继续保留现有 YAML 服务端校验；只是校验对象改为附件 tools 的规范名称/参数。
    catalog = catalog or ToolCatalog()
    if not catalog.contains(wire_tool_name):
        raise UnknownToolError(
            f"Wire tool {wire_tool_name!r} is not present in canonical tools catalog"
        )
    try:
        validated_wire_arguments = catalog.validate_arguments(wire_tool_name, wire_arguments)
    except ValueError as exc:
        raise InvalidArgumentsError(str(exc)) from exc

    numeric_request_id = _request_id_to_int(invocation.request_id)
    wire_request = {
        "jsonrpc": "2.0",
        "id": numeric_request_id,
        "method": "tools/call",
        "params": {
            "name": wire_tool_name,
            "arguments": validated_wire_arguments,
        },
    }
    try:
        # v0.2 线协议：reply=1 保留旧 qos=1 语义；mtu=0 关闭分片，
        # 单次编码输出恰好一个完整帧，与旧 bytes 返回值行为一致。
        frames = mcp_bridge.encodeToolCall(
            wire_request,
            {"version": 0, "type": 0, "encoding": 0, "reply": 1, "mtu": 0},
        )
    except mcp_bridge.UnknownToolError as exc:
        raise UnknownToolError(str(exc)) from exc
    except mcp_bridge.InvalidArgumentsError as exc:
        raise InvalidArgumentsError(str(exc)) from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidArgumentsError(str(exc)) from exc
    if len(frames) != 1:
        raise InvalidArgumentsError(
            f"mcp_bridge expected one un-fragmented frame, got {len(frames)}"
        )
    wire_bytes = frames[0]

    context = {
        "request_id": invocation.request_id,
        "numeric_request_id": numeric_request_id,
        "tool_name": invocation.tool_name,
        "wire_tool_name": wire_tool_name,
        "tool_id": int(TOOL_NAME_TO_ID[wire_tool_name]),
        "arguments": dict(invocation.arguments),
        "wire_arguments": dict(validated_wire_arguments),
        "wire_format": "mcp_bridge_cbor_l2_be",
    }
    return wire_bytes, context


def encode_tool_call(
    mcp_json: dict[str, Any],
    *,
    catalog: ToolCatalog | None = None,
) -> bytes:
    wire, _ = encode_tool_call_with_context(mcp_json, catalog=catalog)
    return wire


def decode_tool_request(wire_bytes: bytes) -> dict[str, Any]:
    """使用附件 ``decodeToolRequest`` 回读单条请求，保持旧函数返回 dict。"""
    try:
        decoded = mcp_bridge.decodeToolRequest(wire_bytes)
    except Exception as exc:
        raise InvalidArgumentsError(f"cannot decode MCP bridge request: {exc}") from exc
    if not decoded:
        return {}
    if len(decoded) != 1:
        raise InvalidArgumentsError(
            f"decode_tool_request expects one entry, got {len(decoded)}"
        )
    return dict(decoded[0])


def inspect_tool_call_encoding(
    mcp_json: dict[str, Any],
    *,
    catalog: ToolCatalog | None = None,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """编码后立即通过附件解码器回读，验证 wire 语义。"""
    wire_bytes, encode_context = encode_tool_call_with_context(mcp_json, catalog=catalog)
    decoded_request = decode_tool_request(wire_bytes)
    return wire_bytes, encode_context, decoded_request


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Encode MCP tools/call using mcp_bridge")
    parser.add_argument("input", help="input MCP JSON file")
    parser.add_argument("-o", "--output", required=True, help="output binary frame file")
    parser.add_argument("--context", help="optional JSON context output file")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    wire, context = encode_tool_call_with_context(payload)
    Path(args.output).write_bytes(wire)
    if args.context:
        Path(args.context).write_text(
            json.dumps(context, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
