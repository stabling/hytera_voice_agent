# =============================================================================
# 【维护注释】文件：shared_runtime/logging_config.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：configure_project_logging, get_logger, log_print
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Project-wide logging configuration.

All Python-side diagnostic output is routed through :mod:`logging` and persisted
under ``logs/``.  The configuration is intentionally dependency-free so both
the gateway package and standalone maintenance scripts can use it safely.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

_CONFIG_LOCK = threading.Lock()
_CONFIGURED = False
_DEFAULT_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOG_DIR = _PROJECT_ROOT / "logs"


class _MillisecondFormatter(logging.Formatter):
    """Formatter that renders ``%(asctime)s`` with millisecond precision.

    The stdlib only appends milliseconds when ``datefmt`` is empty (and then
    uses a comma separator, ``20:57:55,038``).  This subclass keeps the
    configured ``datefmt`` and appends a dot-separated ``.mmm`` suffix so log
    timestamps read ``2026-09-16 20:57:55.038``.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        base = time.strftime(datefmt or _DATE_FORMAT, self.converter(record.created))
        return f"{base}.{int(record.msecs):03d}"


class _NamespaceFilter(logging.Filter):
    def __init__(self, prefixes: tuple[str, ...], *, invert: bool = False) -> None:
        super().__init__()
        self._prefixes = prefixes
        self._invert = invert

    def filter(self, record: logging.LogRecord) -> bool:
        matched = record.name.startswith(self._prefixes)
        return not matched if self._invert else matched


def _resolve_level(level: str | int | None) -> int:
    value = level or os.environ.get("HYTERA_LOG_LEVEL") or os.environ.get(
        "VOICE_LOG_LEVEL", "INFO"
    )
    if isinstance(value, int):
        return value
    return int(getattr(logging, str(value).upper(), logging.INFO))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _build_file_handler(
    path: Path,
    *,
    level: int,
    namespace_filter: logging.Filter | None = None,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=int(os.environ.get("HYTERA_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
        backupCount=int(os.environ.get("HYTERA_LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(_MillisecondFormatter(_DEFAULT_FORMAT, _DATE_FORMAT))
    if namespace_filter is not None:
        handler.addFilter(namespace_filter)
    return handler


def configure_project_logging(
    level: str | int | None = None,
    *,
    log_dir: str | Path | None = None,
    console: bool | None = None,
) -> Path:
    """Configure console and rotating file logs once for the whole process.

    Returns the resolved log directory.  Repeated calls are safe and only update
    the root logger level.
    """

    global _CONFIGURED
    numeric_level = _resolve_level(level)
    resolved_dir = Path(
        log_dir or os.environ.get("HYTERA_LOG_DIR") or _DEFAULT_LOG_DIR
    ).expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)

    with _CONFIG_LOCK:
        root = logging.getLogger()
        root.setLevel(numeric_level)
        if _CONFIGURED:
            for handler in root.handlers:
                handler.setLevel(numeric_level)
            return resolved_dir

        formatter = _MillisecondFormatter(_DEFAULT_FORMAT, _DATE_FORMAT)
        use_console = _env_bool("HYTERA_LOG_CONSOLE", True) if console is None else console
        if use_console:
            stream_handler = logging.StreamHandler()
            stream_handler.setLevel(numeric_level)
            stream_handler.setFormatter(formatter)
            root.addHandler(stream_handler)

        root.addHandler(
            _build_file_handler(
                resolved_dir / "application.log",
                level=numeric_level,
            )
        )
        root.addHandler(
            _build_file_handler(
                resolved_dir / "voice_agent.log",
                level=numeric_level,
                namespace_filter=_NamespaceFilter(("voice_agent",)),
            )
        )
        root.addHandler(
            _build_file_handler(
                resolved_dir / "hytera_gateway.log",
                level=numeric_level,
                namespace_filter=_NamespaceFilter(("hytera_adk_gateway",)),
            )
        )
        root.addHandler(
            _build_file_handler(
                resolved_dir / "tools.log",
                level=numeric_level,
                namespace_filter=_NamespaceFilter(
                    ("voice_agent", "hytera_adk_gateway"), invert=True
                ),
            )
        )
        _CONFIGURED = True
    return resolved_dir


def get_logger(name: str) -> logging.Logger:
    """Return a logger after ensuring persistent handlers are configured."""

    configure_project_logging()
    return logging.getLogger(name)


def log_print(
    logger: logging.Logger,
    *values: Any,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
    level: int | None = None,
) -> None:
    """Logging replacement for legacy ``print`` calls.

    ``file=sys.stderr`` maps to ERROR; all other calls default to INFO.  Embedded
    newlines are written as separate log records so every line receives a
    timestamp and module name.
    """

    del flush  # Logging handlers own flushing behavior.
    message = sep.join(str(value) for value in values)
    if end and end != "\n":
        message += end.rstrip("\n")
    selected_level = level
    if selected_level is None:
        selected_level = logging.ERROR if file in {sys.stderr, sys.__stderr__} else logging.INFO
    lines = message.splitlines() or [""]
    for line in lines:
        logger.log(selected_level, "%s", line)
