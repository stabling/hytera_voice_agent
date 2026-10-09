# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/settings/model.py
# 所属层次：网关配置层
# 主要职责：负责 gateway.ini 与环境配置的解析、校验和只读配置模型。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：GatewayConfig
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""网关配置数据模型与稳定常量。

该模块不执行文件 I/O，只描述配置结果，便于业务模块依赖类型而非解析实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GatewayConfig:
    path: Path
    runtime_dir: Path
    local_ip: str
    bootstrap: str
    managed_ini: bool
    detector_type: int
    listener_ip: str
    ports: dict[str, int]
    audio_mode: str
    input_device: int
    output_device: int
    play_flag: int
    auto_channel: bool
    auto_attach_audio: bool
    rrs_auto_ack: bool
    rrs_valid_time: int
    server_host: str
    server_port: int
    recordings_dir: Path
    wav_sample_rate: int
    wav_channels: int
    wav_sample_width: int
    wav_assume_pcm: bool
    udp_recording_enabled: bool
    udp_recording_bind_ip: str
    udp_recording_ports: list[int]
    voice_capture_auto_start: bool
    voice_capture_iface: str | None
    voice_capture_ports: list[int]
    voice_capture_src_ip: str | None
    voice_capture_dst_ip: str | None
    voice_capture_host: str | None
    voice_capture_strict_direction: bool
    voice_capture_out_dir: str | None
    voice_capture_tshark_path: str | None
    voice_callback_enabled: bool
    voice_callback_url: str | None
    voice_callback_interval_seconds: float
    voice_callback_timeout_seconds: float
    voice_callback_max_packets_per_tick: int
    voice_callback_merge_continuous_audio: bool
    voice_callback_merge_gap_seconds: float
    voice_callback_merge_max_packets: int
    voice_callback_merge_max_audio_bytes: int
    voice_callback_merge_flush_on_idle_tick: bool
    voice_callback_merge_flush_on_batch_end: bool
    voice_callback_fallback_read_audio_raw: bool
    voice_capture_root: Path
    voice_send_root: Path
    channel_mode: str
    default_channel_id: int | None
    default_slot1_channel_id: int | None
    default_slot2_channel_id: int | None
    fallback_for_voice: bool
    watchdog_interval_seconds: float
    thread_join_timeout_seconds: float
    event_history_size: int
    error_history_size: int
    protobuf_tools_max_request_bytes: int
    protobuf_tools_private_target_id: int
    protobuf_tools_send_timeout_seconds: float


# This is the listener profile observed in the working vendor demo capture.
# All services share the same ports on the PC and the repeater; each service
# has one port per TDMA slot. Keep this profile aligned with CPS.
PORT_KEYS = (
    "rrs_slot1",
    "rrs_slot2",
    "lp_slot1",
    "lp_slot2",
    "tp_slot1",
    "tp_slot2",
    "tmp_slot1",
    "tmp_slot2",
    "rcp_slot1",
    "rcp_slot2",
    "rtp_slot1",
    "rtp_slot2",
    "self_define_slot1",
    "self_define_slot2",
)

# RepeaterPortInfo used by ConfigureRepeater does not expose all legacy
# listener families on every V4 CLR build. The golden capture proves the
# compatible path for this deployment is INI-owned listener creation.
AUXILIARY_INI_ONLY_PORT_KEYS = (
    "lp_slot1",
    "lp_slot2",
    "tp_slot1",
    "tp_slot2",
    "self_define_slot1",
    "self_define_slot2",
)
CORE_PORT_KEYS = tuple(name for name in PORT_KEYS if name not in AUXILIARY_INI_ONLY_PORT_KEYS)
