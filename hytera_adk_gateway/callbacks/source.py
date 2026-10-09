# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/source.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：..capture, ..capture.io_utils
# 主要公开符号：CallbackSourceMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..capture import parse_rtp_packet
from ..capture.io_utils import now_iso


class CallbackSourceMixin:
    """语音回调内部能力分组。"""

    def _current_jsonl_path(self) -> Path | None:
        session = self.capture_manager.session
        if session is not None:
            return session.out_dir / "packets.jsonl"
        latest = self.capture_manager.latest_session_dir()
        if latest is not None:
            return latest / "packets.jsonl"
        return None

    def _current_audio_raw_path(self) -> Path | None:
        session = self.capture_manager.session
        if session is not None:
            return session.out_dir / "audio_payload_raw.bin"
        latest = self.capture_manager.latest_session_dir()
        if latest is not None:
            return latest / "audio_payload_raw.bin"
        return None

    def _read_new_audio_raw_item(self) -> dict[str, Any] | None:
        """兜底读取 audio_payload_raw.bin 的新增音频字节。

        用途：packets.jsonl 明明所在会话已有 rtp/audio 统计，但回调线程没有从 JSONL
        提取到 audio_payload_b64 时，仍然能把新增音频作为一个合并段发送出去。
        """
        path = self._current_audio_raw_path()
        if path is None or not path.is_file():
            with self._lock:
                self._last_raw_fallback_bytes = 0
            return None

        with self._lock:
            if self._audio_raw_path != path:
                self._audio_raw_path = path
                self._audio_raw_pos = 0
            pos = self._audio_raw_pos

        try:
            size = path.stat().st_size
        except OSError as exc:
            self._remember_error(
                f"读取 audio_payload_raw.bin 状态失败: {type(exc).__name__}: {exc}"
            )
            return None

        if pos > size:
            pos = 0
        if size <= pos:
            with self._lock:
                self._last_raw_fallback_bytes = 0
            return None

        try:
            with path.open("rb") as fp:
                fp.seek(pos)
                raw = fp.read(size - pos)
                new_pos = fp.tell()
        except OSError as exc:
            self._remember_error(
                f"读取 audio_payload_raw.bin 新增音频失败: {type(exc).__name__}: {exc}"
            )
            return None

        with self._lock:
            self._audio_raw_pos = new_pos
            self._last_raw_fallback_bytes = len(raw)

        if not raw:
            return None

        audio_b64 = base64.b64encode(raw).decode("ascii")
        now = now_iso()
        return {
            "index": f"raw-{new_pos}",
            "captured_at": now,
            "src_ip": None,
            "src_port": None,
            "dst_ip": None,
            "dst_port": None,
            "udp_payload_size": None,
            "udp_payload_b64": None,
            "is_rtp": True,
            "rtp_error": None,
            "audio_payload_b64": audio_b64,
            "files": {"audio_payload_file": str(path)},
            "rtp": {
                "audio_payload_size": len(raw),
                "payload_type": "raw_fallback",
                "sequence": None,
                "timestamp": None,
                "ssrc": "raw_fallback",
                "ssrc_hex": "raw_fallback",
            },
            "_raw_fallback": True,
        }

    def _read_new_audio_items(self, *, max_items: int) -> list[dict[str, Any]]:
        path = self._current_jsonl_path()
        if path is None or not path.is_file():
            with self._lock:
                self._last_read_count = 0
                self._last_audio_item_count = 0
                self._last_ignored_count = 0
            return []

        with self._lock:
            if self._jsonl_path != path:
                self._jsonl_path = path
                self._jsonl_pos = 0
            pos = self._jsonl_pos

        snapshot_size = path.stat().st_size
        if pos > snapshot_size:
            pos = 0

        items: list[dict[str, Any]] = []
        read_count = 0
        ignored_count = 0
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            fp.seek(pos)
            # max_items 表示本轮最多取出的语音包数量。扫描范围固定在本轮开始时
            # 的文件快照，避免大量心跳/握手行长期挡住后续 RTP 音频包。
            while fp.tell() < snapshot_size and len(items) < max_items:
                line_start = fp.tell()
                line = fp.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    fp.seek(line_start)
                    break
                read_count += 1
                try:
                    detail = json.loads(line)
                except json.JSONDecodeError:
                    ignored_count += 1
                    continue

                audio_b64 = self._extract_audio_payload_b64(detail)
                if audio_b64:
                    detail["audio_payload_b64"] = audio_b64
                    items.append(detail)
                else:
                    ignored_count += 1
            new_pos = fp.tell()

        with self._lock:
            self._jsonl_pos = new_pos
            self._last_read_count = read_count
            self._last_audio_item_count = len(items)
            self._last_ignored_count = ignored_count
        return items

    def _extract_audio_payload_b64(self, detail: dict[str, Any]) -> str | None:
        """从 packets.jsonl 明细中尽可能提取音频 payload 的 Base64。"""
        value = detail.get("audio_payload_b64")
        if value:
            return str(value)

        # 兼容旧数据：如果 JSON 里没写 base64，但 packet_files 里已经保存了音频 payload，则直接读文件。
        files = detail.get("files") or {}
        audio_file = files.get("audio_payload_file")
        if audio_file:
            try:
                p = Path(str(audio_file))
                if p.is_file():
                    raw = p.read_bytes()
                    if raw:
                        return base64.b64encode(raw).decode("ascii")
            except Exception as exc:
                self._remember_error(f"读取 audio_payload_file 失败: {type(exc).__name__}: {exc}")

        # 再兜底：如果只有 udp_payload_b64，则重新尝试解析 RTP 并取 RTP header 后面的音频负载。
        udp_b64 = detail.get("udp_payload_b64")
        if udp_b64:
            try:
                udp_payload = base64.b64decode(str(udp_b64), validate=False)
                parsed = parse_rtp_packet(udp_payload)
                if parsed.get("ok") and parsed.get("audio_payload"):
                    rtp = detail.setdefault("rtp", {})
                    for key in (
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
                    ):
                        if key in parsed:
                            rtp[key] = parsed[key]
                    detail["is_rtp"] = True
                    detail["rtp_error"] = None
                    return base64.b64encode(parsed["audio_payload"]).decode("ascii")
            except Exception as exc:
                self._remember_error(
                    f"从 udp_payload_b64 兜底解析音频失败: {type(exc).__name__}: {exc}"
                )

        return None
