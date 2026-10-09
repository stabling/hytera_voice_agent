# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/settings/loader.py
# 所属层次：网关配置层
# 主要职责：负责 gateway.ini 与环境配置的解析、校验和只读配置模型。
# 项目内直接依赖：.model, .parsing
# 主要公开符号：load_config
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""将 ``gateway.ini`` 解析为不可变的 :class:`GatewayConfig`。"""

from __future__ import annotations

import configparser
import ipaddress
from pathlib import Path

from .model import AUXILIARY_INI_ONLY_PORT_KEYS, PORT_KEYS, GatewayConfig
from .parsing import (
    ConfigError,
    _optional_ipv4,
    _optional_positive_int,
    _optional_text,
    _optional_url,
    _port_list,
    _read_ipv4,
    _resolve_runtime,
    _section_getboolean,
    _section_getfloat,
    _section_getint,
)


def load_config(path: str | Path) -> GatewayConfig:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Config not found: {path}")

    raw = configparser.ConfigParser(interpolation=None)
    raw.read(path, encoding="utf-8")
    try:
        runtime_dir = _resolve_runtime(path, raw.get("adk", "runtime_dir"))
        local_ip = _read_ipv4(raw, "network", "local_ip")
        ports = {
            name: raw.getint("ports", name, fallback=0)
            if name in AUXILIARY_INI_ONLY_PORT_KEYS
            else raw.getint("ports", name)
            for name in PORT_KEYS
        }
    except (configparser.Error, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Invalid gateway config: {exc}") from exc

    for name, value in ports.items():
        minimum = 0 if name in AUXILIARY_INI_ONLY_PORT_KEYS else 1
        if not minimum <= value <= 65535:
            expected = "0..65535" if minimum == 0 else "1..65535"
            raise ConfigError(f"Invalid UDP port {name}={value}; expected {expected}")
    active_ports = [value for value in ports.values() if value > 0]
    if len(set(active_ports)) != len(active_ports):
        raise ConfigError("All enabled ADK UDP ports must be unique.")
    if ports["rtp_slot1"] % 2 or ports["rtp_slot2"] % 2:
        raise ConfigError("rtp_slot1 and rtp_slot2 must be even for Hytera repeater voice.")

    bootstrap = (
        raw.get("adk", "bootstrap", fallback=raw.get("adk", "repeater_config", fallback="explicit"))
        .strip()
        .lower()
    )
    if bootstrap not in {"ini", "explicit"}:
        raise ConfigError("adk.bootstrap must be ini or explicit")
    if bootstrap == "explicit" and any(ports[name] > 0 for name in AUXILIARY_INI_ONLY_PORT_KEYS):
        raise ConfigError(
            "The observed working repeater profile contains LP/TP/Self_Define listeners. "
            "Use adk.bootstrap=ini so ADKCore.ini owns all 14 listener sockets."
        )

    managed_ini = raw.getboolean(
        "adk", "managed_ini", fallback=raw.getboolean("adk", "patch_adkcore_ini", fallback=True)
    )
    detector_type = raw.getint("adk", "detector_type", fallback=2)
    if detector_type not in {2, 3}:
        raise ConfigError(
            "adk.detector_type must be 2 (repeater only) or 3 (vendor default USB+repeater)"
        )
    listener_ip = (
        _read_ipv4(raw, "adk", "listener_ip", allow_zero=True)
        if raw.has_option("adk", "listener_ip")
        else "0.0.0.0"
    )
    if listener_ip != "0.0.0.0":
        raise ConfigError(
            "adk.listener_ip must be 0.0.0.0 for this V4 gateway. "
            "Use network.local_ip only for the CPS Third Party Server IP and "
            "for explicit ConfigureRepeater diagnostics."
        )

    audio_mode = raw.get("audio", "mode", fallback="adk_audio").strip().lower()
    if audio_mode != "adk_audio":
        raise ConfigError(
            "This production package supports audio.mode=adk_audio only. "
            "Raw RTP needs a separately validated Hytera RTP codec implementation."
        )

    rrs_auto_ack = raw.getboolean("rrs", "auto_ack", fallback=True)
    rrs_valid_time = raw.getint("rrs", "registration_valid_time", fallback=60)
    if not 1 <= rrs_valid_time <= 86400:
        raise ConfigError("rrs.registration_valid_time must be 1..86400 seconds")

    server_host = raw.get("server", "host", fallback="127.0.0.1").strip()
    server_port = raw.getint("server", "port", fallback=18081)
    if not 1 <= server_port <= 65535:
        raise ConfigError("server.port must be 1..65535")
    watchdog_interval_seconds = _section_getfloat(raw, "server", "watchdog_interval_seconds", 2.0)
    thread_join_timeout_seconds = _section_getfloat(
        raw, "server", "thread_join_timeout_seconds", 3.0
    )
    event_history_size = _section_getint(raw, "server", "event_history_size", 500)
    error_history_size = _section_getint(raw, "server", "error_history_size", 100)
    if watchdog_interval_seconds < 0.2:
        raise ConfigError("server.watchdog_interval_seconds must be >= 0.2")
    if thread_join_timeout_seconds < 0.1:
        raise ConfigError("server.thread_join_timeout_seconds must be >= 0.1")
    if not 10 <= event_history_size <= 100000:
        raise ConfigError("server.event_history_size must be 10..100000")
    if not 10 <= error_history_size <= 10000:
        raise ConfigError("server.error_history_size must be 10..10000")

    protobuf_tools_max_request_bytes = _section_getint(
        raw, "protobuf_tools", "max_request_bytes", 1024 * 1024
    )
    protobuf_tools_private_target_id = _section_getint(
        raw, "protobuf_tools", "private_target_id", 5
    )
    protobuf_tools_send_timeout_seconds = _section_getfloat(
        raw, "protobuf_tools", "send_timeout_seconds", 15.0
    )
    if not 1 <= protobuf_tools_max_request_bytes <= 100 * 1024 * 1024:
        raise ConfigError("protobuf_tools.max_request_bytes must be 1..104857600")
    if not 1 <= protobuf_tools_private_target_id <= 16_777_215:
        raise ConfigError("protobuf_tools.private_target_id must be 1..16777215")
    if not 0.1 <= protobuf_tools_send_timeout_seconds <= 300.0:
        raise ConfigError("protobuf_tools.send_timeout_seconds must be 0.1..300")

    channel_mode = raw.get("channel", "mode", fallback="auto").strip().lower().replace("-", "_")
    if channel_mode not in {"auto", "default", "auto_then_default"}:
        raise ConfigError("channel.mode must be auto, default, or auto_then_default")
    default_channel_id = _optional_positive_int(raw, "channel", "default_channel_id")
    default_slot1_channel_id = _optional_positive_int(raw, "channel", "default_slot1_channel_id")
    default_slot2_channel_id = _optional_positive_int(raw, "channel", "default_slot2_channel_id")
    fallback_for_voice = _section_getboolean(raw, "channel", "fallback_for_voice", False)
    if channel_mode in {"default", "auto_then_default"} and default_channel_id is None:
        raise ConfigError(f"channel.mode={channel_mode} requires channel.default_channel_id")

    recordings_raw = raw.get("recording", "dir", fallback="../runtime/recordings")
    recordings_dir = _resolve_runtime(path, recordings_raw)
    wav_sample_rate = raw.getint("recording", "wav_sample_rate", fallback=8000)
    wav_channels = raw.getint("recording", "wav_channels", fallback=1)
    wav_sample_width = raw.getint("recording", "wav_sample_width", fallback=2)
    if not 8000 <= wav_sample_rate <= 48000:
        raise ConfigError("recording.wav_sample_rate must be 8000..48000")
    if wav_channels not in {1, 2}:
        raise ConfigError("recording.wav_channels must be 1 or 2")
    if wav_sample_width not in {1, 2, 3, 4}:
        raise ConfigError("recording.wav_sample_width must be 1, 2, 3 or 4 bytes")
    wav_assume_pcm = raw.getboolean("recording", "wav_assume_pcm", fallback=False)
    udp_recording_enabled = raw.getboolean("recording", "udp_recording_enabled", fallback=False)
    udp_recording_bind_ip = raw.get(
        "recording", "udp_recording_bind_ip", fallback="0.0.0.0"
    ).strip()
    try:
        ipaddress.IPv4Address(udp_recording_bind_ip)
    except ipaddress.AddressValueError as exc:
        raise ConfigError(
            f"recording.udp_recording_bind_ip must be IPv4, got {udp_recording_bind_ip!r}"
        ) from exc
    ports_raw = raw.get("recording", "udp_recording_ports", fallback="").strip()
    if ports_raw:
        try:
            udp_recording_ports = [
                int(item.strip()) for item in ports_raw.replace(";", ",").split(",") if item.strip()
            ]
        except ValueError as exc:
            raise ConfigError(
                "recording.udp_recording_ports must be a comma-separated port list"
            ) from exc
    else:
        udp_recording_ports = [ports["rtp_slot1"], ports["rtp_slot2"]]
    if any(not 1 <= port <= 65535 for port in udp_recording_ports):
        raise ConfigError("recording.udp_recording_ports contains an invalid UDP port")

    voice_capture_auto_start = _section_getboolean(raw, "voice_capture", "auto_start", True)
    voice_capture_iface = _optional_text(raw, "voice_capture", "iface")
    voice_capture_ports = _port_list(
        _optional_text(raw, "voice_capture", "ports", fallback=""),
        [ports["rtp_slot1"], ports["rtp_slot2"]],
    )
    voice_capture_src_ip = _optional_ipv4(raw, "voice_capture", "src_ip")
    voice_capture_dst_ip = _optional_ipv4(raw, "voice_capture", "dst_ip")
    voice_capture_host = _optional_ipv4(raw, "voice_capture", "host")
    voice_capture_strict_direction = _section_getboolean(
        raw, "voice_capture", "strict_direction", False
    )
    voice_capture_out_dir = _optional_text(raw, "voice_capture", "out_dir")
    voice_capture_tshark_path = _optional_text(raw, "voice_capture", "tshark_path")
    capture_root_raw = (
        _optional_text(raw, "voice_capture", "root_dir", fallback="../voice_capture")
        or "../voice_capture"
    )
    voice_capture_root = _resolve_runtime(path, capture_root_raw)
    send_root_raw = (
        _optional_text(raw, "voice_capture", "send_dir", fallback="../voice_capture/sent")
        or "../voice_capture/sent"
    )
    voice_send_root = _resolve_runtime(path, send_root_raw)

    # 兼容两种写法：
    #   [voice_callback] url=http://...
    #   [voice_callback] callback_url=http://...
    # 之前只读取 url；如果用户写 callback_url，会导致 enabled=true 但 callback_url=null，
    # 后台线程不会进入发送逻辑。
    voice_callback_url = _optional_url(raw, "voice_callback", "url")
    if voice_callback_url is None:
        voice_callback_url = _optional_url(raw, "voice_callback", "callback_url")
    voice_callback_enabled = _section_getboolean(
        raw, "voice_callback", "enabled", bool(voice_callback_url)
    )
    voice_callback_interval_seconds = _section_getfloat(
        raw, "voice_callback", "interval_seconds", 2.0
    )
    voice_callback_timeout_seconds = _section_getfloat(
        raw, "voice_callback", "timeout_seconds", 5.0
    )
    voice_callback_max_packets_per_tick = _section_getint(
        raw, "voice_callback", "max_packets_per_tick", 100
    )
    if voice_callback_interval_seconds < 0.2:
        raise ConfigError("voice_callback.interval_seconds must be >= 0.2")
    if voice_callback_timeout_seconds < 0.2:
        raise ConfigError("voice_callback.timeout_seconds must be >= 0.2")
    if not 1 <= voice_callback_max_packets_per_tick <= 10000:
        raise ConfigError("voice_callback.max_packets_per_tick must be 1..10000")
    voice_callback_merge_continuous_audio = _section_getboolean(
        raw, "voice_callback", "merge_continuous_audio", True
    )
    voice_callback_merge_gap_seconds = _section_getfloat(
        raw, "voice_callback", "merge_gap_seconds", 2.0
    )
    voice_callback_merge_max_packets = _section_getint(
        raw, "voice_callback", "merge_max_packets", 500
    )
    voice_callback_merge_max_audio_bytes = _section_getint(
        raw, "voice_callback", "merge_max_audio_bytes", 2 * 1024 * 1024
    )
    voice_callback_merge_flush_on_idle_tick = _section_getboolean(
        raw, "voice_callback", "merge_flush_on_idle_tick", True
    )
    voice_callback_merge_flush_on_batch_end = _section_getboolean(
        raw, "voice_callback", "merge_flush_on_batch_end", False
    )
    voice_callback_fallback_read_audio_raw = _section_getboolean(
        raw, "voice_callback", "fallback_read_audio_raw", True
    )
    if voice_callback_merge_gap_seconds < 0.1:
        raise ConfigError("voice_callback.merge_gap_seconds must be >= 0.1")
    if not 1 <= voice_callback_merge_max_packets <= 100000:
        raise ConfigError("voice_callback.merge_max_packets must be 1..100000")
    if not 1024 <= voice_callback_merge_max_audio_bytes <= 100 * 1024 * 1024:
        raise ConfigError("voice_callback.merge_max_audio_bytes must be 1024..104857600")

    return GatewayConfig(
        path=path,
        runtime_dir=runtime_dir,
        local_ip=local_ip,
        bootstrap=bootstrap,
        managed_ini=managed_ini,
        detector_type=detector_type,
        listener_ip=listener_ip,
        ports=ports,
        audio_mode=audio_mode,
        input_device=raw.getint("audio", "input_device", fallback=0),
        output_device=raw.getint("audio", "output_device", fallback=0),
        play_flag=raw.getint("audio", "play_flag", fallback=0),
        auto_channel=raw.getboolean("behaviour", "auto_channel", fallback=True),
        auto_attach_audio=raw.getboolean("behaviour", "auto_attach_audio", fallback=True),
        rrs_auto_ack=rrs_auto_ack,
        rrs_valid_time=rrs_valid_time,
        server_host=server_host,
        server_port=server_port,
        recordings_dir=recordings_dir,
        wav_sample_rate=wav_sample_rate,
        wav_channels=wav_channels,
        wav_sample_width=wav_sample_width,
        wav_assume_pcm=wav_assume_pcm,
        udp_recording_enabled=udp_recording_enabled,
        udp_recording_bind_ip=udp_recording_bind_ip,
        udp_recording_ports=udp_recording_ports,
        voice_capture_auto_start=voice_capture_auto_start,
        voice_capture_iface=voice_capture_iface,
        voice_capture_ports=voice_capture_ports,
        voice_capture_src_ip=voice_capture_src_ip,
        voice_capture_dst_ip=voice_capture_dst_ip,
        voice_capture_host=voice_capture_host,
        voice_capture_strict_direction=voice_capture_strict_direction,
        voice_capture_out_dir=voice_capture_out_dir,
        voice_capture_tshark_path=voice_capture_tshark_path,
        voice_callback_enabled=voice_callback_enabled,
        voice_callback_url=voice_callback_url,
        voice_callback_interval_seconds=voice_callback_interval_seconds,
        voice_callback_timeout_seconds=voice_callback_timeout_seconds,
        voice_callback_max_packets_per_tick=voice_callback_max_packets_per_tick,
        voice_callback_merge_continuous_audio=voice_callback_merge_continuous_audio,
        voice_callback_merge_gap_seconds=voice_callback_merge_gap_seconds,
        voice_callback_merge_max_packets=voice_callback_merge_max_packets,
        voice_callback_merge_max_audio_bytes=voice_callback_merge_max_audio_bytes,
        voice_callback_merge_flush_on_idle_tick=voice_callback_merge_flush_on_idle_tick,
        voice_callback_merge_flush_on_batch_end=voice_callback_merge_flush_on_batch_end,
        voice_callback_fallback_read_audio_raw=voice_callback_fallback_read_audio_raw,
        voice_capture_root=voice_capture_root,
        voice_send_root=voice_send_root,
        channel_mode=channel_mode,
        default_channel_id=default_channel_id,
        default_slot1_channel_id=default_slot1_channel_id,
        default_slot2_channel_id=default_slot2_channel_id,
        fallback_for_voice=fallback_for_voice,
        watchdog_interval_seconds=watchdog_interval_seconds,
        thread_join_timeout_seconds=thread_join_timeout_seconds,
        event_history_size=event_history_size,
        error_history_size=error_history_size,
        protobuf_tools_max_request_bytes=protobuf_tools_max_request_bytes,
        protobuf_tools_private_target_id=protobuf_tools_private_target_id,
        protobuf_tools_send_timeout_seconds=protobuf_tools_send_timeout_seconds,
    )
