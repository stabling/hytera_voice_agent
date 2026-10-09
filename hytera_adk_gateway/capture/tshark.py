# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/capture/tshark.py
# 所属层次：语音抓包层
# 主要职责：负责 TShark 抓包、RTP/UDP 数据整理、会话产物和抓包状态管理。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：TSharkCapabilities, find_tshark, list_tshark_interfaces, probe_tshark_capabilities, validate_tshark_interface, make_bpf_filter
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TSharkCapabilities:
    """当前 TShark 安装的字段与版本能力快照。"""

    ok: bool
    tshark_path: str
    version_text: str
    fields: frozenset[str]
    error: str | None = None

    @property
    def payload_fields(self) -> tuple[str, ...]:
        """按优先级返回可用的载荷字段。"""
        return tuple(
            field
            for field in ("udp.payload", "rtp.payload", "data.data")
            if field in self.fields
        )

    @property
    def rtp_field_fallback_ready(self) -> bool:
        """判断是否具备仅凭 TShark RTP 字段恢复元数据的最低条件。"""
        required = {
            "rtp.payload",
            "rtp.version",
            "rtp.p_type",
            "rtp.seq",
            "rtp.timestamp",
            "rtp.ssrc",
        }
        return required.issubset(self.fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tshark_path": self.tshark_path,
            "version_text": self.version_text,
            "payload_fields": list(self.payload_fields),
            "rtp_field_fallback_ready": self.rtp_field_fallback_ready,
            "field_count": len(self.fields),
            "error": self.error,
        }


def find_tshark(explicit: str | None = None) -> str | None:
    """查找 tshark.exe 路径。"""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
    found = shutil.which("tshark")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Wireshark\tshark.exe",
        r"C:\Program Files (x86)\Wireshark\tshark.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def list_tshark_interfaces(tshark_path: str | None = None) -> dict[str, Any]:
    """列出 TShark 可用的网络接口。"""
    tshark = find_tshark(tshark_path)
    if not tshark:
        return {"ok": False, "error": "未找到 tshark.exe，请安装 Wireshark"}
    proc = subprocess.run(
        [tshark, "-D"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "tshark": tshark,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def probe_tshark_capabilities(tshark_path: str) -> TSharkCapabilities:
    """读取当前 TShark 的版本和真实字段目录。

    抓包命令只使用探测到的字段，避免旧版 TShark 因未知 ``-e`` 字段
    直接退出。字段目录来自 TShark 自身，而不是按版本号猜测。
    """
    try:
        version_proc = subprocess.run(
            [tshark_path, "-v"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            check=False,
        )
        version_text = (version_proc.stdout or version_proc.stderr or "").strip()
        if version_proc.returncode != 0:
            return TSharkCapabilities(
                ok=False,
                tshark_path=tshark_path,
                version_text=version_text,
                fields=frozenset(),
                error=f"tshark -v 失败，退出码 {version_proc.returncode}",
            )

        fields_proc = subprocess.run(
            [tshark_path, "-G", "fields"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
        )
        if fields_proc.returncode != 0:
            return TSharkCapabilities(
                ok=False,
                tshark_path=tshark_path,
                version_text=version_text,
                fields=frozenset(),
                error=(fields_proc.stderr or "tshark -G fields 执行失败").strip()[:2000],
            )

        fields: set[str] = set()
        for line in fields_proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0] == "F" and parts[2].strip():
                fields.add(parts[2].strip())

        payload_fields = {
            field for field in ("udp.payload", "rtp.payload", "data.data") if field in fields
        }
        if not payload_fields:
            return TSharkCapabilities(
                ok=False,
                tshark_path=tshark_path,
                version_text=version_text,
                fields=frozenset(fields),
                error=(
                    "当前 TShark 未暴露 udp.payload、rtp.payload 或 data.data，"
                    "无法安全提取语音载荷"
                ),
            )

        capabilities = TSharkCapabilities(
            ok=True,
            tshark_path=tshark_path,
            version_text=version_text,
            fields=frozenset(fields),
        )
        if payload_fields == {"rtp.payload"} and not capabilities.rtp_field_fallback_ready:
            return TSharkCapabilities(
                ok=False,
                tshark_path=tshark_path,
                version_text=version_text,
                fields=frozenset(fields),
                error="当前 TShark 的 RTP 字段不完整，无法验证 sequence/timestamp/SSRC",
            )

        return capabilities
    except Exception as exc:
        return TSharkCapabilities(
            ok=False,
            tshark_path=tshark_path,
            version_text="",
            fields=frozenset(),
            error=f"{type(exc).__name__}: {exc}",
        )


def validate_tshark_interface(tshark_path: str, iface: str) -> dict[str, Any]:
    """确认配置的接口编号或名称存在于 ``tshark -D`` 输出。"""
    try:
        proc = subprocess.run(
            [tshark_path, "-D"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"执行 tshark -D 失败: {type(exc).__name__}: {exc}",
            "stdout": "",
        }
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or "tshark -D 执行失败").strip()[:2000],
            "stdout": proc.stdout,
        }

    requested = str(iface or "").strip()
    matches: list[str] = []
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line or "." not in line:
            continue
        number, remainder = line.split(".", 1)
        number = number.strip()
        remainder = remainder.strip()
        interface_name = remainder.split(" ", 1)[0]
        if requested in {number, remainder, interface_name}:
            matches.append(raw_line)
    if matches:
        return {"ok": True, "matches": matches, "stdout": proc.stdout}
    return {
        "ok": False,
        "error": f"接口 {requested!r} 不在 tshark -D 列表中",
        "stdout": proc.stdout,
    }


def make_bpf_filter(
    ports: list[int],
    src_ip: str | None = None,
    dst_ip: str | None = None,
    host: str | None = None,
    *,
    strict_direction: bool = False,
) -> str:
    """构建 TShark 的 BPF 抓包过滤表达式。"""
    valid_ports = [int(p) for p in ports if 1 <= int(p) <= 65535]
    if not valid_ports:
        raise ValueError("at least one UDP port is required")

    port_expr = " or ".join(f"udp port {p}" for p in valid_ports)
    parts = [f"udp and ({port_expr})"]

    if host:
        parts.append(f"host {host}")
    elif strict_direction and src_ip and dst_ip:
        parts.append(f"src host {src_ip}")
        parts.append(f"dst host {dst_ip}")
    else:
        hosts: list[str] = []
        if src_ip:
            hosts.append(src_ip)
        if dst_ip and dst_ip not in hosts:
            hosts.append(dst_ip)
        if hosts:
            parts.append("(" + " or ".join(f"host {value}" for value in hosts) + ")")

    return " and ".join(parts)
