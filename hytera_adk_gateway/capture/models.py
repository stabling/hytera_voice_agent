# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/capture/models.py
# 所属层次：语音抓包层
# 主要职责：负责 TShark 抓包、RTP/UDP 数据整理、会话产物和抓包状态管理。
# 项目内直接依赖：.io_utils
# 主要公开符号：CaptureSession
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .io_utils import now_iso


@dataclass
class CaptureSession:
    """TShark 抓包会话的状态信息。"""

    session_id: str
    out_dir: Path
    iface: str
    ports: list[int]
    bpf_filter: str
    rtp_ports: list[int] = field(default_factory=list)
    sms_ports: list[int] = field(default_factory=list)
    src_ip: str | None = None
    dst_ip: str | None = None
    host: str | None = None
    strict_direction: bool = False
    tshark_path: str | None = None
    tshark_command: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    stopped_at: str | None = None
    running: bool = False
    packet_count: int = 0
    rtp_count: int = 0
    non_rtp_count: int = 0
    sms_count: int = 0
    audio_payload_bytes: int = 0
    last_error: str | None = None
    process_returncode: int | None = None
    tshark_version: str | None = None
    tshark_payload_fields: list[str] = field(default_factory=list)
    decode_as_rtp: bool = False
    tshark_rtp_field_fallback_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "out_dir": str(self.out_dir),
            "iface": self.iface,
            "ports": self.ports,
            "bpf_filter": self.bpf_filter,
            "rtp_ports": self.rtp_ports,
            "sms_ports": self.sms_ports,
            "src_ip": self.src_ip,
            "dst_ip": self.dst_ip,
            "host": self.host,
            "strict_direction": self.strict_direction,
            "tshark_path": self.tshark_path,
            "tshark_command": self.tshark_command,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "running": self.running,
            "packet_count": self.packet_count,
            "rtp_count": self.rtp_count,
            "non_rtp_count": self.non_rtp_count,
            "sms_count": self.sms_count,
            "audio_payload_bytes": self.audio_payload_bytes,
            "last_error": self.last_error,
            "process_returncode": self.process_returncode,
            "tshark_version": self.tshark_version,
            "tshark_payload_fields": self.tshark_payload_fields,
            "decode_as_rtp": self.decode_as_rtp,
            "tshark_rtp_field_fallback_ready": self.tshark_rtp_field_fallback_ready,
        }
