# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/env.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：project_root, load_dotenv, env_text, env_bool, env_int, env_float, env_path
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""语音代理共享的环境变量读取工具。

该模块不依赖 ``python-dotenv``，以便在离线 Windows 环境中也能工作。
系统环境变量优先于 ``.env``，因此部署脚本可以安全覆盖本地默认值。
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """返回代码包根目录。"""

    return Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> Path:
    """加载简单的 ``KEY=VALUE`` 格式文件并返回实际读取路径。

    已存在的系统环境变量不会被覆盖；无效行和读取异常会被忽略，具体配置
    是否完整由上层业务模块负责校验。
    """

    default_path = project_root() / ".env.agent"
    if not default_path.is_file():
        default_path = project_root() / ".env"
    target = path or Path(os.environ.get("VOICE_AGENT_ENV_FILE", str(default_path))).expanduser()
    if not target.is_file():
        return target

    try:
        for raw_line in target.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError:
        pass
    return target


def env_text(name: str, default: str = "") -> str:
    """读取字符串环境变量。"""

    return str(os.environ.get(name, default)).strip()


def env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量，支持常见真值写法。"""

    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    """读取整数；格式错误时返回默认值。"""

    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    """读取浮点数；格式错误时返回默认值。"""

    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def env_path(name: str, default: str | Path) -> Path:
    """读取并展开路径环境变量。"""

    return Path(os.environ.get(name, str(default))).expanduser()
