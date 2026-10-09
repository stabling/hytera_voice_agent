# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/capture/sms.py
# 所属层次：TShark 抓包解析层
# 主要职责：从 TMP UDP 原始载荷中识别并解析 Hytera 文本短信，不依赖厂商 CLR 回调。
# 兼容性：只读解析抓包字节；不改变既有 ADK/TMP 发送、HTTP、protobuf 或回调契约。
# =============================================================================
from __future__ import annotations

from typing import Any


_HDAP_MAGIC = b"2B"
_DATA_FRAME_TYPE = 0x0020
_TMP_MESSAGE_HEADER = 0x09
_TMP_MESSAGE_FOOTER = 0x03
_MIN_TEXT_PAYLOAD_SIZE = 12  # RequestID(4) + DesIP(4) + SrcIP(4)


def _result(error: str, *, candidate: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "is_sms": False,
        "candidate": candidate,
        "error": error,
    }


def _decode_utf16le_text(data: bytes) -> str | None:
    """严格解码 TMP 文本字段。

    现场抓包确认 H-Series TMP 文本使用 UTF-16LE。这里拒绝奇数字节和
    非文本控制字符，避免把 TMP 状态/应答负载误判成短信正文。
    """
    if not data or len(data) % 2:
        return None
    try:
        text = data.decode("utf-16le", errors="strict").rstrip("\x00")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    for char in text:
        if char in "\r\n\t":
            continue
        if not char.isprintable():
            return None
    return text


def _find_tmp_message(payload: bytes) -> tuple[int, int, int, bytes, int] | None:
    """定位内层 TMP 消息。

    内层格式由协议资料及实抓共同确认：
      MsgHdr(0x09) + Opcode(2) + #of bytes(2) + Payload(n) + Checksum(1) + 0x03

    不硬编码外层 HDAP 头长度；通过长度与结束符双重约束定位，可兼容外层
    字段的小版本差异。
    """
    # 至少保留 1+2+2+12+1+1 字节给完整短信内层消息。
    for start in range(6, max(6, len(payload) - 18 + 1)):
        if payload[start] != _TMP_MESSAGE_HEADER:
            continue
        if start + 5 > len(payload):
            break
        opcode = int.from_bytes(payload[start + 1 : start + 3], "big")
        message_length = int.from_bytes(payload[start + 3 : start + 5], "big")
        body_start = start + 5
        checksum_index = body_start + message_length
        footer_index = checksum_index + 1
        if footer_index >= len(payload):
            continue
        if payload[footer_index] != _TMP_MESSAGE_FOOTER:
            continue
        # 当前 H-Series UDP 样本中 TMP 是帧尾最后一层。拒绝尾随未知字节，
        # 防止在其他业务协议的正文中偶然命中 0x09。
        if footer_index + 1 != len(payload):
            continue
        body = payload[body_start:checksum_index]
        return start, opcode, message_length, body, payload[checksum_index]
    return None


def parse_hytera_tmp_sms(payload: bytes) -> dict[str, Any]:
    """解析一条 Hytera H-Series TMP 短信 UDP payload。

    返回 ``ok=True`` 时字段包括：request_id、destination_id、source_id、text、
    opcode、frame_sequence 等。ACK（0x0001）、心跳（0x0002）以及非 TMP 文本
    数据返回 ``is_sms=False``，调用方可直接忽略。
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return _result("payload must be bytes-like")
    raw = bytes(payload)
    if len(raw) < 6:
        return _result("payload too short")
    if raw[:2] != _HDAP_MAGIC:
        return _result("not Hytera HDAP 0x3242 frame")

    frame_type = int.from_bytes(raw[2:4], "big")
    frame_sequence = int.from_bytes(raw[4:6], "big")
    if frame_type != _DATA_FRAME_TYPE:
        return _result(f"not HDAP data frame: 0x{frame_type:04x}")

    located = _find_tmp_message(raw)
    if located is None:
        return _result("TMP message header/length/footer not found", candidate=True)
    inner_offset, opcode, message_length, body, checksum = located
    if len(body) < _MIN_TEXT_PAYLOAD_SIZE:
        return _result("TMP payload too short for text addressing fields", candidate=True)

    request_id = int.from_bytes(body[0:4], "big")
    # Hytera TMP protocol diagram orders the two 32-bit endpoint fields as
    # DesIP then SrcIP. Keep those names here instead of inferring direction
    # from Ethernet/IP addresses, because every observed TMP event travels
    # repeater -> ADK host regardless of radio sender direction.
    destination_id = int.from_bytes(body[4:8], "big")
    source_id = int.from_bytes(body[8:12], "big")
    text_bytes = body[12:]
    text = _decode_utf16le_text(text_bytes)
    if text is None:
        return _result("TMP payload does not contain valid UTF-16LE text", candidate=True)

    return {
        "ok": True,
        "is_sms": True,
        "candidate": True,
        "error": None,
        "frame_type": frame_type,
        "frame_sequence": frame_sequence,
        "inner_offset": inner_offset,
        "opcode": opcode,
        "opcode_hex": f"0x{opcode:04X}",
        "message_length": message_length,
        "request_id": request_id,
        "destination_id": destination_id,
        "source_id": source_id,
        "text": text,
        "text_encoding": "utf-16le",
        "text_bytes_hex": text_bytes.hex(),
        "checksum": checksum,
        "checksum_hex": f"0x{checksum:02X}",
        # Checksum 算法在当前资料中未给出，不能凭样本臆造校验规则。
        "checksum_valid": None,
        "raw_payload_hex": raw.hex(),
    }


__all__ = ["parse_hytera_tmp_sms"]
