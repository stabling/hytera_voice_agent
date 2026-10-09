# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/cli.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：.backend, .config, hytera_adk_gateway.logging_config
# 主要公开符号：interactive, main
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import argparse
import json
import shlex
import time

from .backend import HyteraAdkBackend
from .config import load_config
from hytera_adk_gateway.logging_config import get_logger, log_print

logger = get_logger(__name__)


def _print(event: object) -> None:
    log_print(logger, json.dumps(event.to_dict(), ensure_ascii=False, default=str), flush=True)


def _drain(backend: HyteraAdkBackend, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        event = backend.next_event(max(0.01, deadline - time.monotonic()))
        if event is not None:
            _print(event)


def _parse_channel(text: str) -> int:
    # Accept explicit prefixes so operators can paste either the decimal integer
    # printed by ``channels`` (e.g. 231678320) or the hexadecimal form printed
    # by transport diagnostics (e.g. 0x0DCFB870 or DCFB870).
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered.startswith("0x"):
        return int(stripped, 16)
    if lowered.startswith("dec:"):
        return int(stripped[4:], 10)
    if lowered.startswith("hex:"):
        return int(stripped[4:], 16)
    # A pure decimal string is treated as decimal; otherwise fall back to hex
    # so historical usage (bare hex digits) keeps working.
    if stripped.isdigit():
        return int(stripped, 10)
    return int(stripped, 16)


def _health(backend: HyteraAdkBackend) -> dict[str, object]:
    return backend.transport_state()


def interactive(cfg_path: str, *, debug_fields: bool = False) -> int:
    cfg = load_config(cfg_path)
    help_text = """Commands:
  status | health | channels | channels-detail | routes | events [count]
  wait [seconds] | wait-online [seconds]
  channel auto | channel <id>            # <id> is decimal (231678320) or hex (0xDCFB870)
  sms-private <radio_id> <text>           # vendor-demo-compatible: no terminal ACK
  sms-private-ack <radio_id> <text>       # require terminal ACK
  sms-private-noack <radio_id> <text>     # alias for sms-private
  sms-group <group_id> <text>
  voices | voice-attach | voice-private <radio_id> | voice-group <group_id> | voice-stop
  quit

A runtime channel is supplied by ADK in SYSTEM_CHANNEL_CHANGED_REPORT or a
received RRS/TMP/RCP callback. A CPS channel alias such as D3 is not a runtime
ADK channel ID and must not be entered into `channel`.

Private TMP is blocked until at least one runtime repeater channel exists. Once
channels are online, sms-private uses the currently selected concrete runtime
channel (auto or manual). This gateway never sends channel_id=0 anymore because
the observed V4 DLL rejects it locally as PACKET_ERROR_BUFFER_INVALID.
"""
    with HyteraAdkBackend(cfg, debug_fields=debug_fields) as backend:
        log_print(logger, help_text)
        while True:
            _drain(backend, 0.02)
            try:
                line = input("hytera> ").strip()
            except (KeyboardInterrupt, EOFError):
                log_print(logger, )
                break
            if not line:
                continue
            args = shlex.split(line)
            command = args[0].lower()
            try:
                if command in {"quit", "exit"}:
                    break
                if command == "help":
                    log_print(logger, help_text)
                elif command == "status":
                    log_print(
                        logger,
                        json.dumps(
                            {
                                "selected_channel": backend.selected_channel,
                                "channels": backend.channels,
                                "radio_routes": backend.radio_routes,
                                "event_counts": backend.event_counts,
                                "last_event": None
                                if backend.last_event is None
                                else backend.last_event.to_dict(),
                                "voice": None if backend.voice is None else backend.voice.__dict__,
                                "audio_mode": cfg.audio_mode,
                                "transport": _health(backend),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif command == "health":
                    log_print(logger, json.dumps(_health(backend), ensure_ascii=False, indent=2))
                elif command == "channels":
                    log_print(logger, json.dumps(backend.channels, ensure_ascii=False, indent=2))
                elif command == "channels-detail":
                    detail = []
                    for cid, info in backend.channels.items():
                        detail.append(
                            {
                                "channel_id_dec": int(cid),
                                "channel_id_hex": f"0x{int(cid):08X}",
                                **info,
                            }
                        )
                    log_print(
                        logger,
                        json.dumps(
                            {
                                "selected_channel_dec": backend.selected_channel,
                                "selected_channel_hex": None
                                if backend.selected_channel is None
                                else f"0x{int(backend.selected_channel):08X}",
                                "selected_channel_source": getattr(
                                    backend, "selected_channel_source", None
                                ),
                                "channels": detail,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif command == "routes":
                    log_print(logger, json.dumps(backend.radio_routes, ensure_ascii=False, indent=2))
                elif command == "events":
                    limit = int(args[1]) if len(args) == 2 else 30
                    log_print(
                        logger,
                        json.dumps(
                            [event.to_dict() for event in backend.recent_events(limit)],
                            ensure_ascii=False,
                            indent=2,
                            default=str,
                        )
                    )
                elif command == "wait-online":
                    seconds = float(args[1]) if len(args) == 2 else 45.0
                    _drain(backend, seconds)
                    log_print(logger, json.dumps(_health(backend), ensure_ascii=False, indent=2))
                elif command == "channel" and len(args) == 2:
                    value = None if args[1].lower() == "auto" else _parse_channel(args[1])
                    log_print(logger, f"selected_channel={backend.select_channel(value)}")
                elif command in {"sms-private", "sms-private-noack"} and len(args) >= 3:
                    log_print(
                        logger,
                        json.dumps(
                            backend.send_private_text(
                                int(args[1]), " ".join(args[2:]), require_ack=False
                            ).to_dict(),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif command == "sms-private-ack" and len(args) >= 3:
                    log_print(
                        logger,
                        json.dumps(
                            backend.send_private_text(
                                int(args[1]), " ".join(args[2:]), require_ack=True
                            ).to_dict(),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif command == "sms-group" and len(args) >= 3:
                    log_print(
                        logger,
                        json.dumps(
                            backend.send_group_text(int(args[1]), " ".join(args[2:])).to_dict(),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif command == "voices":
                    log_print(logger, json.dumps(backend.audio_devices(), ensure_ascii=False, indent=2))
                elif command == "voice-attach":
                    backend.attach_audio()
                    log_print(logger, "Audio attached")
                elif command == "voice-private" and len(args) == 2:
                    log_print(
                        logger,
                        json.dumps(
                            backend.start_voice_call(int(args[1]), "private").to_dict(),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif command == "voice-group" and len(args) == 2:
                    log_print(
                        logger,
                        json.dumps(
                            backend.start_voice_call(int(args[1]), "group").to_dict(),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif command == "voice-stop":
                    log_print(
                        logger,
                        json.dumps(
                            backend.stop_voice_call().to_dict(), ensure_ascii=False, indent=2
                        )
                    )
                elif command == "wait":
                    _drain(backend, float(args[1]) if len(args) > 1 else 5.0)
                else:
                    log_print(logger, "Unknown or incomplete command. Type help.")
            except Exception as exc:
                log_print(logger, f"ERROR {type(exc).__name__}: {exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hytera ADK V4 Python text/voice gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser(
        "doctor", help="Validate runtime files, generated INI, and UDP listener ports"
    )
    doctor_parser.add_argument("--config", default="config/gateway.ini")
    interactive_parser = sub.add_parser("interactive", help="Open terminal text/voice console")
    interactive_parser.add_argument("--config", default="config/gateway.ini")
    interactive_parser.add_argument(
        "--debug-fields",
        action="store_true",
        help="Include CLR reflected fields in received event JSON",
    )
    server_parser = sub.add_parser(
        "server", help="Run FastAPI HTTP server while keeping ADK UDP communication alive"
    )
    server_parser.add_argument("--config", default="config/gateway.ini")
    server_parser.add_argument("--host", default=None)
    server_parser.add_argument("--port", type=int, default=None)
    server_parser.add_argument(
        "--debug-fields",
        action="store_true",
        help="Include CLR reflected fields in received event JSON",
    )
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "doctor":
        from .backend import doctor

        log_print(logger, "\n".join(doctor(cfg)))
        return 0
    if args.command == "server":
        from .server import main as server_main

        forwarded = ["--config", args.config]
        if args.host:
            forwarded.extend(["--host", args.host])
        if args.port:
            forwarded.extend(["--port", str(args.port)])
        if args.debug_fields:
            forwarded.append("--debug-fields")
        return server_main(forwarded)
    return interactive(args.config, debug_fields=args.debug_fields)
