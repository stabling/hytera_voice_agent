# =============================================================================
# 【维护注释】文件：shared_runtime/architecture.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：RuntimeArchitecture, current_architecture, require_windows_x86_python310, require_windows_x64_python310_plus
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Runtime architecture checks for the split x86/x64 deployment."""

from __future__ import annotations

import platform
import struct
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeArchitecture:
    system: str
    machine: str
    python_version: str
    process_bits: int

    def as_dict(self) -> dict[str, object]:
        return {
            "system": self.system,
            "machine": self.machine,
            "python_version": self.python_version,
            "process_bits": self.process_bits,
        }


def current_architecture() -> RuntimeArchitecture:
    return RuntimeArchitecture(
        system=platform.system(),
        machine=platform.machine(),
        python_version=platform.python_version(),
        process_bits=struct.calcsize("P") * 8,
    )


def require_windows_x86_python310() -> RuntimeArchitecture:
    arch = current_architecture()
    if sys.platform != "win32":
        raise RuntimeError("Hytera ADK Gateway must run on Windows")
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError("Hytera ADK Gateway requires CPython 3.10")
    if arch.process_bits != 32:
        raise RuntimeError("Hytera ADK Gateway requires a 32-bit Python process")
    return arch


def require_windows_x64_python310_plus() -> RuntimeArchitecture:
    arch = current_architecture()
    if sys.platform != "win32":
        raise RuntimeError("LangGraph Agent production launcher is intended for Windows")
    if sys.version_info < (3, 10):
        raise RuntimeError("LangGraph Agent requires Python 3.10 or newer")
    if arch.process_bits != 64:
        raise RuntimeError("LangGraph Agent requires a 64-bit Python process")
    return arch
