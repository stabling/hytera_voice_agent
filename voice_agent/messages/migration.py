# =============================================================================
# 【维护注释】文件：voice_agent/messages/migration.py
# 所属职责：旧双库（user_messages.sqlite3 / control_messages.sqlite3）到
#           四库的幂等迁移（需求 §29）。
# 项目内直接依赖：.classification, .store
# 主要公开符号：migrate_legacy_databases, migrate_legacy_database
# 兼容性：只读旧库、不覆盖；重复执行不产生重复记录（dedup_key 幂等）。
# 修改原则：自动兼容旧 schema；自动补 endpoint_complete；正确迁移 call_id。
# 排障顺序：先看返回的 per-db 统计，再对照旧库列映射。
# =============================================================================
"""旧双库 → 四库迁移（需求 §29）。"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from shared_contracts.control_ids import normalize_endpoint_id

from .classification import MEDIUM_SMS, MEDIUM_VOICE, MessageClassifier
from .store import ArchiveRecord, MessageArchiveStore

logger = logging.getLogger(__name__)

LEGACY_USER_DB = "user_messages.sqlite3"
LEGACY_CONTROL_DB = "control_messages.sqlite3"

# 旧库可能的列名兼容映射。
_MEDIUM_KEYS = ("medium", "message_type", "type", "kind")
_TEXT_KEYS = ("text", "content", "body", "message")
_ORIGIN_KEYS = ("origin", "source_type")
_STATUS_KEYS = ("status", "state")
_TIME_KEYS = ("occurred_at", "created_at", "timestamp", "received_at", "recorded_at")
_DIRECTION_KEYS = ("direction", "dir")
_EVENT_ID_KEYS = ("event_id", "id", "message_id")
_REQUEST_KEYS = ("request_id", "req_id")
_SESSION_KEYS = ("session_id", "session")
_CALL_KEYS = ("call_id",)
_SOURCE_KEYS = ("source_id", "src_id", "sender_id", "from_id")
_TARGET_KEYS = ("target_id", "dst_id", "dest_id", "receiver_id", "to_id")
_GROUP_KEYS = ("group_id", "group")
_CHANNEL_KEYS = ("channel_id", "channel")


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _medium_of(row: dict[str, Any]) -> str:
    value = str(_first_value(row, _MEDIUM_KEYS) or "").strip().lower()
    if value in {"sms", "text_message", "textmessage", "tmp"}:
        return MEDIUM_SMS
    return MEDIUM_VOICE


def _rows_from_legacy(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not tables:
            return []
        table = next(
            (name for name in tables if name != "sqlite_sequence"),
            None,
        )
        if table is None:
            return []
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    finally:
        connection.close()


def migrate_legacy_database(
    store: MessageArchiveStore,
    classifier: MessageClassifier,
    legacy_path: Path,
    *,
    expected_role: str,
) -> dict[str, Any]:
    """把一个旧库迁移到四库。

    ``expected_role``: ``user`` / ``control`` —— 旧库整体角色；
    最终 category 仍以端点 ID 分类为权威（需求 §3），仅在端点信息缺失时
    回退到旧库角色。
    """
    if not legacy_path.exists():
        return {"source": str(legacy_path), "exists": False, "migrated": 0, "skipped": 0}

    rows = _rows_from_legacy(legacy_path)
    migrated = 0
    skipped = 0
    for row in rows:
        medium = _medium_of(row)
        source_id = normalize_endpoint_id(_first_value(row, _SOURCE_KEYS))
        target_id = normalize_endpoint_id(_first_value(row, _TARGET_KEYS))
        group_id = normalize_endpoint_id(_first_value(row, _GROUP_KEYS))
        result = classifier.classify(
            medium=medium,
            source_id=source_id,
            target_id=target_id,
            group_id=group_id,
            origin=_first_value(row, _ORIGIN_KEYS),
        )
        # 端点信息完全缺失的旧记录：按旧库角色回退。
        if source_id is None and target_id is None and group_id is None:
            if expected_role == "control":
                category = "control_sms" if medium == MEDIUM_SMS else "control_voice"
            else:
                category = "user_sms" if medium == MEDIUM_SMS else "user_voice"
        else:
            category = result.category
        record = ArchiveRecord(
            category=category,
            medium=medium,
            source_id=source_id,
            target_id=target_id,
            group_id=group_id,
            channel_id=normalize_endpoint_id(_first_value(row, _CHANNEL_KEYS)),
            event_id=(
                normalize_endpoint_id(_first_value(row, _EVENT_ID_KEYS))
                if "event_id" in row
                else None
            ),
            request_id=normalize_endpoint_id(_first_value(row, _REQUEST_KEYS)),
            session_id=normalize_endpoint_id(_first_value(row, _SESSION_KEYS)),
            call_id=normalize_endpoint_id(_first_value(row, _CALL_KEYS)),
            direction=(
                str(_first_value(row, _DIRECTION_KEYS) or "")
                or ("outbound" if source_id is None else "inbound")
            ),
            classification_reason=f"legacy_{expected_role}_migration",
            origin=_first_value(row, _ORIGIN_KEYS),
            text=(
                str(_first_value(row, _TEXT_KEYS) or "") or None
            ),
            text_source=("sms" if medium == MEDIUM_SMS else "asr"),
            status=str(_first_value(row, _STATUS_KEYS) or "") or "migrated",
            endpoint_complete=bool(source_id is not None and target_id is not None),
            occurred_at=(
                str(_first_value(row, _TIME_KEYS) or "") or None
            ),
            recorded_at=str(_first_value(row, _TIME_KEYS) or "") or None,
            metadata={"legacy_source": legacy_path.name},
        )
        if record.event_id is None and "id" in row:
            record.event_id = normalize_endpoint_id(row.get("event_id") or row.get("id"))
        try:
            inserted = store.upsert(record)
        except Exception as exc:
            skipped += 1
            logger.warning(
                "legacy migration row failed source=%s error=%s",
                legacy_path.name,
                exc,
            )
            continue
        migrated += int(inserted)
        skipped += int(not inserted)
    return {
        "source": str(legacy_path),
        "exists": True,
        "rows": len(rows),
        "migrated": migrated,
        "skipped": skipped,
    }


def migrate_legacy_databases(
    store: MessageArchiveStore,
    classifier: MessageClassifier,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """迁移 base_dir 下的旧双库（幂等，可重复执行）。"""
    root = Path(base_dir) if base_dir is not None else store.base_dir
    results = {
        "user_messages": migrate_legacy_database(
            store, classifier, root / LEGACY_USER_DB, expected_role="user"
        ),
        "control_messages": migrate_legacy_database(
            store, classifier, root / LEGACY_CONTROL_DB, expected_role="control"
        ),
    }
    return {"ok": True, "sources": results}
