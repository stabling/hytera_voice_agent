# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/runtime_support/bootstrap.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：prepare_runtime_imports
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_VENV = (PROJECT_ROOT / ".venv").resolve()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def prepare_runtime_imports() -> None:
    """清理误注入的外部虚拟环境路径，并优先使用本项目依赖。"""
    cleaned: list[str] = []
    for item in sys.path:
        try:
            resolved = Path(item).resolve()
        except Exception:
            cleaned.append(item)
            continue
        is_venv = ".venv" in {part.lower() for part in resolved.parts}
        if not is_venv or _is_under(resolved, PROJECT_VENV):
            cleaned.append(item)
    sys.path[:] = cleaned
    os.environ["PYTHONNOUSERSITE"] = "1"

    candidates = [
        PROJECT_VENV / "Lib" / "site-packages",
        PROJECT_VENV
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages",
    ]
    for candidate in reversed(candidates):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
