# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/transport.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：.config
# 主要公开符号：RepeaterTransportError, UdpPreflight, RuntimeIniRepeaterSettings, host_executable_ini_path, render_host_executable_ini, render_runtime_ini, render_runtime_core_engine_demo_ini, host_executable_core_engine_demo_ini_path, render_host_executable_core_engine_demo_ini, read_runtime_repeater_settings
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Hytera V4 repeater listener configuration and UDP diagnostics.

ADKCore V4 derives ``ADKCore.ini`` from the host-process executable directory
(the directory containing ``python.exe`` under CPython), not from the DLL
directory. This module renders one controlled runtime template and stages an
exact mirror beside the active Python executable before ADK starts.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import GatewayConfig

REPEATER_INI_PORT_KEYS = (
    "RRS Port1",
    "RRS Port2",
    "LP Port1",
    "LP Port2",
    "TP Port1",
    "TP Port2",
    "TMP Port1",
    "TMP Port2",
    "RCP Port1",
    "RCP Port2",
    "RTP Port1",
    "RTP Port2",
    "RCP Analog Port",
    "RTP Analog Port",
    "E2E Port1",
    "E2E Port2",
    "DTP Port1",
    "Self_Define Data Port1",
    "Self_Define Data Port2",
    "OTAP Port1",
    "OTAP Port2",
    "OTAR Port1",
    "OTAR Port2",
)

# Field names exposed by both the V4 native header and the CLR RepeaterPortInfo.
# RepeaterPortInfo fields available in the explicit API flow. The complete
# golden profile below is rendered through ADKCore.ini in bootstrap=ini mode.
PORT_FIELD_MAP: dict[str, str] = {
    "rrs_slot1": "port_slot1_RRS",
    "rrs_slot2": "port_slot2_RRS",
    "rcp_slot1": "port_slot1_RCP",
    "rcp_slot2": "port_slot2_RCP",
    "tmp_slot1": "port_slot1_TMP",
    "tmp_slot2": "port_slot2_TMP",
    "rtp_slot1": "port_slot1_RTP",
    "rtp_slot2": "port_slot2_RTP",
}

# Mapping from user-facing gateway.ini keys to the vendor ADKCore.ini names.
INI_PORT_FROM_CONFIG: dict[str, str] = {
    "RRS Port1": "rrs_slot1",
    "RRS Port2": "rrs_slot2",
    "LP Port1": "lp_slot1",
    "LP Port2": "lp_slot2",
    "TP Port1": "tp_slot1",
    "TP Port2": "tp_slot2",
    "TMP Port1": "tmp_slot1",
    "TMP Port2": "tmp_slot2",
    "RCP Port1": "rcp_slot1",
    "RCP Port2": "rcp_slot2",
    "RTP Port1": "rtp_slot1",
    "RTP Port2": "rtp_slot2",
    "Self_Define Data Port1": "self_define_slot1",
    "Self_Define Data Port2": "self_define_slot2",
}


class RepeaterTransportError(RuntimeError):
    """The ADK repeater listener configuration cannot be prepared."""


@dataclass(frozen=True)
class UdpPreflight:
    bind_ip: str
    ports: tuple[int, ...]
    ok: bool
    failed_port: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class RuntimeIniRepeaterSettings:
    ini_path: Path
    detector_type: int | None
    listener_ip: str
    ports: dict[str, int]


def _read_ini_text(path: Path) -> str:
    for encoding in ("mbcs", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except LookupError:
            continue
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="latin-1", errors="replace")


def _parse_ini_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", ";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), {})
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip().lower()] = value.strip()
    return sections


def _replace_ini_values(text: str, section_name: str, values: dict[str, str]) -> str:
    """Replace or add keys while preserving vendor comments and unknown settings."""
    lines = text.splitlines(keepends=True)
    wanted = {key.lower(): (key, str(value)) for key, value in values.items()}
    section_lower = section_name.lower()
    in_target = False
    found_section = False
    consumed: set[str] = set()
    output: list[str] = []

    def append_missing() -> None:
        for low, (original, value) in wanted.items():
            if low not in consumed:
                output.append(f"{original}={value}\r\n")
                consumed.add(low)

    for raw in lines:
        stripped = raw.strip()
        is_section = stripped.startswith("[") and stripped.endswith("]")
        if is_section:
            if in_target:
                append_missing()
            in_target = stripped[1:-1].strip().lower() == section_lower
            found_section = found_section or in_target
            output.append(raw)
            continue
        if in_target and "=" in raw and not stripped.startswith(("//", ";", "#")):
            key, _value = raw.split("=", 1)
            low = key.strip().lower()
            if low in wanted:
                original, value = wanted[low]
                ending = "\r\n" if raw.endswith("\r\n") else "\n" if raw.endswith("\n") else ""
                output.append(f"{original}={value}{ending}")
                consumed.add(low)
                continue
        output.append(raw)
    if in_target:
        append_missing()
    if not found_section:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += "\r\n"
        output.append(f"\r\n[{section_name}]\r\n")
        append_missing()
    return "".join(output)


def _template_path(cfg: GatewayConfig) -> Path:
    vendor = cfg.runtime_dir / "ADKCore.vendor.ini"
    return vendor if vendor.is_file() else cfg.runtime_dir / "ADKCore.ini"


def host_executable_ini_path() -> Path:
    """Return the ADKCore.ini path selected by the native V4 DLL.

    ADKCore.dll calls GetModuleFileNameW(NULL) and appends ``ADKCore.ini``.
    Under CPython, NULL resolves to python.exe, not the directory containing
    ADKCore.dll. Therefore the native configuration belongs beside
    ``sys.executable``.
    """
    return Path(sys.executable).resolve().parent / "ADKCore.ini"


def render_host_executable_ini(cfg: GatewayConfig) -> Path:
    """Mirror the exact generated runtime INI beside python.exe.

    The vendor DLL loads configuration from the host executable directory. A
    stale ADKCore.ini beside Python can silently reactivate default ports such
    as OTAP 30050 even when runtime/ADK/ADKCore.ini is correct.
    """
    source = cfg.runtime_dir / "ADKCore.ini"
    if not source.is_file():
        raise RepeaterTransportError(f"Runtime ADKCore.ini was not found: {source}")
    target = host_executable_ini_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = target.with_name("ADKCore.ini.before_hytera_gateway.bak")
            if not backup.exists():
                backup.write_bytes(target.read_bytes())
        temporary = target.with_suffix(".ini.gateway.tmp")
        temporary.write_bytes(source.read_bytes())
        os.replace(temporary, target)
    except OSError as exc:
        raise RepeaterTransportError(
            "Cannot stage the native ADKCore.ini beside the active Python executable. "
            f"Required path: {target}. Run from a writable per-user 32-bit Python installation "
            "or grant write permission to that directory."
        ) from exc
    return target


def render_runtime_ini(cfg: GatewayConfig) -> Path:
    """Render the project-owned listener settings into ADKCore.ini atomically."""
    target = cfg.runtime_dir / "ADKCore.ini"
    template = _template_path(cfg)
    if not template.is_file():
        raise RepeaterTransportError(f"ADKCore.ini template not found: {template}")
    source = _read_ini_text(template)
    source = _replace_ini_values(
        source,
        "Detector",
        {
            "Type": str(cfg.detector_type),
            "IsEnableDataRepeater": "1",
        },
    )
    # The V4 reference flow is Initialize -> ConfigureRepeater -> Register.
    # When that documented explicit API path is used, disable every INI-created
    # listener. This prevents ADKCore from opening unrelated template ports such
    # as OTAP 30050 before ConfigureRepeater receives the PC's native IPv4 value.
    if cfg.bootstrap == "explicit":
        repeater_values = {"IP": "0.0.0.0"}
        repeater_values.update({key: "0" for key in REPEATER_INI_PORT_KEYS})
    else:
        # Golden-profile INI path: the successful vendor demo exchanges
        # ADK traffic on RRS, LP, TP, TMP, RCP, RTP and Self_Define listeners.
        # Disable only services absent from that observed profile (DTP, E2E,
        # OTAP, OTAR and analog ports) so OTAP 30050 remains closed.
        repeater_values = {"IP": cfg.listener_ip}
        repeater_values.update({key: "0" for key in REPEATER_INI_PORT_KEYS})
        repeater_values.update(
            {ini_key: str(cfg.ports[cfg_key]) for ini_key, cfg_key in INI_PORT_FROM_CONFIG.items()}
        )
    source = _replace_ini_values(source, "Repeater Setting", repeater_values)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name("ADKCore.ini.before_gateway.bak")
        if not backup.exists():
            backup.write_text(_read_ini_text(target), encoding="utf-8", newline="")
    temporary = target.with_suffix(".ini.tmp")
    temporary.write_text(source, encoding="utf-8", newline="")
    os.replace(temporary, target)
    return target


# --------------------------------------------------------------------------
# CoreEngineDemo.ini staging
# --------------------------------------------------------------------------
#
# The vendor V5 reference build (CORE_V05.0.02.008) records the repeater's
# provisioning state in a separate ``CoreEngineDemo.ini`` alongside
# ``ADKCore.ini``. Its ``[repeater setting]`` section uses a signed int32 IP
# encoding (big-endian byte view of the dotted quad) and a ``Configured=1``
# flag. When absent, several V5 builds skip the ``ProcessChannelChanged``
# invocation that transitions repeater UDP listeners into ``standby for data
# communication`` state, and no data channel is ever observed. Staging a
# minimal, gateway-owned copy of this file removes that pre-init gap without
# changing the actual bind ports (those remain owned by ADKCore.ini).

# Section name and key names use the *exact* casing observed in the reference
# CoreEngineDemo.ini file so string-compare code inside the vendor DLL matches.
CORE_ENGINE_DEMO_INI_NAME = "CoreEngineDemo.ini"
CORE_ENGINE_DEMO_SECTION = "repeater setting"

# Mapping from CoreEngineDemo.ini port keys to GatewayConfig.ports keys.
# The vendor demo abbreviates "Self_Define Data" as "SD" and keeps the analog
# ports as scalar entries; both slots share a single RCP/RTP analog key.
CORE_ENGINE_DEMO_PORT_MAP: tuple[tuple[str, str | None], ...] = (
    ("RCP Port1", "rcp_slot1"),
    ("TMP Port1", "tmp_slot1"),
    ("LP Port1", "lp_slot1"),
    ("RRS Port1", "rrs_slot1"),
    ("RTP Port1", "rtp_slot1"),
    ("RCP Port2", "rcp_slot2"),
    ("TMP Port2", "tmp_slot2"),
    ("LP Port2", "lp_slot2"),
    ("RRS Port2", "rrs_slot2"),
    ("RTP Port2", "rtp_slot2"),
    ("TP Port1", "tp_slot1"),
    ("TP Port2", "tp_slot2"),
    ("SD Port1", "self_define_slot1"),
    ("SD Port2", "self_define_slot2"),
    # Analog ports are not part of the golden profile in this deployment;
    # emit 0 so the vendor app treats them as "no analog listener".
    ("RCP Analog Port", None),
    ("RTP Analog Port", None),
)


def _core_engine_demo_body(cfg: GatewayConfig) -> str:
    """Return the exact CoreEngineDemo.ini body that mirrors the vendor demo."""
    # The reference IP field belongs to the PC that runs the demo. Using
    # ``network.local_ip`` (which the gateway already validates as the wired
    # IPv4 also configured as CPS Third Party Server IP) keeps this consistent.
    ip_signed = core_engine_demo_ini_ip(cfg.local_ip)
    lines: list[str] = [f"[{CORE_ENGINE_DEMO_SECTION}]", f"IP={ip_signed}"]
    for ini_key, cfg_key in CORE_ENGINE_DEMO_PORT_MAP:
        port = 0 if cfg_key is None else int(cfg.ports.get(cfg_key, 0) or 0)
        lines.append(f"{ini_key}={port}")
    # ``Configured=1`` is the flag the V5 vendor build checks before it emits
    # ``ProcessChannelChanged End`` and transitions listeners to standby.
    lines.append("Configured=1")
    return "\r\n".join(lines) + "\r\n"


def _write_core_engine_demo_ini(target: Path, body: str) -> Path:
    """Atomically write ``CoreEngineDemo.ini`` and back the previous file up."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            existing = _read_ini_text(target)
        except OSError:
            existing = ""
        if existing == body:
            return target
        backup = target.with_name(target.name + ".before_gateway.bak")
        if not backup.exists():
            try:
                backup.write_text(existing, encoding="utf-8", newline="")
            except OSError:
                pass
    temporary = target.with_suffix(target.suffix + ".gateway.tmp")
    temporary.write_text(body, encoding="utf-8", newline="")
    os.replace(temporary, target)
    return target


def render_runtime_core_engine_demo_ini(cfg: GatewayConfig) -> Path:
    """Render ``CoreEngineDemo.ini`` beside ``ADKCore.ini`` in the runtime dir."""
    target = cfg.runtime_dir / CORE_ENGINE_DEMO_INI_NAME
    return _write_core_engine_demo_ini(target, _core_engine_demo_body(cfg))


def host_executable_core_engine_demo_ini_path() -> Path:
    """Return the CoreEngineDemo.ini path the V5 vendor DLL resolves.

    Same resolution rule as :func:`host_executable_ini_path`: the native V5
    build reads INI files relative to the host process executable
    (``python.exe`` under CPython), not the DLL directory.
    """
    return Path(sys.executable).resolve().parent / CORE_ENGINE_DEMO_INI_NAME


def render_host_executable_core_engine_demo_ini(cfg: GatewayConfig) -> Path:
    """Mirror the runtime CoreEngineDemo.ini beside ``python.exe``.

    Without this file a stale host-level demo INI can silently keep
    ``Configured=0``, which prevents the V5 DLL from advancing repeater UDP
    listeners into ``standby for data communication`` state.
    """
    target = host_executable_core_engine_demo_ini_path()
    try:
        return _write_core_engine_demo_ini(target, _core_engine_demo_body(cfg))
    except OSError as exc:
        raise RepeaterTransportError(
            "Cannot stage CoreEngineDemo.ini beside the active Python executable. "
            f"Required path: {target}. Run from a writable per-user 32-bit Python "
            "installation or grant write permission to that directory."
        ) from exc


def read_runtime_repeater_settings(cfg: GatewayConfig) -> RuntimeIniRepeaterSettings:
    ini = cfg.runtime_dir / "ADKCore.ini"
    if not ini.is_file():
        raise RepeaterTransportError(f"ADKCore.ini was not found: {ini}")
    parsed = _parse_ini_sections(_read_ini_text(ini))
    detector = parsed.get("detector", {})
    repeater = parsed.get("repeater setting", {})
    if not repeater:
        raise RepeaterTransportError(f"[Repeater Setting] is missing in {ini}")
    try:
        detector_type = int(detector.get("type", ""))
    except ValueError:
        detector_type = None
    listener_ip = repeater.get("ip", "").strip()
    try:
        ipaddress.IPv4Address(listener_ip)
    except ipaddress.AddressValueError as exc:
        raise RepeaterTransportError(
            f"Invalid [Repeater Setting] IP={listener_ip!r} in {ini}"
        ) from exc
    ports: dict[str, int] = {}
    for key in REPEATER_INI_PORT_KEYS:
        raw = repeater.get(key.lower(), "0")
        try:
            port = int(raw)
        except ValueError as exc:
            raise RepeaterTransportError(f"Invalid {key}={raw!r} in {ini}") from exc
        if not 0 <= port <= 65535:
            raise RepeaterTransportError(f"Out-of-range {key}={port} in {ini}")
        ports[key] = port
    return RuntimeIniRepeaterSettings(ini, detector_type, listener_ip, ports)


def native_ipv4_uint32(ip: str) -> int:
    """Return the ``uint`` representation required by V4 ``ConfigureRepeater``.

    ``ADKFramework.ConfigureRepeater`` takes a managed ``System.UInt32`` rather
    than a byte array.  In the V4 x86 CLR/native bridge, passing the host-memory
    value (for example ``0x2E40A8C0`` for ``192.168.64.46``) is byte-swapped by
    the native listener path and makes Winsock bind ``46.64.168.192``.

    The correct CLR value is the dotted-decimal/network-order integer
    ``0xC0A8402E``.  The native bridge then produces the expected in-memory
    ``in_addr`` bytes ``C0 A8 40 2E`` before calling ``bind``.
    """
    return int(ipaddress.IPv4Address(ip))


def core_engine_demo_ini_ip(ip: str) -> int:
    """Return the signed int32 encoding used by the reference CoreEngineDemo.ini.

    The vendor V5 CoreEngineDemo stores ``[repeater setting] IP`` as a signed
    32-bit integer whose big-endian byte view equals the dotted-quad IPv4.
    Example from the reference capture: ``IP=-1062726120`` decodes to
    ``0xC0A81618`` big-endian = ``192.168.22.24``. Windows two's-complement
    signed interpretation is required because dotted-quads with a high octet
    >= 0x80 (i.e. any 128.x.x.x or above) overflow ``int``.
    """
    unsigned = int(ipaddress.IPv4Address(ip))
    return unsigned - 0x1_0000_0000 if unsigned >= 0x8000_0000 else unsigned


def native_ipv4_diagnostic(ip: str) -> dict[str, str | int]:
    """Show both the CLR input and the address reached by the V4 native bridge."""
    packed = socket.inet_aton(ip)
    value = native_ipv4_uint32(ip)
    native_value = socket.htonl(value)
    return {
        "ipv4": ip,
        "network_bytes": packed.hex(" ").upper(),
        "configure_repeater_uint32_decimal": value,
        "configure_repeater_uint32_hex": f"0x{value:08X}",
        "native_bridge_uint32_hex": f"0x{native_value:08X}",
        "expected_bind_ipv4": socket.inet_ntoa(struct.pack("<I", native_value)),
    }


def udp_bind_preflight(
    bind_ip: str, ports: Iterable[int], *, allow_wildcard: bool = False
) -> UdpPreflight:
    try:
        ipaddress.IPv4Address(bind_ip)
    except ipaddress.AddressValueError as exc:
        return UdpPreflight(bind_ip, (), False, error=f"invalid IPv4: {exc}")
    if bind_ip == "0.0.0.0" and not allow_wildcard:
        return UdpPreflight(bind_ip, (), False, error="wildcard bind not allowed")
    unique_ports = tuple(sorted({int(port) for port in ports if int(port) > 0}))
    opened: list[socket.socket] = []
    try:
        for port in unique_ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((bind_ip, port))
            except OSError as exc:
                sock.close()
                return UdpPreflight(
                    bind_ip, unique_ports, False, port, f"{type(exc).__name__}: {exc}"
                )
            opened.append(sock)
        return UdpPreflight(bind_ip, unique_ports, True)
    finally:
        for sock in opened:
            try:
                sock.close()
            except OSError:
                pass


def _create_port_info(types: dict[str, Any], cfg: GatewayConfig) -> Any:
    info = types["RepeaterPortInfo"]()
    try:
        field_names = [str(field.Name) for field in info.GetType().GetFields()]
    except Exception:
        field_names = list(PORT_FIELD_MAP.values())
    for field in field_names:
        try:
            setattr(info, field, types["UInt16"](0))
        except Exception:
            continue
    for key, field in PORT_FIELD_MAP.items():
        if field in field_names:
            setattr(info, field, types["UInt16"](cfg.ports[key]))
    return info


def configure_repeater(adk: Any, types: dict[str, Any], cfg: GatewayConfig) -> None:
    adk.ConfigureRepeater(
        types["UInt32"](native_ipv4_uint32(cfg.local_ip)), _create_port_info(types, cfg)
    )
