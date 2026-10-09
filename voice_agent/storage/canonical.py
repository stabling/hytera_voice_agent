# =============================================================================
# 【维护注释】文件：voice_agent/storage/canonical.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：redact, canonical_json, content_hash
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Canonical serialization/hashing shared by migration and reconciliation."""
from __future__ import annotations
import hashlib
import json
from typing import Any

SENSITIVE_KEYS = {"authorization","api_key","apikey","access_token","token","secret","password","audio_payload_b64","audio_b64"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SENSITIVE_KEYS or any(t in lowered for t in ("password","secret","token","base64","b64")):
                out[str(key)] = "<redacted>"
            else:
                out[str(key)] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


def canonical_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
