# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/capture/packet.py
# 所属层次：语音抓包层
# 主要职责：负责 TShark 抓包、RTP/UDP 数据整理、会话产物和抓包状态管理。
# 项目内直接依赖：.io_utils
# 主要公开符号：clean_hex_to_bytes, parse_rtp_packet, parse_rtp_from_tshark_fields, extract_hytera_radio_id, read_length_prefixed_packets, write_pcmu_wav_from_raw
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import Any, Mapping

from .io_utils import ensure_dir

try:
    import audioop
except Exception:  # pragma: no cover
    audioop = None


def clean_hex_to_bytes(text: str) -> bytes:
    """将 TShark 输出的十六进制文本转为字节串。"""
    if not text:
        return b""
    text = text.strip().strip('"')
    for sep in (":", " ", ",", ";"):
        text = text.replace(sep, "")
    if not text or len(text) % 2:
        return b""
    try:
        return bytes.fromhex(text)
    except ValueError:
        return b""


def _parse_tshark_int(value: Any) -> int | None:
    """解析 TShark 可能输出的十进制或十六进制整数。"""
    if value in (None, ""):
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except (TypeError, ValueError):
        return None


def _parse_tshark_bool(value: Any) -> bool | None:
    """解析 TShark 布尔字段，兼容 0/1 与 True/False 文本。"""
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "set"}:
        return True
    if text in {"0", "false", "no", "not set"}:
        return False
    return None


def parse_rtp_packet(data: bytes) -> dict[str, Any]:
    """严格解析从 UDP 载荷字节 0 开始的 RTP v2 数据包。

    该函数只接受完整 UDP 载荷，不扫描或猜测厂商前置字节。这样可以避免把
    任意 UDP 数据误判为 RTP。若当前 TShark 版本不能导出 ``udp.payload``，
    抓包管理器会改用 TShark 已解码的 RTP 字段与 ``rtp.payload``。
    """
    if len(data) < 12:
        return {"ok": False, "error": f"too short for RTP header: {len(data)} bytes"}

    b0 = data[0]
    b1 = data[1]
    version = (b0 >> 6) & 0x03
    padding = (b0 >> 5) & 0x01
    extension = (b0 >> 4) & 0x01
    csrc_count = b0 & 0x0F
    marker = (b1 >> 7) & 0x01
    payload_type = b1 & 0x7F

    if version != 2:
        return {
            "ok": False,
            "error": f"not RTP v2: version={version}",
            "version": version,
            "payload_type": payload_type,
        }

    sequence = struct.unpack("!H", data[2:4])[0]
    timestamp = struct.unpack("!I", data[4:8])[0]
    ssrc = struct.unpack("!I", data[8:12])[0]
    offset = 12

    csrc_values: list[int] = []
    csrc_bytes = b""
    if csrc_count:
        csrc_len = csrc_count * 4
        if len(data) < offset + csrc_len:
            return {"ok": False, "error": "packet too short for CSRC list"}
        csrc_bytes = data[offset : offset + csrc_len]
        csrc_values = list(struct.unpack(f"!{csrc_count}I", csrc_bytes))
        offset += csrc_len

    extension_header = b""
    extension_profile = None
    extension_length_words = 0
    extension_data = b""
    if extension:
        if len(data) < offset + 4:
            return {"ok": False, "error": "packet too short for RTP extension header"}
        extension_header = data[offset : offset + 4]
        extension_profile = struct.unpack("!H", data[offset : offset + 2])[0]
        extension_length_words = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
        offset += 4
        extension_data_len = extension_length_words * 4
        if len(data) < offset + extension_data_len:
            return {"ok": False, "error": "packet too short for RTP extension data"}
        extension_data = data[offset : offset + extension_data_len]
        offset += extension_data_len

    payload_with_padding = data[offset:]
    padding_count = 0
    if padding:
        if not payload_with_padding:
            return {"ok": False, "error": "RTP padding flag set but packet has no padding octet"}
        padding_count = payload_with_padding[-1]
        if padding_count <= 0:
            return {"ok": False, "error": "RTP padding count must be greater than zero"}
        if padding_count > len(payload_with_padding):
            return {
                "ok": False,
                "error": "RTP padding count is greater than remaining payload length",
            }
        audio_payload = payload_with_padding[:-padding_count]
    else:
        audio_payload = payload_with_padding

    return {
        "ok": True,
        "error": None,
        "parse_source": "udp_payload",
        "version": version,
        "padding": padding,
        "padding_count": padding_count,
        "extension": extension,
        "csrc_count": csrc_count,
        "csrc_values": csrc_values,
        "marker": marker,
        "payload_type": payload_type,
        "sequence": sequence,
        "timestamp": timestamp,
        "ssrc": ssrc,
        "ssrc_hex": f"0x{ssrc:08x}",
        "header_len": offset,
        "base_header": data[:12],
        "csrc_bytes": csrc_bytes,
        "extension_header": extension_header,
        "extension_profile": extension_profile,
        "extension_length_words": extension_length_words,
        "extension_data": extension_data,
        "rtp_packet_size": len(data),
        "audio_payload": audio_payload,
        "audio_payload_size": len(audio_payload),
    }


def parse_rtp_from_tshark_fields(
    fields: Mapping[str, Any], audio_payload: bytes
) -> dict[str, Any]:
    """使用 TShark 已解码字段构造 RTP 结果。

    这是旧版 Wireshark 没有 ``udp.payload`` 字段时的兼容路径。只有版本、
    payload type、sequence、timestamp 和 SSRC 全部可验证时才返回成功，
    不会因端口命中就把任意 UDP 包当作 RTP。
    """
    version = _parse_tshark_int(fields.get("rtp_version"))
    payload_type = _parse_tshark_int(fields.get("rtp_payload_type"))
    sequence = _parse_tshark_int(fields.get("rtp_sequence"))
    timestamp = _parse_tshark_int(fields.get("rtp_timestamp"))
    ssrc = _parse_tshark_int(fields.get("rtp_ssrc"))
    missing = [
        name
        for name, value in (
            ("rtp.version", version),
            ("rtp.p_type", payload_type),
            ("rtp.seq", sequence),
            ("rtp.timestamp", timestamp),
            ("rtp.ssrc", ssrc),
        )
        if value is None
    ]
    if missing:
        return {"ok": False, "error": "missing TShark RTP fields: " + ", ".join(missing)}
    if version != 2:
        return {"ok": False, "error": f"not RTP v2: version={version}"}
    if not 0 <= int(payload_type) <= 127:
        return {"ok": False, "error": f"invalid RTP payload type: {payload_type}"}
    if not 0 <= int(sequence) <= 0xFFFF:
        return {"ok": False, "error": f"invalid RTP sequence: {sequence}"}
    if not 0 <= int(timestamp) <= 0xFFFFFFFF:
        return {"ok": False, "error": f"invalid RTP timestamp: {timestamp}"}
    if not 0 <= int(ssrc) <= 0xFFFFFFFF:
        return {"ok": False, "error": f"invalid RTP SSRC: {ssrc}"}

    padding = bool(_parse_tshark_bool(fields.get("rtp_padding")) or False)
    parsed_padding_count = _parse_tshark_int(fields.get("rtp_padding_count"))
    if padding and parsed_padding_count is None:
        return {"ok": False, "error": "missing TShark RTP field: rtp.padding.count"}
    padding_count = parsed_padding_count or 0
    if padding and not 1 <= padding_count <= 255:
        return {"ok": False, "error": f"invalid RTP padding count: {padding_count}"}
    if not padding and padding_count:
        return {"ok": False, "error": "RTP padding count exists while padding flag is clear"}

    parsed_csrc_count = _parse_tshark_int(fields.get("rtp_csrc_count"))
    csrc_count = parsed_csrc_count or 0
    if not 0 <= csrc_count <= 15:
        return {"ok": False, "error": f"invalid RTP CSRC count: {csrc_count}"}

    extension = bool(_parse_tshark_bool(fields.get("rtp_extension")) or False)
    extension_profile = _parse_tshark_int(fields.get("rtp_extension_profile"))
    parsed_extension_length = _parse_tshark_int(fields.get("rtp_extension_length"))
    if extension and (extension_profile is None or parsed_extension_length is None):
        return {
            "ok": False,
            "error": "missing TShark RTP extension profile or length",
        }
    extension_length_words = parsed_extension_length or 0
    if not 0 <= extension_length_words <= 0xFFFF:
        return {
            "ok": False,
            "error": f"invalid RTP extension length: {extension_length_words}",
        }
    marker = bool(_parse_tshark_bool(fields.get("rtp_marker")) or False)

    return {
        "ok": True,
        "error": None,
        "parse_source": "tshark_rtp_fields",
        "version": int(version),
        "padding": int(padding),
        "padding_count": int(padding_count),
        "extension": int(extension),
        "csrc_count": int(csrc_count),
        "csrc_values": [],
        "marker": int(marker),
        "payload_type": int(payload_type),
        "sequence": int(sequence),
        "timestamp": int(timestamp),
        "ssrc": int(ssrc),
        "ssrc_hex": f"0x{int(ssrc):08x}",
        "header_len": None,
        "base_header": b"",
        "csrc_bytes": b"",
        "extension_header": b"",
        "extension_profile": extension_profile,
        "extension_length_words": int(extension_length_words),
        "extension_data": b"",
        "rtp_packet_size": None,
        "audio_payload": audio_payload,
        "audio_payload_size": len(audio_payload),
    }


def extract_hytera_radio_id(extension_data: bytes) -> int | None:
    """从已验证的 Hytera RTP 扩展数据中提取源终端 Radio ID。"""
    if not extension_data or len(extension_data) < 4:
        return None
    if extension_data[0] != 0x02:
        return None
    radio_id = int.from_bytes(extension_data[1:4], byteorder="big")
    if radio_id <= 0 or radio_id > 0xFFFFFF:
        return None
    if len(extension_data) >= 7:
        radio_id_dup = int.from_bytes(extension_data[4:7], byteorder="big")
        if radio_id_dup == radio_id:
            return radio_id
        if radio_id in (0, 0xFFFFFF):
            return None
    return radio_id


def read_length_prefixed_packets(path: Path, limit: int | None = None) -> list[bytes]:
    """从带长度前缀的二进制文件中读取所有数据包。"""
    packets: list[bytes] = []
    if not path.is_file():
        return packets
    with path.open("rb") as f:
        while True:
            raw_len = f.read(4)
            if not raw_len or len(raw_len) < 4:
                break
            size = struct.unpack("!I", raw_len)[0]
            payload = f.read(size)
            if len(payload) != size:
                break
            packets.append(payload)
            if limit is not None and len(packets) >= limit:
                break
    return packets


def write_pcmu_wav_from_raw(raw_path: Path, wav_path: Path) -> dict[str, Any]:
    """将原始 PCMU 音频转换为 8 kHz 单声道 WAV。"""
    if audioop is None:
        return {"ok": False, "error": "audioop 模块不可用（当前 Python 运行时不支持）"}
    if not raw_path.is_file() or raw_path.stat().st_size == 0:
        return {"ok": False, "error": "音频 payload 原始文件不存在或为空"}
    try:
        raw = raw_path.read_bytes()
        pcm16 = audioop.ulaw2lin(raw, 2)
        ensure_dir(wav_path.parent)
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(pcm16)
        return {
            "ok": True,
            "wav_path": str(wav_path),
            "input_bytes": len(raw),
            "pcm_bytes": len(pcm16),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
