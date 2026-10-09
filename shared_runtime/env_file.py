# =============================================================================
# 【维护注释】文件：shared_runtime/env_file.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：load_env_file
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Dependency-free KEY=VALUE environment-file loader."""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        return target
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return target
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return target
