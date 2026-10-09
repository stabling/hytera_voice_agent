# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/capture/manager.py
# 所属层次：语音抓包层
# 主要职责：负责 TShark 抓包、RTP/UDP 数据整理、会话产物和抓包状态管理。
# 项目内直接依赖：.io_utils, .models, .packet, .tshark
# 主要公开符号：TSharkVoiceCaptureManager
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import base64
import csv
import json
import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from .io_utils import (
    ensure_dir,
    now_iso,
    safe_session_stamp,
    tail_csv,
    tail_text,
    write_length_prefixed,
)
from .models import CaptureSession
from .sms import parse_hytera_tmp_sms
from .packet import (
    clean_hex_to_bytes,
    extract_hytera_radio_id,
    parse_rtp_from_tshark_fields,
    parse_rtp_packet,
    write_pcmu_wav_from_raw,
)
from .tshark import (
    TSharkCapabilities,
    find_tshark,
    make_bpf_filter,
    probe_tshark_capabilities,
    validate_tshark_interface,
)

logger = logging.getLogger("hytera_adk_gateway.capture")


class TSharkVoiceCaptureManager:
    """TShark 语音抓包管理器。

    管理一个活跃的 TShark 抓包进程，将 RTP/音频数据存储到磁盘。
    同一时间只允许运行一个抓包会话。

    抓包输出文件说明：
    - packets.csv: 每个包的元数据（序列号、时间戳、SSRC 等）
    - packets.jsonl: 每个包的完整详细信息（含 base64 数据）
    - rtp_packets_with_length_prefix.bin: 完整 RTP 包（带长度前缀）
    - audio_payload_raw.bin: 去掉 RTP 头后的原始音频 payload
    - audio_payload_as_pcmu_8k_mono.wav: 自动生成的 WAV 文件
    """

    def __init__(
        self,
        root_dir: str | Path = "voice_capture",
        *,
        sms_ports: list[int] | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        ensure_dir(self.root_dir)
        self._lock = threading.RLock()
        self._session: CaptureSession | None = None
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._packet_condition = threading.Condition()
        self._packet_generation = 0
        # TShark 各版本、已安装插件和厂商 dissector 暴露的字段名可能不同。
        # 首次启动抓包时通过 ``tshark -G fields`` 自动发现可能表示源终端 ID
        # 的字段，并把字段名和值一起写入诊断日志。
        self._tshark_id_field_catalog: list[dict[str, str]] | None = None
        self._tshark_capabilities: TSharkCapabilities | None = None
        # 从 RTP 扩展数据中实时提取的源终端 Radio ID（Hytera DMR 协议）。
        self._latest_radio_id: int | None = None
        self._latest_radio_id_lock = threading.Lock()
        # 与语音共用一个 TShark 进程，避免同一网卡重复抓包。TMP 端口只做
        # 原始 UDP 短信解析，不参与 RTP Decode-As。
        self.sms_ports = tuple(
            dict.fromkeys(int(port) for port in (sms_ports or []) if 1 <= int(port) <= 65535)
        )

    @property
    def session(self) -> CaptureSession | None:
        return self._session

    @property
    def latest_radio_id(self) -> int | None:
        """最近一次从 UDP/RTP 扩展数据中提取到的源终端 Radio ID。"""
        with self._latest_radio_id_lock:
            return self._latest_radio_id

    def packet_generation(self) -> int:
        """返回最近一次已落盘语音包的递增序号。"""
        with self._packet_condition:
            return self._packet_generation

    def wait_for_packet(self, generation: int, timeout: float) -> int:
        """等待新语音包落盘；超时后返回当前序号。"""
        with self._packet_condition:
            self._packet_condition.wait_for(
                lambda: self._packet_generation != generation,
                timeout=max(0.0, float(timeout)),
            )
            return self._packet_generation

    def _notify_packet_persisted(self) -> None:
        with self._packet_condition:
            self._packet_generation += 1
            self._packet_condition.notify_all()

    def start(
        self,
        *,
        iface: str,
        ports: list[int] | None = None,
        src_ip: str | None = None,
        dst_ip: str | None = None,
        host: str | None = None,
        strict_direction: bool = False,
        out_dir: str | Path | None = None,
        tshark_path: str | None = None,
    ) -> dict[str, Any]:
        """启动 TShark 抓包会话。

        参数:
            iface: 网卡名称或编号
            ports: UDP 端口列表，默认 [30012, 30014]
            src_ip: 源 IP 过滤
            dst_ip: 目的 IP 过滤
            host: 不区分方向的主机过滤
            strict_direction: 是否使用严格方向匹配
            out_dir: 输出目录（默认自动生成）
            tshark_path: tshark.exe 路径
        """
        with self._lock:
            if self._session is not None and self._session.running:
                return {"ok": False, "error": "抓包已在运行中", "session": self._session.as_dict()}

            tshark = find_tshark(tshark_path)
            if not tshark:
                return {
                    "ok": False,
                    "error": "未找到 tshark.exe，请安装 Wireshark 或指定 tshark_path",
                }
            if not iface:
                return {"ok": False, "error": "iface 是必填参数，请使用 tshark -D 查看可用网卡"}

            rtp_ports = list(dict.fromkeys(int(p) for p in (ports or [30012, 30014])))
            capture_ports = list(dict.fromkeys(rtp_ports + list(self.sms_ports)))
            capabilities = probe_tshark_capabilities(tshark)
            if not capabilities.ok:
                return {
                    "ok": False,
                    "error": (
                        "TShark 能力探测失败，抓包未启动: "
                        + str(capabilities.error or "未知错误")
                    ),
                    "tshark": capabilities.as_dict(),
                }
            interface_check = validate_tshark_interface(tshark, str(iface))
            if not interface_check.get("ok"):
                return {
                    "ok": False,
                    "error": (
                        "TShark 抓包接口校验失败: "
                        + str(interface_check.get("error"))
                    ),
                    "interfaces": interface_check.get("stdout"),
                }
            self._tshark_capabilities = capabilities
            self._tshark_id_field_catalog = None
            try:
                bpf = make_bpf_filter(
                    capture_ports,
                    src_ip=src_ip,
                    dst_ip=dst_ip,
                    host=host,
                    strict_direction=strict_direction,
                )
            except Exception as exc:
                return {"ok": False, "error": f"无效的抓包过滤参数: {type(exc).__name__}: {exc}"}

            session_id = safe_session_stamp()
            session_dir = Path(out_dir).resolve() if out_dir else self.root_dir / session_id
            ensure_dir(session_dir)

            session = CaptureSession(
                session_id=session_id,
                out_dir=session_dir,
                iface=str(iface),
                ports=capture_ports,
                bpf_filter=bpf,
                rtp_ports=rtp_ports,
                sms_ports=list(self.sms_ports),
                src_ip=src_ip,
                dst_ip=dst_ip,
                host=host,
                strict_direction=bool(strict_direction),
                tshark_path=tshark,
                running=True,
                tshark_version=(
                    capabilities.version_text.splitlines()[0]
                    if capabilities.version_text
                    else None
                ),
                tshark_payload_fields=list(capabilities.payload_fields),
                decode_as_rtp="rtp.payload" in capabilities.fields,
                tshark_rtp_field_fallback_ready=capabilities.rtp_field_fallback_ready,
            )
            self._session = session
            self._thread = threading.Thread(
                target=self._run_capture,
                args=(session,),
                name=f"hytera-tshark-voice-{session_id}",
                daemon=True,
            )
            self._thread.start()
            return {"ok": True, "session": session.as_dict()}

    def stop(self, timeout: float = 3.0) -> dict[str, Any]:
        """停止当前抓包会话并生成 WAV 文件。"""
        with self._lock:
            session = self._session
            proc = self._process
            thread = self._thread
            if session is None:
                return {"ok": True, "message": "当前没有运行中的抓包会话", "session": None}
            session.running = False
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

        if thread is not None:
            thread.join(timeout=timeout)

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                try:
                    self._process.kill()
                except Exception:
                    pass
            if self._session is not None:
                self._session.running = False
                self._session.stopped_at = self._session.stopped_at or now_iso()
                self._write_session_wav(self._session)
                return {
                    "ok": True,
                    "session": self._session.as_dict(),
                    "info": self.info(refresh_wav=False),
                }
            return {"ok": True, "session": None}

    def info(self, *, limit: int = 20, refresh_wav: bool = True) -> dict[str, Any]:
        """查询当前或最近一次抓包会话的数据信息。"""
        with self._lock:
            session = self._session
            if session is None:
                latest = self.latest_session_dir()
                if latest is None:
                    return {
                        "ok": True,
                        "running": False,
                        "session": None,
                        "latest": None,
                        "capture_root": str(self.root_dir),
                    }
                data = self.session_dir_info(latest, limit=limit, refresh_wav=refresh_wav)
                data["capture_root"] = str(self.root_dir)
                return data

            if refresh_wav:
                self._write_session_wav(session)
            data = self.session_dir_info(session.out_dir, limit=limit, refresh_wav=False)
            data["session"] = session.as_dict()
            data["running"] = session.running
            data["capture_root"] = str(self.root_dir)
            return data

    def latest_session_dir(self) -> Path | None:
        """获取最近一次抓包会话的输出目录。"""
        if not self.root_dir.is_dir():
            return None
        dirs = [p for p in self.root_dir.iterdir() if p.is_dir()]
        if not dirs:
            return None
        return max(dirs, key=lambda p: p.stat().st_mtime)

    def session_dir_info(
        self, out_dir: Path, *, limit: int = 20, refresh_wav: bool = True
    ) -> dict[str, Any]:
        """获取指定抓包目录的详细信息。"""
        if refresh_wav:
            self._write_wav_for_dir(out_dir)

        files = {
            "packets_csv": out_dir / "packets.csv",
            "packets_jsonl": out_dir / "packets.jsonl",
            "sms_messages_csv": out_dir / "sms_messages.csv",
            "sms_messages_jsonl": out_dir / "sms_messages.jsonl",
            "udp_payloads": out_dir / "udp_payloads_with_length_prefix.bin",
            "rtp_packets": out_dir / "rtp_packets_with_length_prefix.bin",
            "rtp_extension_data": out_dir / "rtp_extension_data_with_length_prefix.bin",
            "audio_payload_raw": out_dir / "audio_payload_raw.bin",
            "audio_wav_pcmu": out_dir / "audio_payload_as_pcmu_8k_mono.wav",
            "tshark_stderr": out_dir / "tshark_stderr.log",
            "tshark_stdout_fields": out_dir / "tshark_stdout_fields.log",
            "tshark_radio_id_field_catalog": out_dir / "tshark_radio_id_field_catalog.json",
            "udp_radio_id_diagnostics": out_dir / "udp_radio_id_diagnostics.jsonl",
            "capture_manifest": out_dir / "capture_manifest.json",
            "tshark_command": out_dir / "tshark_command.txt",
            "tshark_command_json": out_dir / "tshark_command.json",
        }
        file_info = {
            name: {
                "path": str(path),
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
            }
            for name, path in files.items()
        }

        rows = tail_csv(files["packets_csv"], limit=limit)
        total_packets = 0
        rtp_packets = 0
        audio_bytes = 0
        sms_messages = 0
        if files["packets_csv"].is_file():
            try:
                with files["packets_csv"].open("r", encoding="utf-8", newline="") as f:
                    all_rows = list(csv.DictReader(f))
                total_packets = len(all_rows)
                for row in all_rows:
                    if str(row.get("is_rtp", "")).lower() in {"true", "1", "yes"}:
                        rtp_packets += 1
                    try:
                        audio_bytes += int(row.get("audio_payload_size") or 0)
                    except Exception:
                        pass
            except Exception:
                pass
        if files["sms_messages_csv"].is_file():
            try:
                with files["sms_messages_csv"].open("r", encoding="utf-8", newline="") as f:
                    sms_messages = sum(1 for _row in csv.DictReader(f))
            except Exception:
                pass

        running = bool(self._session and self._session.running and self._session.out_dir == out_dir)

        diagnosis: list[str] = []
        if running and total_packets == 0:
            diagnosis.append(
                "TShark 进程正在运行但尚未解析到 UDP payload。"
                "请检查 iface、ports、IP 过滤参数以及 Npcap/Wireshark 权限。"
            )
            diagnosis.append(
                "可查看会话目录中的 tshark_stdout_fields.log 和 tshark_stderr.log 排查。"
                "如果 stdout 为空说明 TShark 未抓到匹配的包；如果 stdout 非空但 packet 文件为空，说明字段解析失败。"
            )

        return {
            "ok": True,
            "running": running,
            "latest": str(out_dir),
            "summary": {
                "total_udp_payload_packets": total_packets,
                "rtp_packets": rtp_packets,
                "non_rtp_packets": max(0, total_packets - rtp_packets),
                "sms_messages": sms_messages,
                "audio_payload_bytes": audio_bytes,
            },
            "files": file_info,
            "recent_packets": rows,
            "diagnosis": diagnosis,
            "tshark_stderr_tail": tail_text(files["tshark_stderr"], 4000),
            "tshark_stdout_fields_tail": tail_text(files["tshark_stdout_fields"], 4000),
        }

    @staticmethod
    def _looks_like_terminal_id_field(display_name: str, abbreviation: str, protocol: str) -> bool:
        """筛选与数字集群终端身份相关的 TShark 字段。

        仅匹配 DMR/Hytera/MOTOTRBO/P25/TETRA/NXDN/dPMR 等无线协议上下文，
        避免把 IEEE 1722.1、GSM、工业协议中的 ``device_id``、``talker_id``
        等无关字段加入抓包命令并打印数百行噪声。
        """
        text = f"{display_name} {abbreviation} {protocol}".lower()
        compact = re.sub(r"[^a-z0-9]+", "", text)
        radio_protocol_terms = (
            "dmr",
            "hytera",
            "hyt",
            "mototrbo",
            "trbo",
            "p25",
            "tetra",
            "nxdn",
            "dpmr",
        )
        if not any(term in text for term in radio_protocol_terms):
            return False

        id_patterns = (
            "srcid",
            "sourceid",
            "senderid",
            "radioid",
            "terminalid",
            "subscriberid",
            "callerid",
            "callingpartyid",
            "originatorid",
            "unitid",
            "msid",
            "issi",
        )
        if any(pattern in compact for pattern in id_patterns):
            return True

        source_terms = (
            "source",
            "sender",
            "radio",
            "terminal",
            "subscriber",
            "caller",
            "calling party",
            "originator",
            "unit",
            "mobile station",
        )
        id_terms = (" id", "identifier", "identity", "number")
        return any(term in text for term in source_terms) and any(term in text for term in id_terms)

    def _discover_tshark_terminal_id_fields(self, tshark_path: str) -> list[dict[str, str]]:
        """从当前 TShark 安装中发现可能的源终端 ID 字段。

        ``tshark -G fields`` 输出的是当前版本及已安装 dissector 的真实字段表，
        因此该结果比在代码中猜测一个固定参数名更可靠。
        """
        if self._tshark_id_field_catalog is not None:
            return list(self._tshark_id_field_catalog)

        catalog: list[dict[str, str]] = []
        try:
            result = subprocess.run(
                [tshark_path, "-G", "fields"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20.0,
                check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "无法读取 TShark 字段目录，源终端参数名只能通过原始包和 ADK 事件确认 | "
                    "returncode=%s stderr=%s",
                    result.returncode,
                    (result.stderr or "").strip()[:1000],
                )
            else:
                seen: set[str] = set()
                for line in result.stdout.splitlines():
                    parts = line.split("\t")
                    # 字段记录通常为：F, Display name, abbreviation, type, parent, ...
                    if len(parts) < 3 or parts[0] != "F":
                        continue
                    display_name = parts[1].strip()
                    abbreviation = parts[2].strip()
                    if not abbreviation or abbreviation in seen:
                        continue
                    protocol = parts[4].strip() if len(parts) > 4 else ""
                    if not self._looks_like_terminal_id_field(
                        display_name, abbreviation, protocol
                    ):
                        continue
                    seen.add(abbreviation)
                    catalog.append(
                        {
                            "name": abbreviation,
                            "display_name": display_name,
                            "type": parts[3].strip() if len(parts) > 3 else "",
                            "protocol": protocol,
                        }
                    )
                    # 无线协议中的来源/主叫 ID 字段数量通常很少；保留上限防御异常插件。
                    if len(catalog) >= 32:
                        break
        except Exception as exc:
            logger.warning(
                "读取 TShark 字段目录失败，源终端参数名只能通过原始包和 ADK 事件确认 | %s: %s",
                type(exc).__name__,
                exc,
            )

        self._tshark_id_field_catalog = catalog
        return list(catalog)

    def _tshark_field_specs(self, session: CaptureSession) -> list[tuple[str, str]]:
        """按当前 TShark 的真实字段能力构建输出字段。"""
        capabilities = self._tshark_capabilities
        if capabilities is None or not capabilities.ok:
            raise RuntimeError("TShark 能力尚未通过启动前探测")
        available = capabilities.fields

        candidates = [
            ("frame_number", "frame.number"),
            ("frame_time_epoch", "frame.time_epoch"),
            ("frame_length", "frame.len"),
            ("frame_capture_length", "frame.cap_len"),
            ("src_ip", "ip.src"),
            ("src_ipv6", "ipv6.src"),
            ("src_port", "udp.srcport"),
            ("dst_ip", "ip.dst"),
            ("dst_ipv6", "ipv6.dst"),
            ("dst_port", "udp.dstport"),
            ("udp_length", "udp.length"),
            ("udp_payload_hex", "udp.payload"),
            ("rtp_payload_hex", "rtp.payload"),
            ("data_data_hex", "data.data"),
            ("rtp_version", "rtp.version"),
            ("rtp_padding", "rtp.padding"),
            ("rtp_padding_count", "rtp.padding.count"),
            ("rtp_extension", "rtp.ext"),
            ("rtp_csrc_count", "rtp.cc"),
            ("rtp_marker", "rtp.marker"),
            ("rtp_payload_type", "rtp.p_type"),
            ("rtp_sequence", "rtp.seq"),
            ("rtp_timestamp", "rtp.timestamp"),
            ("rtp_ssrc", "rtp.ssrc"),
            ("rtp_extension_profile", "rtp.ext.profile"),
            ("rtp_extension_length", "rtp.ext.len"),
        ]
        specs = [(alias, field) for alias, field in candidates if field in available]
        aliases = {alias for alias, _field in specs}
        if (
            "udp_payload_hex" not in aliases
            and "rtp_payload_hex" not in aliases
            and "data_data_hex" not in aliases
        ):
            raise RuntimeError("当前 TShark 没有可用的 UDP/RTP 载荷字段")

        for item in self._discover_tshark_terminal_id_fields(session.tshark_path or "tshark"):
            if item["name"] in available:
                specs.append((item["name"], item["name"]))
        return specs

    def _build_tshark_command(
        self,
        session: CaptureSession,
        *,
        field_specs: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        """构建字段兼容且强制按配置端口解码 RTP 的 TShark 命令。"""
        specs = field_specs or self._tshark_field_specs(session)
        command = [
            session.tshark_path or "tshark",
            "-l",
            "-n",
            "-i",
            session.iface,
            "-f",
            session.bpf_filter,
        ]
        capabilities = self._tshark_capabilities
        if capabilities is not None and "rtp.payload" in capabilities.fields:
            decode_ports = session.rtp_ports or session.ports
            for port in decode_ports:
                command.extend(["-d", f"udp.port=={int(port)},rtp"])
        command.extend(
            [
                "-T",
                "fields",
                "-E",
                "separator=/t",
                "-E",
                "occurrence=f",
                "-E",
                "quote=n",
            ]
        )
        for _alias, tshark_field in specs:
            command.extend(["-e", tshark_field])
        return command

    def _stderr_pump(
        self, proc: subprocess.Popen[str], stderr_path: Path, session: CaptureSession
    ) -> None:
        """将 TShark stderr 输出实时写入文件（用于诊断）。"""
        try:
            assert proc.stderr is not None
            with stderr_path.open("a", encoding="utf-8", errors="replace") as stderr_fp:
                for line in proc.stderr:
                    stderr_fp.write(line)
                    stderr_fp.flush()
                    text = line.strip()
                    if text:
                        session.last_error = text
        except Exception as exc:
            session.last_error = f"stderr pump failed: {type(exc).__name__}: {exc}"

    @staticmethod
    def _row_int(row: dict[str, str], key: str) -> int | None:
        """读取 TShark 行中的整数，兼容十进制和 0x 十六进制。"""
        value = str(row.get(key) or "").strip()
        if not value:
            return None
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value, 10)
        except ValueError:
            return None

    def _decode_tshark_row(
        self, row: dict[str, str]
    ) -> tuple[bytes, dict[str, Any], str | None, bool]:
        """把一行 TShark 字段转换为完整 UDP 载荷和可信 RTP 解析结果。

        返回值依次为：可用的完整 UDP 载荷、RTP 解析结果、载荷来源、是否截断。
        若 TShark 报告抓包长度小于线上帧长度，则失败关闭，不使用截断音频。
        """
        frame_length = self._row_int(row, "frame_length")
        capture_length = self._row_int(row, "frame_capture_length")
        truncated = (
            frame_length is not None
            and capture_length is not None
            and capture_length < frame_length
        )
        if truncated:
            return (
                b"",
                {"ok": False, "error": "captured frame is truncated"},
                None,
                True,
            )

        udp_payload_text = str(row.get("udp_payload_hex") or "").strip()
        data_payload_text = str(row.get("data_data_hex") or "").strip()
        rtp_payload_text = str(row.get("rtp_payload_hex") or "").strip()
        udp_payload = clean_hex_to_bytes(udp_payload_text)
        data_payload = clean_hex_to_bytes(data_payload_text)
        rtp_audio = clean_hex_to_bytes(rtp_payload_text)

        # 字段非空但无法解码时直接失败，避免把损坏的十六进制文本当作空载荷。
        for source, text, decoded in (
            ("udp.payload", udp_payload_text, udp_payload),
            ("data.data", data_payload_text, data_payload),
            ("rtp.payload", rtp_payload_text, rtp_audio),
        ):
            if text and not decoded:
                return (
                    b"",
                    {"ok": False, "error": f"invalid hexadecimal data in {source}"},
                    source,
                    False,
                )

        # udp.payload 是完整 UDP 载荷。字段存在时必须由本地严格解析器通过，
        # 不能在原始字节校验失败后退回 TShark 元字段继续执行。
        if udp_payload:
            udp_length = self._row_int(row, "udp_length")
            if udp_length is not None:
                if udp_length < 8:
                    return (
                        udp_payload,
                        {"ok": False, "error": f"invalid UDP length: {udp_length}"},
                        "udp.payload",
                        False,
                    )
                expected_payload_length = udp_length - 8
                if len(udp_payload) != expected_payload_length:
                    return (
                        udp_payload,
                        {
                            "ok": False,
                            "error": (
                                "UDP payload length mismatch: "
                                f"expected={expected_payload_length}, actual={len(udp_payload)}"
                            ),
                        },
                        "udp.payload",
                        False,
                    )
            parsed = parse_rtp_packet(udp_payload)
            parsed["payload_source"] = "udp.payload"
            if parsed.get("ok") and rtp_audio and parsed.get("audio_payload") != rtp_audio:
                return (
                    udp_payload,
                    {
                        "ok": False,
                        "error": "udp.payload and rtp.payload are inconsistent",
                    },
                    "udp.payload",
                    False,
                )
            return udp_payload, parsed, "udp.payload", False

        # data.data 在不同解析器组合下含义不稳定，仅在它自身能严格解析为完整
        # RTP 包时使用；否则再尝试 TShark 已确认的 RTP 元字段兼容路径。
        data_error: str | None = None
        if data_payload:
            parsed = parse_rtp_packet(data_payload)
            if parsed.get("ok"):
                parsed["payload_source"] = "data.data"
                if rtp_audio and parsed.get("audio_payload") != rtp_audio:
                    return (
                        data_payload,
                        {
                            "ok": False,
                            "error": "data.data and rtp.payload are inconsistent",
                        },
                        "data.data",
                        False,
                    )
                return data_payload, parsed, "data.data", False
            data_error = f"data.data: {parsed.get('error')}"

        parsed = parse_rtp_from_tshark_fields(row, rtp_audio)
        if parsed.get("ok"):
            parsed["payload_source"] = "rtp.payload"
            # 旧版 TShark 兼容路径只有音频载荷，不能伪造完整 RTP 包。
            return b"", parsed, "rtp.payload", False

        error_parts = [part for part in (data_error, parsed.get("error")) if part]
        return (
            data_payload,
            {"ok": False, "error": "; ".join(str(part) for part in error_parts)},
            "data.data" if data_payload else None,
            False,
        )

    def _run_capture(self, session: CaptureSession) -> None:
        """捕获抓包线程入口中的所有异常，避免会话永久停留在运行状态。"""
        try:
            self._run_capture_impl(session)
        except Exception as exc:
            session.last_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                if self._process is not None and self._process.poll() is None:
                    try:
                        self._process.terminate()
                    except Exception:
                        logger.exception("TShark 抓包线程异常后终止进程失败")
                self._process = None
                session.running = False
                session.stopped_at = session.stopped_at or now_iso()
            try:
                self._write_session_wav(session)
                (session.out_dir / "capture_manifest.json").write_text(
                    json.dumps(session.as_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                logger.exception("TShark 抓包线程异常后写入会话诊断失败")
            logger.exception("TShark 抓包线程异常退出")

    def _run_capture_impl(self, session: CaptureSession) -> None:
        """启动 TShark、解析字段并持久化通过完整性校验的数据包。"""
        field_specs = self._tshark_field_specs(session)
        cmd = self._build_tshark_command(session, field_specs=field_specs)
        session.tshark_command = list(cmd)

        fields = [alias for alias, _tshark_field in field_specs]
        terminal_id_catalog = list(self._tshark_id_field_catalog or [])
        terminal_id_field_names = [item["name"] for item in terminal_id_catalog]

        packet_dir = session.out_dir / "packet_files"
        ensure_dir(packet_dir)

        csv_path = session.out_dir / "packets.csv"
        jsonl_path = session.out_dir / "packets.jsonl"
        sms_csv_path = session.out_dir / "sms_messages.csv"
        sms_jsonl_path = session.out_dir / "sms_messages.jsonl"
        udp_path = session.out_dir / "udp_payloads_with_length_prefix.bin"
        rtp_path = session.out_dir / "rtp_packets_with_length_prefix.bin"
        ext_path = session.out_dir / "rtp_extension_data_with_length_prefix.bin"
        audio_path = session.out_dir / "audio_payload_raw.bin"
        stderr_path = session.out_dir / "tshark_stderr.log"
        stdout_debug_path = session.out_dir / "tshark_stdout_fields.log"
        command_path = session.out_dir / "tshark_command.txt"
        command_json_path = session.out_dir / "tshark_command.json"
        manifest_path = session.out_dir / "capture_manifest.json"

        command_path.write_text(" ".join(str(x) for x in cmd), encoding="utf-8")
        command_json_path.write_text(
            json.dumps(cmd, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest_path.write_text(
            json.dumps(session.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._lock:
                self._process = proc

            self._stderr_thread = threading.Thread(
                target=self._stderr_pump,
                args=(proc, stderr_path, session),
                name=f"hytera-tshark-stderr-{session.session_id}",
                daemon=True,
            )
            self._stderr_thread.start()

            with (
                csv_path.open("a", encoding="utf-8", newline="") as csv_fp,
                jsonl_path.open("a", encoding="utf-8") as jsonl_fp,
                sms_csv_path.open("a", encoding="utf-8", newline="") as sms_csv_fp,
                sms_jsonl_path.open("a", encoding="utf-8") as sms_jsonl_fp,
                udp_path.open("ab") as udp_fp,
                rtp_path.open("ab") as rtp_fp,
                ext_path.open("ab") as ext_fp,
                audio_path.open("ab") as audio_fp,
                stdout_debug_path.open("a", encoding="utf-8", errors="replace") as stdout_dbg_fp,
            ):
                writer = csv.DictWriter(
                    csv_fp,
                    fieldnames=[
                        "index",
                        "captured_at",
                        "frame_number",
                        "frame_time_epoch",
                        "src_ip",
                        "src_port",
                        "dst_ip",
                        "dst_port",
                        "udp_length",
                        "protocol",
                        "info",
                        "udp_payload_size",
                        "payload_source",
                        "capture_truncated",
                        "tshark_radio_id_fields",
                        "is_rtp",
                        "rtp_parse_source",
                        "rtp_error",
                        "payload_type",
                        "sequence",
                        "timestamp",
                        "ssrc_hex",
                        "marker",
                        "extension",
                        "header_len",
                        "extension_profile",
                        "extension_length_words",
                        "audio_payload_size",
                        "udp_payload_file",
                        "rtp_packet_file",
                        "audio_payload_file",
                    ],
                )
                if csv_fp.tell() == 0:
                    writer.writeheader()
                    csv_fp.flush()

                sms_writer = csv.DictWriter(
                    sms_csv_fp,
                    fieldnames=[
                        "index",
                        "captured_at",
                        "frame_number",
                        "frame_time_epoch",
                        "src_ip",
                        "src_port",
                        "dst_ip",
                        "dst_port",
                        "frame_sequence",
                        "opcode_hex",
                        "request_id",
                        "destination_id",
                        "source_id",
                        "text",
                        "text_encoding",
                        "checksum_hex",
                        "raw_payload_hex",
                    ],
                )
                if sms_csv_fp.tell() == 0:
                    sms_writer.writeheader()
                    sms_csv_fp.flush()

                assert proc.stdout is not None
                for line in proc.stdout:
                    if not session.running:
                        break

                    stdout_dbg_fp.write(line)
                    stdout_dbg_fp.flush()

                    parts = line.rstrip("\r\n").split("\t")
                    while len(parts) < len(fields):
                        parts.append("")
                    row = dict(zip(fields, parts))

                    (
                        payload,
                        parsed,
                        payload_source,
                        capture_truncated,
                    ) = self._decode_tshark_row(row)
                    # 至少需要完整 UDP 载荷或经过字段校验的 RTP；空白/诊断行不进入包统计。
                    if not payload and not parsed.get("ok") and not capture_truncated:
                        continue

                    session.packet_count += 1
                    idx = session.packet_count

                    src_ip = row.get("src_ip") or row.get("src_ipv6") or ""
                    src_port = row.get("src_port") or ""
                    dst_ip = row.get("dst_ip") or row.get("dst_ipv6") or ""
                    dst_port = row.get("dst_port") or ""

                    sms_detail: dict[str, Any] | None = None
                    src_port_int = self._row_int(row, "src_port")
                    dst_port_int = self._row_int(row, "dst_port")
                    if payload and session.sms_ports and (
                        src_port_int in session.sms_ports or dst_port_int in session.sms_ports
                    ):
                        parsed_sms = parse_hytera_tmp_sms(payload)
                        if parsed_sms.get("ok") and parsed_sms.get("is_sms"):
                            session.sms_count += 1
                            sms_detail = dict(parsed_sms)
                            sms_row = {
                                "index": session.sms_count,
                                "captured_at": now_iso(),
                                "frame_number": row.get("frame_number"),
                                "frame_time_epoch": row.get("frame_time_epoch"),
                                "src_ip": src_ip,
                                "src_port": src_port,
                                "dst_ip": dst_ip,
                                "dst_port": dst_port,
                                "frame_sequence": parsed_sms.get("frame_sequence"),
                                "opcode_hex": parsed_sms.get("opcode_hex"),
                                "request_id": parsed_sms.get("request_id"),
                                "destination_id": parsed_sms.get("destination_id"),
                                "source_id": parsed_sms.get("source_id"),
                                "text": parsed_sms.get("text"),
                                "text_encoding": parsed_sms.get("text_encoding"),
                                "checksum_hex": parsed_sms.get("checksum_hex"),
                                "raw_payload_hex": parsed_sms.get("raw_payload_hex"),
                            }
                            sms_writer.writerow(sms_row)
                            sms_csv_fp.flush()
                            sms_jsonl_fp.write(
                                json.dumps(
                                    {
                                        **sms_row,
                                        "opcode": parsed_sms.get("opcode"),
                                        "message_length": parsed_sms.get("message_length"),
                                        "inner_offset": parsed_sms.get("inner_offset"),
                                        "checksum": parsed_sms.get("checksum"),
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                            sms_jsonl_fp.flush()
                            logger.info(
                                "TShark实时短信 | source_id=%s destination_id=%s "
                                "request_id=%s opcode=%s text=%r udp=%s:%s->%s:%s",
                                parsed_sms.get("source_id"),
                                parsed_sms.get("destination_id"),
                                parsed_sms.get("request_id"),
                                parsed_sms.get("opcode_hex"),
                                parsed_sms.get("text"),
                                src_ip,
                                src_port,
                                dst_ip,
                                dst_port,
                            )

                    safe_prefix = (
                        f"{idx:06d}_{src_ip}_{src_port}_to_{dst_ip}_{dst_port}".replace(":", "_")
                        .replace("\\", "_")
                        .replace("/", "_")
                    )
                    udp_packet_file = ""
                    rtp_packet_file = ""
                    audio_packet_file = ""

                    if payload:
                        udp_packet_file = f"{safe_prefix}_udp_payload.bin"
                        write_length_prefixed(udp_fp, payload)
                        udp_fp.flush()
                        (packet_dir / udp_packet_file).write_bytes(payload)

                    is_rtp = bool(parsed.get("ok")) and not capture_truncated
                    terminal_id_values = {
                        name: row.get(name)
                        for name in terminal_id_field_names
                        if str(row.get(name) or "").strip()
                    }

                    if is_rtp:
                        session.rtp_count += 1

                        # 只有拿到完整 UDP/RTP 字节时才写入完整 RTP 包文件。
                        if payload and parsed.get("parse_source") == "udp_payload":
                            write_length_prefixed(rtp_fp, payload)
                            rtp_fp.flush()
                            rtp_packet_file = f"{safe_prefix}_rtp_packet.bin"
                            (packet_dir / rtp_packet_file).write_bytes(payload)

                        ext_data = parsed.get("extension_data") or b""
                        if ext_data:
                            write_length_prefixed(ext_fp, ext_data)
                            ext_fp.flush()

                        audio_payload = parsed.get("audio_payload") or b""
                        if audio_payload:
                            audio_fp.write(audio_payload)
                            audio_fp.flush()
                            session.audio_payload_bytes += len(audio_payload)

                            audio_packet_file = f"{safe_prefix}_audio_payload.bin"
                            (packet_dir / audio_packet_file).write_bytes(audio_payload)
                    else:
                        session.non_rtp_count += 1

                    csv_row = {
                        "index": idx,
                        "captured_at": now_iso(),
                        "frame_number": row.get("frame_number"),
                        "frame_time_epoch": row.get("frame_time_epoch"),
                        "src_ip": src_ip,
                        "src_port": src_port,
                        "dst_ip": dst_ip,
                        "dst_port": dst_port,
                        "udp_length": row.get("udp_length"),
                        "protocol": "RTP" if is_rtp else "UDP",
                        "info": None,
                        "udp_payload_size": len(payload) if payload else None,
                        "payload_source": payload_source,
                        "capture_truncated": capture_truncated,
                        "tshark_radio_id_fields": json.dumps(
                            terminal_id_values, ensure_ascii=False, sort_keys=True
                        ),
                        "is_rtp": is_rtp,
                        "rtp_parse_source": parsed.get("parse_source"),
                        "rtp_error": parsed.get("error"),
                        "payload_type": parsed.get("payload_type"),
                        "sequence": parsed.get("sequence"),
                        "timestamp": parsed.get("timestamp"),
                        "ssrc_hex": parsed.get("ssrc_hex"),
                        "marker": parsed.get("marker"),
                        "extension": parsed.get("extension"),
                        "header_len": parsed.get("header_len"),
                        "extension_profile": parsed.get("extension_profile"),
                        "extension_length_words": parsed.get("extension_length_words"),
                        "audio_payload_size": parsed.get("audio_payload_size", 0),
                        "udp_payload_file": udp_packet_file,
                        "rtp_packet_file": rtp_packet_file,
                        "audio_payload_file": audio_packet_file,
                    }
                    writer.writerow(csv_row)
                    csv_fp.flush()

                    detail: dict[str, Any] = {
                        "index": idx,
                        "captured_at": now_iso(),
                        "frame_number": row.get("frame_number"),
                        "frame_time_epoch": row.get("frame_time_epoch"),
                        "src_ip": src_ip,
                        "src_port": src_port,
                        "dst_ip": dst_ip,
                        "dst_port": dst_port,
                        "protocol": "RTP" if is_rtp else "UDP",
                        "info": None,
                        "udp_payload_size": len(payload) if payload else None,
                        "udp_payload_b64": (
                            base64.b64encode(payload).decode("ascii") if payload else None
                        ),
                        "payload_source": payload_source,
                        "capture_truncated": capture_truncated,
                        # key 保留 TShark 的真实参数名，便于现场确认哪个字段是终端 ID。
                        "tshark_radio_id_fields": terminal_id_values,
                        "is_rtp": is_rtp,
                        "rtp_parse_source": parsed.get("parse_source"),
                        "rtp_error": parsed.get("error"),
                        "files": {
                            "udp_payload_file": str(packet_dir / udp_packet_file)
                            if udp_packet_file
                            else None,
                            "rtp_packet_file": str(packet_dir / rtp_packet_file)
                            if rtp_packet_file
                            else None,
                            "audio_payload_file": str(packet_dir / audio_packet_file)
                            if audio_packet_file
                            else None,
                        },
                    }
                    if sms_detail is not None:
                        detail["sms"] = sms_detail

                    if is_rtp:
                        payload_type = parsed.get("payload_type")
                        detail["payload_type"] = payload_type
                        if payload_type == 0:
                            detail["audio_codec"] = "pcmu"
                        elif payload_type == 8:
                            detail["audio_codec"] = "pcma"
                        detail["rtp"] = {
                            k: parsed.get(k)
                            for k in (
                                "version",
                                "padding",
                                "padding_count",
                                "extension",
                                "csrc_count",
                                "marker",
                                "payload_type",
                                "sequence",
                                "timestamp",
                                "ssrc",
                                "ssrc_hex",
                                "header_len",
                                "extension_profile",
                                "extension_length_words",
                                "rtp_packet_size",
                                "audio_payload_size",
                                "parse_source",
                                "payload_source",
                            )
                        }
                        audio_payload = parsed.get("audio_payload") or b""
                        detail["audio_payload_b64"] = base64.b64encode(audio_payload).decode(
                            "ascii"
                        )

                    extension_data = (parsed.get("extension_data") or b"") if is_rtp else b""
                    if extension_data:
                        extracted_id = extract_hytera_radio_id(extension_data)
                        if extracted_id is not None:
                            with self._latest_radio_id_lock:
                                self._latest_radio_id = extracted_id
                    jsonl_fp.write(json.dumps(detail, ensure_ascii=False) + "\n")
                    jsonl_fp.flush()
                    if is_rtp and parsed.get("audio_payload_size", 0) > 0:
                        self._notify_packet_persisted()

                    # Keep the manifest useful while the capture is still running.
                    if idx == 1 or idx % 20 == 0:
                        manifest_path.write_text(
                            json.dumps(session.as_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )

            session.process_returncode = proc.poll()
        except Exception as exc:
            session.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                if self._process is not None and self._process.poll() is None:
                    try:
                        self._process.terminate()
                    except Exception:
                        pass
                self._process = None
                session.running = False
                session.stopped_at = session.stopped_at or now_iso()
                self._write_session_wav(session)
                manifest_path.write_text(
                    json.dumps(session.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
                )

    def _write_session_wav(self, session: CaptureSession) -> dict[str, Any]:
        return self._write_wav_for_dir(session.out_dir)

    def _write_wav_for_dir(self, out_dir: Path) -> dict[str, Any]:
        return write_pcmu_wav_from_raw(
            out_dir / "audio_payload_raw.bin", out_dir / "audio_payload_as_pcmu_8k_mono.wav"
        )
