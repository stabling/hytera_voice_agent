"""
L3: MCP Bridge - convert between MCP JSON-RPC and binary frames.

This module sits between L2 (frame codec + fragmenter) and L4 (business
logic). It handles:
  - Stripping/filling MCP JSON-RPC envelope (jsonrpc, method, params)
  - Mapping tool names to ToolId via tool_registry
  - Serializing/deserializing inner messages using int-key CBOR
  - Options-controlled batch aggregation (reply grouping, mtu budget)
    and MTU fragmentation via codec.fragment

Replaced with the atomic_api v0.2 wire protocol (zip
atomic_api/python/src/mcp_bridge.py). Import paths are adapted to the
mcp_wire package layout (relative imports) and cbor2 falls back to the
bundled pure-Python codec when the external wheel is unavailable.

Only business data (arguments/result) crosses the wire.
MCP framework fields (jsonrpc, method) are auto-filled on decode.
"""

import sys
import os
import json

try:
    import cbor2  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised without external cbor2
    # External cbor2 is optional. Fall back to the bundled pure-Python CBOR
    # codec (mcp_wire/_cbor2_fallback.py, one directory above this file);
    # it is wire-compatible with cbor2 for the types used here.
    sys.path.insert(
        0, os.path.dirname(os.path.abspath(__file__)))
    import _cbor2_fallback as cbor2

from .codec.frame import (
    encodePayload, decodePayload,
    frameVer, frameType, frameEnc, frameReply,
    MSG_TYPE_REQUEST, MSG_TYPE_RESPONSE, MSG_TYPE_EVENT,
    FIXED_HEADER_SIZE, ATOMIC_MTU_BYTES,
)
from .codec.fragment import (
    fragmentPayload, frameIsComplete, reassembleWithHeader,
    nextTransferId, MessageTooLargeError, FrameIncompleteError,
)
from .tool_registry import (
    TOOL_NAME_TO_ID, TOOL_ID_TO_NAME,
    TOOL_NAME_TO_REQUEST_SCHEMA, TOOL_NAME_TO_RESPONSE_SCHEMA,
    TOOL_ID_TO_REQUEST_SCHEMA, TOOL_ID_TO_RESPONSE_SCHEMA,
    EVENT_REGISTRY, ENUM_REGISTRY,
)


class UnknownToolError(Exception):
    """Raised when tool name is not in the registry."""
    pass


class InvalidArgumentsError(Exception):
    """Raised when arguments don't match the tool schema."""
    pass


# -- CBOR serialization helpers --

def _argsToCbor(arguments, schema):
    """Convert dict(field_name -> value) to CBOR bytes (int-key map).

    Args:
        arguments: dict of field names to values.
        schema: list of field dicts with 'name', 'key', 'type', 'default'.

    Returns:
        bytes: CBOR-encoded int-key map.
    """
    if not arguments:
        arguments = {}

    cborMap = {}
    for field in schema:
        name = field['name']
        if name not in arguments:
            # Fill in default if available
            dv = field.get('default')
            if dv is not None:
                val = dv
            else:
                continue
        else:
            val = arguments[name]

        if field['type'] == 'enum':
            ref = field.get('enum_ref')
            if ref is None:
                raise InvalidArgumentsError(
                    "enum field '{}' missing enum_ref".format(name))
            enumIndex = ENUM_REGISTRY[ref]['index']
            if val not in enumIndex:
                raise InvalidArgumentsError(
                    "Invalid enum value '{}' for field '{}'. Valid: {}".format(
                        val, name, list(enumIndex.keys())))
            val = enumIndex[val]
        elif field['type'] == 'array':
            itemFields = field.get('item_fields', [])
            val = _arrayItemsToCbor(val, itemFields)
        elif field['type'] == 'integer':
            val = int(val)
        elif field['type'] == 'boolean':
            val = bool(val)
        elif field['type'] in ('number', 'float'):
            val = float(val)
        elif field['type'] == 'string':
            val = str(val)

        cborMap[field['key']] = val

    return cbor2.dumps(cborMap)


def _arrayItemsToCbor(itemsList, itemFields):
    """Convert a list of item dicts to list of CBOR int-key maps.

    Missing fields are filled from their default before encoding, so the
    wire data is always complete and the C++ decoder stays a pure parser.
    """
    result = []
    for item in itemsList:
        itemMap = {}
        for field in itemFields:
            name = field['name']
            if name not in item:
                # Fill in default if available
                dv = field.get('default')
                if dv is not None:
                    val = dv
                else:
                    continue
            else:
                val = item[name]
            # Skip None values for optional item fields
            if val is None and field.get('presence') == 'optional':
                continue
            if field['type'] == 'enum':
                ref = field.get('enum_ref')
                if ref is None:
                    raise InvalidArgumentsError(
                        "enum field '{}' missing enum_ref".format(name))
                enumIndex = ENUM_REGISTRY[ref]['index']
                if val not in enumIndex:
                    raise InvalidArgumentsError(
                        "Invalid enum value '{}' for field '{}'. Valid: {}".format(
                            val, name, list(enumIndex.keys())))
                val = enumIndex[val]
            elif field['type'] == 'integer':
                val = int(val)
            elif field['type'] == 'boolean':
                val = bool(val)
            elif field['type'] in ('number', 'float'):
                val = float(val)
            elif field['type'] == 'string':
                val = str(val)
            itemMap[field['key']] = val
        result.append(itemMap)
    return result


def _responseArgsToCbor(arguments, schema):
    """Like _argsToCbor but handles array fields with item_fields.
    Skips optional fields (presence='optional') when value is None."""
    if not arguments:
        return cbor2.dumps({})

    cborMap = {}
    for field in schema:
        name = field['name']
        if name not in arguments:
            continue
        val = arguments[name]

        # Skip None values for optional fields
        if val is None and field.get('presence') == 'optional':
            continue

        if field['type'] == 'enum':
            ref = field.get('enum_ref')
            if ref is None:
                raise InvalidArgumentsError(
                    "enum field '{}' missing enum_ref".format(name))
            enumIndex = ENUM_REGISTRY[ref]['index']
            if val not in enumIndex:
                raise InvalidArgumentsError(
                    "Invalid enum value '{}' for field '{}'. Valid: {}".format(
                        val, name, list(enumIndex.keys())))
            val = enumIndex[val]
        elif field['type'] == 'array':
            itemFields = field.get('item_fields', [])
            val = _arrayItemsToCbor(val, itemFields)
        elif field['type'] == 'integer':
            val = int(val)
        elif field['type'] == 'boolean':
            val = bool(val)
        elif field['type'] in ('number', 'float'):
            val = float(val)
        elif field['type'] == 'string':
            val = str(val)

        cborMap[field['key']] = val

    return cbor2.dumps(cborMap)


def _cborToArgs(cborBytes, schema):
    """Convert CBOR bytes to dict(field_name -> value)."""
    if not cborBytes:
        return {}

    raw = cbor2.loads(cborBytes)
    if not isinstance(raw, dict):
        return {}

    result = {}
    for field in schema:
        key = field['key']
        if key not in raw:
            continue
        val = raw[key]

        if field.get('enum_ref'):
            val = _wireToEnumName(field['enum_ref'], val, field['name'])
        elif field['type'] == 'enum':
            enumValues = field.get('enum_values', [])
            if isinstance(val, int) and 0 <= val < len(enumValues):
                val = enumValues[val]
        elif field['type'] == 'array':
            itemFields = field.get('item_fields', [])
            if isinstance(val, list):
                items = []
                for itemRaw in val:
                    item = {}
                    if isinstance(itemRaw, dict):
                        for ifield in itemFields:
                            ikey = ifield['key']
                            if ikey in itemRaw:
                                ival = itemRaw[ikey]
                                if ifield.get('enum_ref'):
                                    ival = _wireToEnumName(ifield['enum_ref'],
                                                           ival, ifield['name'])
                                elif ifield['type'] == 'enum':
                                    ienumValues = ifield.get('enum_values', [])
                                    if isinstance(ival, int) and 0 <= ival < len(ienumValues):
                                        ival = ienumValues[ival]
                                item[ifield['name']] = ival
                    items.append(item)
                val = items

        result[field['name']] = val

    return result


# -- int-key CBOR decode (shared by tool responses & events) --

def _wireToEnumName(ref, val, fieldName):
    """枚举 wire 值 → 名称（公共注册表显式 value / 私有 0..n-1 均可反查）。"""
    entry = ENUM_REGISTRY.get(ref)
    index = entry.get('index', {}) if entry else {}
    nameByWire = {v: k for k, v in index.items()}
    if isinstance(val, bool) or not isinstance(val, int) or val not in nameByWire:
        raise InvalidArgumentsError(
            "Invalid enum wire value {} for field '{}' (enum '{}'). Valid: {}".format(
                val, fieldName, ref, sorted(nameByWire)))
    return nameByWire[val]


def decodeIntKeyCbor(cborBytes, schema):
    """Convert int-key CBOR bytes to dict(field_name -> value).

    Generic form of the params->dict decoding previously inlined in
    decodeToolResponse(); shared with decodeEventFrame(). Schema items
    carry {'name', 'key' (defaults to 1-based field order), 'type',
    'enum_ref', 'presence', ...}. Enum fields resolve their wire value
    back to the value name via ENUM_REGISTRY (explicit registry values,
    sparse sets included); out-of-range wire values raise
    InvalidArgumentsError.
    """
    if not cborBytes:
        return {}

    raw = cbor2.loads(cborBytes)
    if not isinstance(raw, dict):
        return {}

    result = {}
    for i, field in enumerate(schema):
        key = field.get('key', i + 1)
        if key not in raw:
            continue
        val = raw[key]

        if field.get('enum_ref'):
            val = _wireToEnumName(field['enum_ref'], val, field['name'])
        elif field.get('type') == 'enum':
            enumValues = field.get('enum_values', [])
            if isinstance(val, int) and 0 <= val < len(enumValues):
                val = enumValues[val]
        elif field.get('type') == 'array':
            itemFields = field.get('item_fields', [])
            if isinstance(val, list):
                items = []
                for itemRaw in val:
                    item = {}
                    if isinstance(itemRaw, dict):
                        for j, ifield in enumerate(itemFields):
                            ikey = ifield.get('key', j + 1)
                            if ikey in itemRaw:
                                ival = itemRaw[ikey]
                                if ifield.get('enum_ref'):
                                    ival = _wireToEnumName(ifield['enum_ref'],
                                                           ival, ifield['name'])
                                elif ifield.get('type') == 'enum':
                                    ienumValues = ifield.get('enum_values', [])
                                    if isinstance(ival, int) and 0 <= ival < len(ienumValues):
                                        ival = ienumValues[ival]
                                item[ifield['name']] = ival
                    items.append(item)
                val = items

        result[field['name']] = val

    return result


# -- Options & encode (MCP JSON -> Frame list) --

OPTIONS_DEFAULT = {
    'version': 0,      # protocol version, bump only on incompatible change
    'type': 0,         # 0 request / 1 response / 2 event (encodeToolCall: 0 only)
    'encoding': 0,     # 0 = int-key CBOR (only value supported)
    'reply': 0,        # 0 no reply needed / 1 reply required
    'mtu': 512,        # fragment when a frame would exceed this; <=0 disables
}


def _normalizeOptions(options):
    opts = dict(OPTIONS_DEFAULT)
    if options:
        opts.update(options)
    if opts['type'] != 0:
        raise InvalidArgumentsError(
            "encodeToolCall only supports type=0 (request) in this version")
    if opts['encoding'] != 0:
        raise InvalidArgumentsError("only encoding=0 (int-key CBOR) supported")
    if opts['reply'] not in (0, 1):
        raise InvalidArgumentsError("reply must be 0 or 1")
    if not 0 <= opts['version'] <= 7:
        raise InvalidArgumentsError("version must be 0..7")
    return opts


def encodeToolCall(mcpJson, options=None):
    """Encode MCP tool call(s) into a list of wire frames.

    Per-item top-level 'reply' (0/1) overrides options['reply']; items with
    different reply values are split into independent frames automatically.
    Each returned frame is ready for the transport (<= mtu when mtu > 0).
    """
    opts = _normalizeOptions(options)
    items = mcpJson if isinstance(mcpJson, list) else [mcpJson]
    if not items:
        raise InvalidArgumentsError("Empty batch: no JSON-RPC items")

    groups = {}                       # (ver, enc, reply) -> [entry dicts]
    order = []
    for itemJson in items:
        params = itemJson.get("params", {})
        toolName = params.get("name", "")
        if toolName not in TOOL_NAME_TO_ID:
            raise UnknownToolError("Unknown tool: '{}'".format(toolName))
        seqId = itemJson.get("id", 0)
        if not isinstance(seqId, int) or isinstance(seqId, bool) \
                or not 1 <= seqId <= 0xFFFFFFFF:
            raise InvalidArgumentsError(
                "MCP id must be an integer in 1..0xFFFFFFFF (seq_id 0 reserved)")
        try:
            reply = int(itemJson.get("reply", opts['reply']))
        except (TypeError, ValueError):
            raise InvalidArgumentsError("reply must be an integer")
        if reply not in (0, 1):
            raise InvalidArgumentsError("reply must be 0 or 1")

        paraBytes = _argsToCbor(params.get("arguments", {}),
                                TOOL_NAME_TO_REQUEST_SCHEMA.get(toolName, []))
        key = (opts['version'], opts['encoding'], reply)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append({
            'seq_id': seqId,
            'opcode': TOOL_NAME_TO_ID[toolName],
            'params': paraBytes,
        })

    mtu = opts['mtu']
    entryMax = ATOMIC_MTU_BYTES - FIXED_HEADER_SIZE if mtu <= 0 else mtu - FIXED_HEADER_SIZE
    frames = []
    for key in order:
        ver, enc, reply = key
        pdus, current, curLen = [], [], 0
        for entry in groups[key]:
            entryLen = 10 + len(entry['params'])
            if current and curLen + entryLen > entryMax:
                pdus.append(current)
                current, curLen = [], 0
            current.append(entry)
            curLen += entryLen
        if current:
            pdus.append(current)
        for pdu in pdus:
            payload = encodePayload(pdu)
            frames.extend(fragmentPayload(
                payload, ver, MSG_TYPE_REQUEST, enc, reply, mtu))
    return frames


# -- decode (Frame list -> dicts) --

def _reassembleEntries(frameList):
    if not frameIsComplete(frameList):
        raise FrameIncompleteError("frame group not complete")
    header, payload = reassembleWithHeader(frameList)
    entries = decodePayload(payload)
    return header, entries


def decodeToolRequest(frameList):
    """Decode a complete request frame group into per-command dicts.

    Accepts a single frame (bytes) or a list of frames; a lone bytes value
    is wrapped automatically (legacy single-frame call sites).
    """
    if isinstance(frameList, (bytes, bytearray)):
        frameList = [bytes(frameList)]
    header, entries = _reassembleEntries(frameList)
    decoded = []
    for entry in entries:
        toolId = entry['opcode']
        argsDict = _cborToArgs(entry['params'],
                               TOOL_ID_TO_REQUEST_SCHEMA.get(toolId, [])) \
            if entry['params_len'] > 0 else {}
        decoded.append({
            'ver': frameVer(header),
            'type': frameType(header),
            'enc': frameEnc(header),
            'reply': frameReply(header),
            'seq_id': entry['seq_id'],
            'opcode': toolId,
            'tool_id': toolId,
            'tool_name': TOOL_ID_TO_NAME.get(toolId, "unknown"),
            'arguments': argsDict,
        })
    return decoded


def isQueryTool(toolName):
    """Query tools (name suffix _list/_get) always respond regardless of reply.

    Aligned with embedded atomicIsQueryCommand() in atomic_commands.h.
    """
    return toolName.endswith("_list") or toolName.endswith("_get")


def buildResponseFrame(items, options=None):
    """Build response frame(s) from [{'seq_id','opcode','response_args'}]."""
    opts = dict(OPTIONS_DEFAULT)
    if options:
        opts.update(options)
    entries = []
    for item in items:
        toolId = item['opcode']
        schema = TOOL_ID_TO_RESPONSE_SCHEMA.get(toolId, [])
        paraBytes = _responseArgsToCbor(item['response_args'], schema)
        entries.append({'seq_id': item['seq_id'], 'opcode': toolId,
                        'params': paraBytes})
    payload = encodePayload(entries)
    return fragmentPayload(payload, opts['version'], MSG_TYPE_RESPONSE,
                           opts['encoding'], 0, opts['mtu'])


_eventSeqId = [0]


def buildEventFrame(eventId, paramsCbor, options=None, seqId=None):
    """Build an event frame (t=2, rp=0). seqId defaults to local increment."""
    opts = dict(OPTIONS_DEFAULT)
    if options:
        opts.update(options)
    if seqId is None:
        _eventSeqId[0] = _eventSeqId[0] % 0xFFFFFFFF + 1
        seqId = _eventSeqId[0]
    payload = encodePayload([
        {'seq_id': seqId, 'opcode': eventId, 'params': bytes(paramsCbor)}])
    return fragmentPayload(payload, opts['version'], MSG_TYPE_EVENT,
                           opts['encoding'], 0, opts['mtu'])


def decodeEventFrame(frameList):
    """事件帧组 → {"event": name, "arguments": {...}}；枚举 wire 值反查名称。"""
    if isinstance(frameList, (bytes, bytearray)):
        frameList = [bytes(frameList)]
    header, payload = reassembleWithHeader(frameList)
    entries = decodePayload(payload)
    if not entries:
        return None
    ent = entries[0]
    eventId = ent['opcode']
    params = ent['params']
    nameById = {v['id']: k for k, v in EVENT_REGISTRY.items()}
    if eventId not in nameById:
        raise UnknownToolError('Unknown event id {}'.format(eventId))
    name = nameById[eventId]
    fields = decodeIntKeyCbor(bytes(params), EVENT_REGISTRY[name]['fields'])
    return {"event": name, "arguments": fields}


def decodeToolResponse(frameList):
    """Convert response frame group to MCP JSON-RPC response(s).

    Accepts a single frame (bytes) or a list of frames; a lone bytes value
    is wrapped automatically (legacy single-frame call sites).
    """
    if isinstance(frameList, (bytes, bytearray)):
        frameList = [bytes(frameList)]
    _, entries = _reassembleEntries(frameList)
    if not entries:
        return None
    responses = []
    for entry in entries:
        toolId = entry['opcode']
        responseSchema = TOOL_ID_TO_RESPONSE_SCHEMA.get(toolId, [])
        if not responseSchema:
            errorText = json.dumps(
                {"code": -1, "error": "unknown_tool"}, ensure_ascii=False)
            responses.append({"jsonrpc": "2.0", "id": entry['seq_id'],
                              "result": {"content": [
                                  {"type": "text", "text": errorText}]}})
            continue
        resultDict = decodeIntKeyCbor(entry['params'], responseSchema) \
            if entry['params_len'] > 0 else {}
        if "code" not in resultDict:
            resultDict["code"] = 0
        textContent = json.dumps(resultDict, ensure_ascii=False)
        responses.append({"jsonrpc": "2.0", "id": entry['seq_id'],
                          "result": {"content": [
                              {"type": "text", "text": textContent}]}})
    return responses[0] if len(responses) == 1 else responses


def decodeToolResult(frameList):
    """Convert response frame group to plain business dict(s) directly.

    Unlike decodeToolResponse() which wraps result in MCP content/text
    format, this function returns the business fields as a plain dict:

        {"jsonrpc": "2.0", "id": 1, "result": {"code": 0, "total": 2, ...}}

    Args:
        frameList: Frame list of one complete response PDU. A single bytes
            frame is also accepted (legacy single-frame call sites).

    Returns:
        dict (single entry), list[dict] (batch), or None (no entries).
        Each dict has keys: jsonrpc, id, result (plain business dict).
    """
    if isinstance(frameList, (bytes, bytearray)):
        frameList = [bytes(frameList)]
    _, entries = _reassembleEntries(frameList)
    if not entries:
        return None
    responses = []
    for entry in entries:
        toolId = entry['opcode']
        responseSchema = TOOL_ID_TO_RESPONSE_SCHEMA.get(toolId, [])
        if not responseSchema:
            responses.append({
                "jsonrpc": "2.0",
                "id": entry['seq_id'],
                "result": {"code": -1, "error": "unknown_tool"}
            })
            continue
        resultDict = _cborToArgs(entry['params'], responseSchema) \
            if entry['params_len'] > 0 else {}
        if "code" not in resultDict:
            resultDict["code"] = 0
        responses.append({
            "jsonrpc": "2.0",
            "id": entry['seq_id'],
            "result": resultDict
        })
    return responses[0] if len(responses) == 1 else responses
