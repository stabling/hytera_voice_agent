# =============================================================================
# 【维护注释】文件：voice_agent/messages/__init__.py
# 所属层次：消息身份/分类/归档/历史子系统
# 主要职责：四类消息（user_voice/user_sms/control_voice/control_sms）的内存分类、
#           异步四库归档、用户历史查询与旧库迁移。
# 项目内直接依赖：shared_contracts.control_ids
# 主要公开符号：MessageCategory, MessageClassifier, MessageArchiveStore,
#               ArchiveWriter, MessageArchiveService, UserHistoryService
# 兼容性：归档与 Agent 主链解耦（Archive Writer 独立线程）；分类只做内存集合查询。
# 修改原则：分类层禁止 I/O；归档层负责 retry/dedup/migration；历史层强制 scope=user。
# 排障顺序：先看 ClassificationResult.classification_reason，再看 ArchiveWriter 指标。
# =============================================================================
"""消息分类、归档与用户历史子系统。"""

from .classification import (
    CATEGORY_CONTROL_SMS,
    CATEGORY_CONTROL_VOICE,
    CATEGORY_USER_SMS,
    CATEGORY_USER_VOICE,
    MEDIUM_SMS,
    MEDIUM_VOICE,
    ClassificationResult,
    MessageClassifier,
)
from .history import UserHistoryService
from .metrics import MessageMetrics
from .migration import migrate_legacy_databases
from .service import MessageArchiveService, get_message_archive_service
from .store import MessageArchiveStore
from .writer import ArchiveWriter

__all__ = [
    "CATEGORY_CONTROL_SMS",
    "CATEGORY_CONTROL_VOICE",
    "CATEGORY_USER_SMS",
    "CATEGORY_USER_VOICE",
    "ArchiveWriter",
    "ClassificationResult",
    "MEDIUM_SMS",
    "MEDIUM_VOICE",
    "MessageArchiveService",
    "MessageArchiveStore",
    "MessageClassifier",
    "MessageMetrics",
    "UserHistoryService",
    "get_message_archive_service",
    "migrate_legacy_databases",
]
