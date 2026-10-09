"""Tiny CBOR fallback used only when external cbor2 is unavailable.

Supports the data types emitted by the provided tool registry: None, bool,
int, float, bytes, str, list/tuple and dict. It intentionally does not
implement tags or indefinite-length items.
"""
from __future__ import annotations

import struct
from typing import Any


def _head(major: int, value: int) -> bytes:
    if value < 0:
        raise ValueError("CBOR length/value must be non-negative")
    prefix = major << 5
    if value < 24:
        return bytes([prefix | value])
    if value <= 0xFF:
        return bytes([prefix | 24, value])
    if value <= 0xFFFF:
        return bytes([prefix | 25]) + struct.pack(">H", value)
    if value <= 0xFFFFFFFF:
        return bytes([prefix | 26]) + struct.pack(">I", value)
    if value <= 0xFFFFFFFFFFFFFFFF:
        return bytes([prefix | 27]) + struct.pack(">Q", value)
    raise OverflowError("integer too large for CBOR uint64")


def dumps(value: Any) -> bytes:
    if value is None:
        return b"\xf6"
    if value is False:
        return b"\xf4"
    if value is True:
        return b"\xf5"
    if isinstance(value, int):
        if value >= 0:
            return _head(0, value)
        return _head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return _head(2, len(raw)) + raw
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(dumps(item) for item in value)
    if isinstance(value, dict):
        out = bytearray(_head(5, len(value)))
        for key, item in value.items():
            out += dumps(key)
            out += dumps(item)
        return bytes(out)
    raise TypeError(f"unsupported CBOR type: {type(value).__name__}")


def _read_uint(raw: bytes, pos: int, ai: int) -> tuple[int, int]:
    if ai < 24:
        return ai, pos
    if ai == 24:
        if pos + 1 > len(raw): raise ValueError("truncated CBOR")
        return raw[pos], pos + 1
    if ai == 25:
        if pos + 2 > len(raw): raise ValueError("truncated CBOR")
        return struct.unpack_from(">H", raw, pos)[0], pos + 2
    if ai == 26:
        if pos + 4 > len(raw): raise ValueError("truncated CBOR")
        return struct.unpack_from(">I", raw, pos)[0], pos + 4
    if ai == 27:
        if pos + 8 > len(raw): raise ValueError("truncated CBOR")
        return struct.unpack_from(">Q", raw, pos)[0], pos + 8
    raise ValueError("indefinite-length CBOR is not supported")


def _load(raw: bytes, pos: int) -> tuple[Any, int]:
    if pos >= len(raw):
        raise ValueError("truncated CBOR")
    initial = raw[pos]
    pos += 1
    major = initial >> 5
    ai = initial & 0x1F

    if major in (0, 1):
        value, pos = _read_uint(raw, pos, ai)
        return (value if major == 0 else -1 - value), pos
    if major in (2, 3):
        length, pos = _read_uint(raw, pos, ai)
        end = pos + length
        if end > len(raw): raise ValueError("truncated CBOR")
        chunk = raw[pos:end]
        return (chunk if major == 2 else chunk.decode("utf-8")), end
    if major == 4:
        length, pos = _read_uint(raw, pos, ai)
        items = []
        for _ in range(length):
            item, pos = _load(raw, pos)
            items.append(item)
        return items, pos
    if major == 5:
        length, pos = _read_uint(raw, pos, ai)
        result = {}
        for _ in range(length):
            key, pos = _load(raw, pos)
            item, pos = _load(raw, pos)
            result[key] = item
        return result, pos
    if major == 7:
        if ai == 20: return False, pos
        if ai == 21: return True, pos
        if ai == 22: return None, pos
        if ai == 26:
            if pos + 4 > len(raw): raise ValueError("truncated CBOR")
            return struct.unpack_from(">f", raw, pos)[0], pos + 4
        if ai == 27:
            if pos + 8 > len(raw): raise ValueError("truncated CBOR")
            return struct.unpack_from(">d", raw, pos)[0], pos + 8
    raise ValueError(f"unsupported CBOR major/additional info: {major}/{ai}")


def loads(data: bytes | bytearray | memoryview) -> Any:
    raw = bytes(data)
    value, pos = _load(raw, 0)
    if pos != len(raw):
        raise ValueError("trailing bytes after CBOR item")
    return value
