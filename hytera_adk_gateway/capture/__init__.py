# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/capture/__init__.py
# 所属层次：语音抓包层
# 主要职责：负责 TShark 抓包、RTP/UDP 数据整理、会话产物和抓包状态管理。
# 项目内直接依赖：.manager, .models, .packet, .tshark
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from .manager import TSharkVoiceCaptureManager
from .models import CaptureSession
from .sms import parse_hytera_tmp_sms
from .packet import (
    parse_rtp_from_tshark_fields,
    parse_rtp_packet,
    read_length_prefixed_packets,
    write_pcmu_wav_from_raw,
)
from .tshark import (
    TSharkCapabilities,
    find_tshark,
    list_tshark_interfaces,
    make_bpf_filter,
    probe_tshark_capabilities,
    validate_tshark_interface,
)

__all__ = [
    "TSharkVoiceCaptureManager",
    "CaptureSession",
    "parse_hytera_tmp_sms",
    "parse_rtp_packet",
    "parse_rtp_from_tshark_fields",
    "read_length_prefixed_packets",
    "write_pcmu_wav_from_raw",
    "TSharkCapabilities",
    "find_tshark",
    "list_tshark_interfaces",
    "make_bpf_filter",
    "probe_tshark_capabilities",
    "validate_tshark_interface",
]
