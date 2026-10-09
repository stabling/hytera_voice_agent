# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/runtime.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..transport, .native_output, .shared
# 主要公开符号：RuntimeMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import os
import platform
import socket
import struct
import sys
from pathlib import Path
from typing import Any

from ..transport import (
    INI_PORT_FROM_CONFIG,
)
from .native_output import suppress_hstrp_console_output
from .shared import AdkRuntimeError


class RuntimeMixin:
    """内部能力分组；由 ``HyteraAdkBackend`` 组合使用。"""

    def _validate(self) -> None:
        if platform.system() != "Windows":
            raise AdkRuntimeError("Hytera ADK V4 runtime is Windows-only")
        if struct.calcsize("P") * 8 != 32:
            raise AdkRuntimeError("This ADK V4 package is PE32/x86; start it with 32-bit CPython.")
        missing = [name for name in self.REQUIRED if not (self.cfg.runtime_dir / name).is_file()]
        if missing:
            raise AdkRuntimeError(f"Missing ADK runtime files: {', '.join(missing)}")
        if not self._local_ip_exists(self.cfg.local_ip):
            raise AdkRuntimeError(
                f"network.local_ip={self.cfg.local_ip} is not currently assigned to this PC. "
                "Set it to the wired Ethernet IPv4 also configured as CPS Third Party Server IP."
            )

    def _validate_runtime_ini(self, settings: Any) -> None:
        if settings.detector_type not in {2, 3}:
            raise AdkRuntimeError(
                f"ADKCore.ini Detector.Type must be 2 or 3, got {settings.detector_type!r}."
            )
        if self.cfg.bootstrap == "explicit":
            if settings.listener_ip != "0.0.0.0":
                raise AdkRuntimeError(
                    f"Explicit mode requires ADKCore.ini [Repeater Setting] IP=0.0.0.0, got {settings.listener_ip}."
                )
            active_ini_ports = {key: port for key, port in settings.ports.items() if port > 0}
            if active_ini_ports:
                raise AdkRuntimeError(
                    "Explicit mode requires all ADKCore.ini repeater ports to be 0 so only "
                    "ConfigureRepeater creates sockets. Active ports: " + str(active_ini_ports)
                )
            return

        if settings.listener_ip != "0.0.0.0":
            raise AdkRuntimeError(
                "INI mode requires ADKCore.ini [Repeater Setting] IP=0.0.0.0 to avoid "
                f"the vendor reversed-IP bind path, got {settings.listener_ip}."
            )
        # The working vendor-demo trace establishes the complete INI-owned
        # listener profile: RRS, LP, TP, TMP, RCP, RTP and Self_Define Data,
        # each with one listener per TDMA slot. Keep validation aligned with
        # the complete listener set rendered by the current configuration.
        # eight-port rule and rejected its own generated configuration.
        # Derive the allowed INI set from the single transport mapping so
        # rendering, validation and diagnostics cannot drift apart again.
        expected = {
            ini_key: self.cfg.ports[cfg_key] for ini_key, cfg_key in INI_PORT_FROM_CONFIG.items()
        }
        mismatched = [
            f"{key}: ini={settings.ports.get(key)} cfg={value}"
            for key, value in expected.items()
            if settings.ports.get(key) != value
        ]
        if mismatched:
            raise AdkRuntimeError(
                "ADKCore.ini and gateway.ini disagree for the managed INI listener profile: "
                + "; ".join(mismatched)
            )
        unexpected = {
            key: port for key, port in settings.ports.items() if key not in expected and port > 0
        }
        if unexpected:
            raise AdkRuntimeError(
                "INI mode permits only the configured RRS/LP/TP/TMP/RCP/RTP/"
                "Self_Define listeners. Disable every other repeater listener "
                "(including analog, E2E, DTP, OTAP and OTAR). Unexpected active ports: "
                + str(unexpected)
            )

    @staticmethod
    def _local_ip_exists(ip: str) -> bool:
        if ip in {"127.0.0.1", "0.0.0.0"}:
            return ip == "127.0.0.1"
        addresses: set[str] = set()
        try:
            addresses.update(
                info[4][0]
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            )
        except OSError:
            pass
        try:
            # This does not transmit; it discovers the preferred IPv4 route.
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            addresses.add(probe.getsockname()[0])
            probe.close()
        except OSError:
            pass
        return ip in addresses

    def _load_types(self) -> None:
        # HSTRP is a native vendor component and bypasses Python logging.
        # Redirect its stdout/stderr before pythonnet loads the ADK DLLs.
        suppress_hstrp_console_output()
        runtime = self.cfg.runtime_dir
        if hasattr(os, "add_dll_directory"):
            self._dll_cookie = os.add_dll_directory(str(runtime))
        if str(runtime) not in sys.path:
            sys.path.insert(0, str(runtime))
        self._cwd = os.getcwd()
        os.chdir(runtime)
        try:
            os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
            from pythonnet import load

            load("netfx")
            import clr

            clr.AddReference(str(runtime / "ADKCoreEngine_CLR.dll"))
            from System import Array, BitConverter, Byte, Char, String, UInt16, UInt32, Enum
            from ADKCoreEngine_CLR import (
                ADKEventHandler,
                ADKFramework,
                ADKRTPDataReceived,
                ButtonKeyboardOperationRequest,
                ButtonOperation,
                Buttons,
                CallRequest,
                CallType,
                OptionCode,
                OnlineAnswerRequest,
                RadioOnlineResult,
                RepeaterPortInfo,
                ServiceType,
                TextMessageRequest,
            )
        except Exception as exc:
            raise AdkRuntimeError(
                "Cannot load the V4 CLR runtime. Verify .NET Framework 4.8, Visual C++ x86 runtime, "
                "32-bit CPython, pythonnet, and that the three V4 DLLs stay in one directory."
            ) from exc
        self._types = {
            "UInt16": UInt16,
            "UInt32": UInt32,
            "Enum": Enum,
            "ByteArray": lambda value: Array[Byte](value),
            "CharArray": lambda length: Array.CreateInstance(Char, length),
            "BitConverter": BitConverter,
            "String": String,
            "ADKFramework": ADKFramework,
            "ADKEventHandler": ADKEventHandler,
            "ADKRTPDataReceived": ADKRTPDataReceived,
            "OptionCode": OptionCode,
            "OnlineAnswerRequest": OnlineAnswerRequest,
            "RadioOnlineResult": RadioOnlineResult,
            "RepeaterPortInfo": RepeaterPortInfo,
            "ServiceType": ServiceType,
            "TextMessageRequest": TextMessageRequest,
            "CallRequest": CallRequest,
            "CallType": CallType,
            "ButtonKeyboardOperationRequest": ButtonKeyboardOperationRequest,
            "Buttons": Buttons,
            "ButtonOperation": ButtonOperation,
        }
        try:
            self._option_names = {
                int(value): self._enum_name(value) for value in Enum.GetValues(OptionCode)
            }
        except Exception:
            # The normal string conversion remains a compatible fallback for
            # older pythonnet builds that cannot iterate this enum.
            self._option_names = {}

    @staticmethod
    def _log_dir() -> Path:
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ADKLog"

    def _log_files(self) -> set[Path]:
        base = self._log_dir()
        return {path for path in base.rglob("*") if path.is_file()} if base.is_dir() else set()

    def _adk_log_tail(self, lines: int = 120) -> str:
        base = self._log_dir()
        if not base.is_dir():
            return f"Vendor ADK log directory was not found: {base}"
        files = [path for path in base.rglob("*") if path.is_file()]
        if not files:
            return f"Vendor ADK log directory is empty: {base}"
        changed = [path for path in files if path not in self._log_snapshot]
        latest = max(changed or files, key=lambda path: path.stat().st_mtime)
        text = ""
        for encoding in ("mbcs", "utf-8", "latin-1"):
            try:
                text = latest.read_text(encoding=encoding, errors="replace")
                break
            except LookupError:
                continue
        return f"Latest vendor ADK log: {latest}\n" + "\n".join(text.splitlines()[-lines:])
