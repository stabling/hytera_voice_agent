# =============================================================================
# 【维护注释】文件：voice_agent/messages/classification.py
# 所属层次：消息分类层（实时热路径）
# 主要职责：仅凭内存 HashSet 判断 CONTROL/USER 与 endpoint_complete。
# 项目内直接依赖：shared_contracts.control_ids
# 主要公开符号：CATEGORY_*, MessageCategory, ClassificationResult, MessageClassifier
# 兼容性：分类禁止 SQLite/HTTP/MCP/LLM/远程配置访问（需求 §2/§9）。
# 修改原则：控制分类以通信端点 ID 为权威依据；origin 只作审计元数据。
# 排障顺序：先看 classification_reason / endpoint_complete，再回溯 ADK 端点解析。
# =============================================================================
"""纯内存消息分类（需求 §5/§8/§9）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from shared_contracts.control_ids import IdentityConfig, classify_endpoints, normalize_endpoint_id

MessageCategory = Literal["user_voice", "user_sms", "control_voice", "control_sms"]

CATEGORY_USER_VOICE: MessageCategory = "user_voice"
CATEGORY_USER_SMS: MessageCategory = "user_sms"
CATEGORY_CONTROL_VOICE: MessageCategory = "control_voice"
CATEGORY_CONTROL_SMS: MessageCategory = "control_sms"

MEDIUM_VOICE = "voice"
MEDIUM_SMS = "sms"


@dataclass(frozen=True)
class ClassificationResult:
    """一次分类的完整结果（可审计、可序列化）。"""

    category: MessageCategory
    source_id: str | None
    target_id: str | None
    group_id: str | None
    endpoint_complete: bool
    classification_reason: str
    origin: str | None = None
    medium: str = MEDIUM_VOICE

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "medium": self.medium,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "group_id": self.group_id,
            "endpoint_complete": self.endpoint_complete,
            "classification_reason": self.classification_reason,
            "origin": self.origin,
        }


class MessageClassifier:
    """内存分类器；构造一次，长期复用，分类 O(1)。"""

    def __init__(self, identity: IdentityConfig) -> None:
        self.identity = identity

    def classify(
        self,
        *,
        medium: str,
        source_id: Any = None,
        target_id: Any = None,
        group_id: Any = None,
        origin: Any = None,
    ) -> ClassificationResult:
        src = normalize_endpoint_id(source_id)
        dst = normalize_endpoint_id(target_id)
        grp = normalize_endpoint_id(group_id)
        is_control, hit_field = classify_endpoints(self.identity, src, dst, grp)
        if is_control:
            category_suffix = (
                CATEGORY_CONTROL_SMS if medium == MEDIUM_SMS else CATEGORY_CONTROL_VOICE
            )
            reason = f"{hit_field}_in_control_ids"
        else:
            category_suffix = (
                CATEGORY_USER_SMS if medium == MEDIUM_SMS else CATEGORY_USER_VOICE
            )
            reason = "no_endpoint_in_control_ids"
        # 身份完整性（需求 §8）：source_id 与 target_id 均非空才算完整；
        # 组呼按 §3 由 ADK 层把 target_id 补为组 ID。
        endpoint_complete = src is not None and dst is not None
        origin_text = normalize_endpoint_id(origin)
        return ClassificationResult(
            category=category_suffix,
            source_id=src,
            target_id=dst,
            group_id=grp,
            endpoint_complete=endpoint_complete,
            classification_reason=reason,
            origin=origin_text,
            medium=MEDIUM_SMS if medium == MEDIUM_SMS else MEDIUM_VOICE,
        )

    def category_for_store(self, result: ClassificationResult) -> str:
        return result.category
