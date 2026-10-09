# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/diagnostics.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..config, ..transport, .backend
# 主要公开符号：doctor
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import platform
import struct

from ..config import GatewayConfig
from ..transport import (
    host_executable_core_engine_demo_ini_path,
    host_executable_ini_path,
    native_ipv4_diagnostic,
    read_runtime_repeater_settings,
    render_host_executable_core_engine_demo_ini,
    render_host_executable_ini,
    render_runtime_core_engine_demo_ini,
    render_runtime_ini,
    udp_bind_preflight,
)


from .backend import HyteraAdkBackend


def doctor(cfg: GatewayConfig) -> list[str]:
    channel_mode = getattr(cfg, "channel_mode", "auto")
    default_channel_id = getattr(cfg, "default_channel_id", None)
    default_slot1_channel_id = getattr(cfg, "default_slot1_channel_id", None)
    default_slot2_channel_id = getattr(cfg, "default_slot2_channel_id", None)
    fallback_for_voice = bool(getattr(cfg, "fallback_for_voice", False))
    lines = [
        f"Config: {cfg.path}",
        f"Runtime: {cfg.runtime_dir}",
        f"Python bitness: {struct.calcsize('P') * 8}",
        f"Platform: {platform.platform()}",
        f"PC IPv4 / CPS Third Party Server IP: {cfg.local_ip}",
        f"Bootstrap: {cfg.bootstrap}",
        f"Managed ADKCore.ini: {cfg.managed_ini}",
        f"Channel mode: {channel_mode}",
        "Configured default ChannelID: "
        + ("none" if default_channel_id is None else f"0x{default_channel_id:08X}"),
        "Configured default slot 1 ChannelID: "
        + ("none" if default_slot1_channel_id is None else f"0x{default_slot1_channel_id:08X}"),
        "Configured default slot 2 ChannelID: "
        + ("none" if default_slot2_channel_id is None else f"0x{default_slot2_channel_id:08X}"),
        f"Default ChannelID permitted for voice/RTP: {fallback_for_voice}",
    ]
    for name in HyteraAdkBackend.REQUIRED:
        lines.append(f"{'OK' if (cfg.runtime_dir / name).is_file() else 'MISSING'}  {name}")
    native_ini = host_executable_ini_path()
    native_demo_ini = host_executable_core_engine_demo_ini_path()
    lines.append(f"Native ADKCore.ini path (beside python.exe): {native_ini}")
    lines.append(f"Native CoreEngineDemo.ini path (beside python.exe): {native_demo_ini}")
    try:
        if cfg.managed_ini:
            render_runtime_ini(cfg)
            render_host_executable_ini(cfg)
            runtime_demo_ini = render_runtime_core_engine_demo_ini(cfg)
            render_host_executable_core_engine_demo_ini(cfg)
            lines.append(f"Runtime CoreEngineDemo.ini: {runtime_demo_ini}")
        settings = read_runtime_repeater_settings(cfg)
        lines.append(f"Runtime ADKCore.ini listener IP: {settings.listener_ip}")
        lines.append(f"Runtime ADKCore.ini detector Type: {settings.detector_type}")
        lines.append(f"Native host ADKCore.ini: {'OK' if native_ini.is_file() else 'MISSING'}")
        lines.append(
            f"Native host CoreEngineDemo.ini: {'OK' if native_demo_ini.is_file() else 'MISSING'}"
        )
        for name, port in settings.ports.items():
            lines.append(f"ADKCore.ini {name}: {port}")
        if cfg.bootstrap == "explicit":
            conversion = native_ipv4_diagnostic(cfg.local_ip)
            lines.append("ConfigureRepeater local IP conversion: " + str(conversion))
            preflight_ip = cfg.local_ip
            preflight_ports = cfg.ports.values()
            label = "ConfigureRepeater required-port UDP preflight"
        else:
            preflight_ip = settings.listener_ip
            preflight_ports = settings.ports.values()
            label = "ADKCore.ini all-listener UDP preflight"
        preflight = udp_bind_preflight(
            preflight_ip,
            preflight_ports,
            allow_wildcard=preflight_ip == "0.0.0.0",
        )
        lines.append(
            label
            + ": "
            + ("OK" if preflight.ok else f"FAILED port={preflight.failed_port}; {preflight.error}")
        )
    except Exception as exc:
        lines.append(f"ADKCore.ini inspection: FAILED: {exc}")
    lines.append(
        "Native V4 configuration is staged beside python.exe; runtime/ADK/ADKCore.ini alone is not sufficient."
    )
    lines.append(
        "CPS Third Party Server IP must equal the PC IPv4 above. It is not the repeater IP."
    )
    return lines
