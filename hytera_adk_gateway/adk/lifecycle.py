# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/lifecycle.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：..audio, ..config, ..models, ..transport, .shared
# 主要公开符号：LifecycleMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import os
from collections import deque
import queue
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..audio import AdkAudioPipeline
from ..config import GatewayConfig
from ..models import AdkEvent, VoiceSession
from ..transport import (
    configure_repeater,
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
from .shared import AdkRuntimeError

if TYPE_CHECKING:
    from .backend import HyteraAdkBackend


class LifecycleMixin:
    """内部能力分组；由 ``HyteraAdkBackend`` 组合使用。"""

    def __init__(self, cfg: GatewayConfig, *, debug_fields: bool = False) -> None:
        self.cfg = cfg
        self.debug_fields = debug_fields
        self._events: queue.Queue[AdkEvent] = queue.Queue()
        self._started = False
        self._adk: Any = None
        self._types: dict[str, Any] = {}
        self._services: dict[str, Any] = {}
        self._callbacks: dict[str, Any] = {}
        self._dll_cookie: Any = None
        self._cwd: str | None = None
        self._log_snapshot: set[Path] = set()
        self.channels: dict[int, dict[str, Any]] = {}
        # Radio ID -> ADK channel ID. Updated by SYSTEM route reports and, when
        # the CLR exposes source/channel fields, by an RRS registration fallback.
        self.radio_routes: dict[int, int] = {}
        self.event_counts: dict[str, int] = {}
        self.last_event: AdkEvent | None = None
        # Retain a bounded local history so field failures can be diagnosed after
        # the callback has drained. This deliberately stores only decoded event
        # metadata, never raw packet content beyond TMP text already received.
        self._event_history: deque[AdkEvent] = deque(maxlen=200)
        self._option_names: dict[int, str] = {}
        self.selected_channel: int | None = None
        # ``adk_auto`` means a real SYSTEM/RRS/TMP/RCP callback selected this ID.
        # ``manual_runtime`` and ``configured_default`` are explicit operator choices.
        # The source is intentionally tracked separately so a configured fallback is
        # never presented as an ADK-discovered, online channel.
        self.selected_channel_source: str | None = None
        self._channel_mode_override: str | None = None
        self.voice: VoiceSession | None = None
        self.audio: AdkAudioPipeline | None = None
        # RTP/media byte capture is no longer implemented inside the ADK backend.
        # The HTTP layer reads live capture output produced by TShark/Wireshark helpers.
        self.voice_capture_root = Path(
            getattr(cfg, "voice_capture_root", None) or (Path.cwd() / "voice_capture")
        )
        self.voice_send_root = Path(
            getattr(cfg, "voice_send_root", None) or (self.voice_capture_root / "sent")
        )

    @property
    def is_started(self) -> bool:
        """返回原生 ADK 后端是否已完成启动。"""
        return self._started

    def __enter__(self) -> "HyteraAdkBackend":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def next_event(self, timeout: float | None = None) -> AdkEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def start(self) -> None:
        if self._started:
            return
        self._validate()
        if self.cfg.managed_ini:
            render_runtime_ini(self.cfg)
        settings = read_runtime_repeater_settings(self.cfg)
        self._validate_runtime_ini(settings)
        # ADKCore V4 resolves ADKCore.ini beside python.exe (the host process),
        # not beside the native DLL. Mirror the generated runtime file before
        # CLR loading so no stale host-level default can reactivate OTAP 30050.
        host_ini = (
            render_host_executable_ini(self.cfg)
            if self.cfg.managed_ini
            else host_executable_ini_path()
        )
        if not host_ini.is_file():
            raise AdkRuntimeError(
                f"Native ADKCore.ini is missing: {host_ini}. "
                "ADKCore V4 reads the configuration beside python.exe."
            )
        # V5 vendor builds also read ``CoreEngineDemo.ini`` (both beside the
        # DLL and beside python.exe) to obtain the ``Configured=1`` flag that
        # transitions repeater UDP listeners into "standby for data
        # communication" state. Stage a gateway-owned copy in both locations
        # so this pre-init handshake never blocks first-packet arrival.
        if self.cfg.managed_ini:
            runtime_demo_ini = render_runtime_core_engine_demo_ini(self.cfg)
            host_demo_ini = render_host_executable_core_engine_demo_ini(self.cfg)
        else:
            runtime_demo_ini = self.cfg.runtime_dir / "CoreEngineDemo.ini"
            host_demo_ini = host_executable_core_engine_demo_ini_path()
        # In INI compatibility mode ADKCore itself owns listener creation from
        # the generated ADKCore.ini. In explicit mode ConfigureRepeater owns the
        # eight enabled sockets. Both modes preflight only active RRS/TMP/RCP/RTP
        # ports; inactive vendor-template ports such as OTAP 30050 are not used.
        preflight_ip = (
            self.cfg.local_ip if self.cfg.bootstrap == "explicit" else settings.listener_ip
        )
        preflight_ports = (
            self.cfg.ports.values() if self.cfg.bootstrap == "explicit" else settings.ports.values()
        )
        preflight = udp_bind_preflight(
            preflight_ip,
            preflight_ports,
            allow_wildcard=preflight_ip == "0.0.0.0",
        )
        if not preflight.ok:
            raise AdkRuntimeError(
                "ADK UDP preflight failed before CLR initialization: "
                f"ini={settings.ini_path}; bind_ip={preflight_ip}; "
                f"port={preflight.failed_port}; {preflight.error}. "
                "Close the owning process or choose unused CPS ports."
            )

        self._log_snapshot = self._log_files()
        self._load_types()
        try:
            self._adk = self._types["ADKFramework"]()
            self._adk.Initialize()
            if self.cfg.bootstrap == "explicit":
                try:
                    configure_repeater(self._adk, self._types, self.cfg)
                except Exception as exc:
                    raise AdkRuntimeError(
                        "ADKFramework.ConfigureRepeater failed. The V4 API requires PC local_ip and all "
                        "enabled CPS repeater ports to match exactly.\n" + self._adk_log_tail()
                    ) from exc

            self._emit(
                "SYSTEM",
                "transport_ready",
                "ADK_REPEATER_READY",
                metadata={
                    "bootstrap": self.cfg.bootstrap,
                    "runtime_ini": str(settings.ini_path),
                    "native_host_ini": str(host_ini),
                    "runtime_core_engine_demo_ini": str(runtime_demo_ini),
                    "native_host_core_engine_demo_ini": str(host_demo_ini),
                    "detector_type": settings.detector_type,
                    "listener_ip": settings.listener_ip,
                    "third_party_server_ip": self.cfg.local_ip,
                    "configure_repeater_ip": native_ipv4_diagnostic(self.cfg.local_ip)
                    if self.cfg.bootstrap == "explicit"
                    else None,
                    "enabled_ports": dict(self.cfg.ports)
                    if self.cfg.bootstrap == "explicit"
                    else {name: port for name, port in settings.ports.items() if port > 0},
                },
            )

            # Same registration order as the vendor C# appendix. The INI mode is
            # deliberately preferred because some V4 CLR builds return
            # SYSTEM_ERROR_CONFIGE_REPEATER_ERROR at SYSTEM.Register after an
            # explicit ConfigureRepeater call, even though RRS/TMP/RCP registered.
            self._register("RRS", self._service_enum("RRS"), self._on_rrs)
            self._register("TMP", self._service_enum("TMP"), self._on_tmp)
            self._register("RCP", self._service_enum("RCP"), self._on_rcp)
            self._register("SYSTEM", self._service_enum("SYSTEM", "SYS"), self._on_system)

            # RTP/media byte capture is intentionally handled outside ADK by the TShark capture layer.
            # Do not register ADK RTP callback/port/raw-socket capture here.

            self.audio = AdkAudioPipeline(self._adk, self.cfg, self._emit_audio)
            self.audio.start()
            self._started = True
            self._emit("SYSTEM", "started", "ADK_STARTED")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        # 关闭 RTP UDP 直发 socket（VoiceMixin 中创建）
        rtp_sock = getattr(self, "_rtp_udp_socket", None)
        if rtp_sock is not None:
            try:
                rtp_sock.close()
            except OSError:
                pass
            self._rtp_udp_socket = None
            self._rtp_udp_target = None
        for name, service in list(self._services.items())[::-1]:
            callback = self._callbacks.get(name)
            try:
                if callback is not None:
                    service.Unregister(callback)
            except Exception:
                pass
        self._services.clear()
        self._callbacks.clear()
        if self._adk is not None:
            try:
                self._adk.Uninitialize()
            except Exception:
                pass
        self._adk = None
        self.audio = None
        if self._started:
            self._emit("SYSTEM", "stopped", "ADK_STOPPED")
        self._started = False
        if self._cwd is not None:
            try:
                os.chdir(self._cwd)
            except OSError:
                pass
            self._cwd = None
        if self._dll_cookie is not None:
            try:
                self._dll_cookie.close()
            except Exception:
                pass
            self._dll_cookie = None
