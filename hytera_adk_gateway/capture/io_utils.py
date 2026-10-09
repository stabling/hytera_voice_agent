# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/capture/io_utils.py
# 所属层次：语音抓包层
# 主要职责：负责 TShark 抓包、RTP/UDP 数据整理、会话产物和抓包状态管理。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：now_iso, safe_session_stamp, ensure_dir, write_length_prefixed, tail_text, tail_csv
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import csv
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def safe_session_stamp() -> str:
    """生成可安全用于文件名的 UTC 时间戳字符串。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def ensure_dir(path: Path) -> None:
    """确保目录存在，不存在则递归创建。"""
    path.mkdir(parents=True, exist_ok=True)


def write_length_prefixed(fp, data: bytes) -> None:
    """写入带 4 字节大端长度前缀的数据包。"""
    fp.write(struct.pack("!I", len(data)))
    fp.write(data)


def tail_text(path: Path, limit_chars: int = 4000) -> str:
    """读取文本文件的末尾内容（用于日志展示）。"""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max(1, int(limit_chars)) :]
    except Exception:
        return ""


def tail_csv(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """读取 CSV 文件的最后 N 行作为字典列表。"""
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-max(1, int(limit)) :]
    except Exception:
        return []
