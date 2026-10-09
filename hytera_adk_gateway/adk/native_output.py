# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/native_output.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：suppress_hstrp_console_output
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import IO, Any, Iterable


_INSTALLED = False
_RESOURCES: list[Any] = []


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _iter_loggers() -> Iterable[logging.Logger]:
    """Yield the root logger and every currently-created concrete logger."""
    yield logging.getLogger()
    for item in logging.root.manager.loggerDict.values():
        if isinstance(item, logging.Logger):
            yield item


def _stream_fd(stream: Any) -> int | None:
    try:
        return int(stream.fileno())
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _open_console_stream(template: IO[str] | None) -> IO[str]:
    """Open an independent Windows console output stream.

    Using ``CONOUT$`` is important here.  Duplicating fd 1/2 is insufficient
    because Windows' console TextIO wrappers can keep using console-specific
    APIs after fd 1/2 has been redirected to ``NUL``, which causes WinError 1.
    """
    encoding = getattr(template, "encoding", None) or "utf-8"
    errors = getattr(template, "errors", None) or "backslashreplace"
    stream = open(
        "CONOUT$",
        mode="w",
        buffering=1,
        encoding=encoding,
        errors=errors,
        newline=None,
    )
    _RESOURCES.append(stream)
    return stream


def _rebind_python_streams(
    stdout_stream: IO[str],
    stderr_stream: IO[str],
    old_stdout: IO[str] | None,
    old_stderr: IO[str] | None,
) -> None:
    """Move Python/Uvicorn console logging away from native fd 1 and fd 2.

    Uvicorn creates its StreamHandler objects before the FastAPI lifespan
    starts.  Replacing only ``sys.stderr`` therefore does not update those
    existing handlers.  Rebind every current console StreamHandler before the
    native descriptors are redirected.
    """
    for logger in _iter_loggers():
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                continue
            if not isinstance(handler, logging.StreamHandler):
                continue

            stream = getattr(handler, "stream", None)
            fd = _stream_fd(stream)
            if stream is old_stdout or fd == 1:
                # Assign directly rather than setStream(): setStream() flushes
                # the old stream and may fail after native redirection.
                handler.stream = stdout_stream
            elif stream is old_stderr or fd == 2:
                handler.stream = stderr_stream

    sys.stdout = stdout_stream
    sys.__stdout__ = stdout_stream
    sys.stderr = stderr_stream
    sys.__stderr__ = stderr_stream


def suppress_hstrp_console_output() -> bool:
    """Silence vendor HSTRP/ADK native console output on Windows.

    The vendor DLL writes directly to the process' native stdout/stderr and
    bypasses Python logging.  Python/Uvicorn output is first moved to independent
    ``CONOUT$`` streams.  Only then are the C-runtime descriptors and Win32
    standard handles redirected to ``NUL``.

    Set ``HYTERA_SUPPRESS_HSTRP_LOGS=0`` to leave the vendor output enabled.
    If an independent console stream cannot be opened, suppression is skipped
    instead of risking damage to Python logging.
    """
    global _INSTALLED

    if _INSTALLED:
        return True
    if os.name != "nt":
        return False
    if not _env_enabled("HYTERA_SUPPRESS_HSTRP_LOGS", default=True):
        return False

    old_stdout = sys.stdout
    old_stderr = sys.stderr

    try:
        python_stdout = _open_console_stream(old_stdout)
        python_stderr = _open_console_stream(old_stderr)
    except OSError:
        # No attached console (for example, a Windows service).  Do not touch
        # fd 1/2 because doing so would break existing logging handlers.
        return False

    try:
        # Uvicorn/logging handlers must stop using fd 1/2 before dup2 below.
        _rebind_python_streams(
            python_stdout,
            python_stderr,
            old_stdout,
            old_stderr,
        )

        null_stream = open(os.devnull, mode="wb", buffering=0)
        null_fd = null_stream.fileno()
        _RESOURCES.append(null_stream)

        # Native printf/fprintf and the vendor C runtime normally use fd 1/2.
        os.dup2(null_fd, 1, inheritable=True)
        os.dup2(null_fd, 2, inheritable=True)

        # Some vendor components call GetStdHandle directly instead of using
        # the C-runtime descriptors, so replace the process standard handles.
        import msvcrt

        null_handle = msvcrt.get_osfhandle(null_fd)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        kernel32.SetStdHandle.restype = ctypes.c_int

        std_output_handle = ctypes.c_uint32(-11).value
        std_error_handle = ctypes.c_uint32(-12).value
        if not kernel32.SetStdHandle(
            std_output_handle,
            ctypes.c_void_p(null_handle),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.SetStdHandle(
            std_error_handle,
            ctypes.c_void_p(null_handle),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        # Python streams have already been moved to independent CONOUT$ handles,
        # so keeping them is safe even if native suppression is unavailable.
        return False

    _INSTALLED = True
    return True
