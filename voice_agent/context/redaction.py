# =============================================================================
# 【维护注释】文件：voice_agent/context/redaction.py
# 所属层次：决策上下文层
# 主要职责：负责事件、历史、终端状态等上下文的构建、裁剪、脱敏和快照，为规划器提供受控信息。
# 项目内直接依赖：.models
# 主要公开符号：sanitize_untrusted_text, redact_payload, canonical_json
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Centralized field-level redaction and prompt-injection containment."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from shared_runtime.json_utils import canonical_json

from .models import Sensitivity

_SECRET_KEYS = {
    "audio", "audio_path", "audio_payload", "audio_payload_b64", "audio_b64",
    "authorization", "token", "api_key", "secret", "password",
}
_RESTRICTED_KEYS = {"transcript", "content", "raw_text", "text", "identity_mapping"}
_INSTRUCTION_RE = re.compile(
    r"(?i)(ignore\s+(all\s+)?previous|system\s+prompt|developer\s+message|"
    r"绕过|忽略.{0,8}(指令|规则)|系统提示|执行以下命令)"
)
_LEVEL = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.RESTRICTED: 2,
    Sensitivity.SECRET: 3,
}


def sanitize_untrusted_text(value: str) -> tuple[str, bool]:
    """Neutralize instruction-like history while retaining an auditable digest."""
    text = str(value)
    if not _INSTRUCTION_RE.search(text):
        return text, False
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"<untrusted-instruction-like-content sha256={digest}>", True


def redact_payload(
    value: Any,
    *,
    sensitivity: Sensitivity,
    access_level: Sensitivity,
    sanitize_untrusted: bool = True,
    allow_restricted_fields: bool = False,
) -> tuple[Any, bool, bool]:
    """Return payload, redaction flag, and untrusted-content flag."""
    if _LEVEL[sensitivity] > _LEVEL[access_level]:
        return {"redacted": True, "reason": "insufficient_access"}, True, False

    redacted = False
    untrusted = False

    def visit(item: Any, key: str | None = None) -> Any:
        nonlocal redacted, untrusted
        lowered = (key or "").lower()
        if lowered in _SECRET_KEYS or any(token in lowered for token in ("password", "secret", "token", "base64")):
            redacted = True
            return "<redacted>"
        if (
            lowered in _RESTRICTED_KEYS
            and not allow_restricted_fields
            and _LEVEL[access_level] < _LEVEL[Sensitivity.RESTRICTED]
        ):
            redacted = True
            if isinstance(item, str):
                return {"redacted": True, "sha256": hashlib.sha256(item.encode("utf-8")).hexdigest()}
            return "<redacted>"
        if isinstance(item, dict):
            return {str(k): visit(v, str(k)) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(v, key) for v in item]
        if isinstance(item, str) and sanitize_untrusted:
            cleaned, flagged = sanitize_untrusted_text(item)
            untrusted = untrusted or flagged
            return cleaned
        if isinstance(item, str):
            return item
        return item

    return visit(value), redacted, untrusted
