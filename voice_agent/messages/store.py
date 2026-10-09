# =============================================================================
# 【维护注释】文件：voice_agent/messages/store.py
# 所属层次：归档存储层
# 主要职责：四类消息的物理隔离 SQLite 存储（user_voice/user_sms/control_voice/
#           control_sms），WAL + busy_timeout + 短事务 + 幂等 upsert + 查询。
# 项目内直接依赖：shared_runtime.json_utils
# 主要公开符号：MessageArchiveStore, ArchiveRecord
# 兼容性：语音库只存 ASR 文本与元数据（需求 §13）；Schema 升级遵循
#         “先加字段 → 再建依赖该字段的索引”（需求 §30）。
# 修改原则：Planner 不持有 SQLite 事务；写事务只包含单条 upsert。
# 排障顺序：先看 upsert 返回值与 retry 指标，再查 WAL/busy_timeout 配置。
# =============================================================================
"""四库消息归档存储（需求 §11/§12/§13/§16/§31）。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from shared_runtime.json_utils import canonical_json

logger = logging.getLogger(__name__)

USER_VOICE_DB = "user_voice.sqlite3"
USER_SMS_DB = "user_sms.sqlite3"
CONTROL_VOICE_DB = "control_voice.sqlite3"
CONTROL_SMS_DB = "control_sms.sqlite3"

CATEGORY_TO_DB = {
    "user_voice": USER_VOICE_DB,
    "user_sms": USER_SMS_DB,
    "control_voice": CONTROL_VOICE_DB,
    "control_sms": CONTROL_SMS_DB,
}

_TABLE = "message"

_COLUMNS: tuple[str, ...] = (
    "dedup_key",
    "event_id",
    "request_id",
    "session_id",
    "call_id",
    "source_id",
    "target_id",
    "group_id",
    "channel_id",
    "medium",
    "category",
    "direction",
    "classification_reason",
    "origin",
    "text",
    "text_source",
    "status",
    "endpoint_complete",
    "occurred_at",
    "recorded_at",
    "metadata_json",
)

# 先建表（含全部列）；对旧库按“先加字段 → 再建依赖该字段的索引”的顺序升级。
_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE}(
 dedup_key TEXT PRIMARY KEY,
 event_id TEXT,
 request_id TEXT,
 session_id TEXT,
 call_id TEXT,
 source_id TEXT,
 target_id TEXT,
 group_id TEXT,
 channel_id TEXT,
 medium TEXT NOT NULL,
 category TEXT NOT NULL,
 direction TEXT,
 classification_reason TEXT,
 origin TEXT,
 text TEXT,
 text_source TEXT,
 status TEXT,
 endpoint_complete INTEGER NOT NULL DEFAULT 0,
 occurred_at TEXT,
 recorded_at TEXT NOT NULL,
 metadata_json TEXT
);
"""

_CREATE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_message_call_id ON {_TABLE}(call_id);",
    f"CREATE INDEX IF NOT EXISTS idx_message_source_time ON {_TABLE}(source_id, occurred_at);",
    f"CREATE INDEX IF NOT EXISTS idx_message_target_time ON {_TABLE}(target_id, occurred_at);",
    f"CREATE INDEX IF NOT EXISTS idx_message_occurred ON {_TABLE}(occurred_at);",
)

_BUSY_RETRIES = 8
_BUSY_BACKOFF_SECONDS = 0.05


@dataclass
class ArchiveRecord:
    """一条待归档消息（§12 统一字段）。"""

    category: str
    medium: str
    source_id: str | None = None
    target_id: str | None = None
    group_id: str | None = None
    channel_id: str | None = None
    event_id: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    call_id: str | None = None
    direction: str | None = None
    classification_reason: str | None = None
    origin: str | None = None
    text: str | None = None
    text_source: str | None = None
    status: str | None = None
    endpoint_complete: bool = False
    occurred_at: str | None = None
    recorded_at: str | None = None
    dedup_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def build_dedup_key(self) -> str:
        """确定性去重键（需求 §31）：event_id 优先，其次 request_id+session_id，
        最后对全部身份字段做规范哈希。"""
        if self.dedup_key:
            return self.dedup_key
        if self.event_id:
            return f"event:{self.event_id}"
        if self.request_id and self.session_id:
            return f"req:{self.request_id}:{self.session_id}"
        if self.request_id and self.call_id:
            return f"req:{self.request_id}:{self.call_id}"
        digest = hashlib.sha256(
            canonical_json(
                {
                    "category": self.category,
                    "source_id": self.source_id,
                    "target_id": self.target_id,
                    "group_id": self.group_id,
                    "call_id": self.call_id,
                    "text": self.text,
                    "occurred_at": self.occurred_at,
                }
            ).encode("utf-8")
        ).hexdigest()
        return f"hash:{digest}"

    def to_row(self) -> dict[str, Any]:
        row = {
            "dedup_key": self.build_dedup_key(),
            "event_id": self.event_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "group_id": self.group_id,
            "channel_id": self.channel_id,
            "medium": self.medium,
            "category": self.category,
            "direction": self.direction,
            "classification_reason": self.classification_reason,
            "origin": self.origin,
            "text": self.text,
            "text_source": self.text_source,
            "status": self.status,
            "endpoint_complete": 1 if self.endpoint_complete else 0,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "metadata_json": (
                canonical_json(self.metadata) if self.metadata else None
            ),
        }
        return row


class _ClosingConnection(sqlite3.Connection):
    """退出 with 块时真正关闭连接，避免 Windows 句柄滞留。"""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class MessageArchiveStore:
    """四个物理隔离 SQLite 库的统一入口。

    - WAL + busy_timeout + 短事务（需求 §16）；
    - upsert 以 dedup_key 幂等（需求 §31）；
    - schema 升级先加列、后建索引（需求 §30）。
    """

    def __init__(self, base_dir: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.base_dir = Path(base_dir).expanduser().resolve(strict=False)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._paths = {
            name: self.base_dir / filename
            for name, filename in CATEGORY_TO_DB.items()
        }
        for path in self._paths.values():
            self._ensure_schema(path)

    # ---- 连接与 schema --------------------------------------------------
    def db_path(self, category: str) -> Path:
        try:
            return self._paths[category]
        except KeyError:
            raise ValueError(f"unknown message category: {category!r}") from None

    def _connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    def _ensure_schema(self, path: Path) -> None:
        with self._lock, self._connect(path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_CREATE_TABLE_SQL)
            self._upgrade_columns(connection)
            # 索引必须在列补齐之后创建（需求 §30）。
            for statement in _CREATE_INDEX_SQL:
                connection.execute(statement)

    @staticmethod
    def _existing_columns(connection: sqlite3.Connection) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({_TABLE})")}

    def _upgrade_columns(self, connection: sqlite3.Connection) -> None:
        existing = self._existing_columns(connection)
        for column in _COLUMNS:
            if column in existing:
                continue
            if column == "endpoint_complete":
                definition = "INTEGER NOT NULL DEFAULT 0"
            elif column == "medium":
                definition = "TEXT DEFAULT 'voice'"
            elif column == "category":
                definition = "TEXT DEFAULT 'user_voice'"
            elif column == "recorded_at":
                definition = "TEXT NOT NULL DEFAULT ''"
            else:
                definition = "TEXT"
            connection.execute(
                f"ALTER TABLE {_TABLE} ADD COLUMN {column} {definition}"
            )
            existing.add(column)

    # ---- 写入 -----------------------------------------------------------
    def upsert(self, record: ArchiveRecord) -> bool:
        """幂等写入一条消息；已存在（重放）返回 False。短事务 + BUSY 自动重试。"""
        row = record.to_row()
        columns = ", ".join(_COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        statement = (
            f"INSERT OR IGNORE INTO {_TABLE}({columns}) VALUES ({placeholders})"
        )
        values = [row[name] for name in _COLUMNS]
        last_error: Exception | None = None
        for attempt in range(_BUSY_RETRIES):
            try:
                with self._lock, self._connect(self.db_path(record.category)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        cursor = connection.execute(statement, values)
                        connection.execute("COMMIT")
                        return cursor.rowcount > 0
                    except sqlite3.IntegrityError:
                        connection.execute("ROLLBACK")
                        return False
                    except Exception:
                        connection.execute("ROLLBACK")
                        raise
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" in message or "busy" in message:
                    last_error = exc
                    time.sleep(_BUSY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise
        raise sqlite3.OperationalError(
            f"message archive busy after {_BUSY_RETRIES} retries: {last_error}"
        )

    # ---- 查询 -----------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["endpoint_complete"] = bool(item.get("endpoint_complete"))
        metadata = item.get("metadata_json")
        if metadata:
            try:
                item["metadata"] = json.loads(metadata)
            except json.JSONDecodeError:
                item["metadata"] = None
        else:
            item["metadata"] = None
        item.pop("metadata_json", None)
        item.pop("dedup_key", None)
        return item

    def _query(
        self,
        category: str,
        where: str,
        params: Iterable[Any],
        *,
        order_by: str = "occurred_at DESC, dedup_key DESC",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = (
            f"SELECT * FROM {_TABLE} WHERE {where} "
            f"ORDER BY {order_by} LIMIT {int(limit)}"
        )
        with self._connect(self.db_path(category)) as connection:
            rows = connection.execute(sql, list(params)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count(self, category: str) -> int:
        with self._connect(self.db_path(category)) as connection:
            row = connection.execute(f"SELECT COUNT(*) AS n FROM {_TABLE}").fetchone()
        return int(row["n"])

    def call_records(self, category: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """按时间倒序返回某语音库的全部通话记录（供倒数第 N 通计算）。"""
        return self._query(category, "1=1", [], limit=limit)

    def messages_for_actor(
        self,
        category: str,
        *,
        actor_id: str,
        days: int | None = None,
        limit: int = 50,
        extra_where: str = "",
        extra_params: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        where = "(source_id = ? OR target_id = ?)"
        params: list[Any] = [actor_id, actor_id]
        if days and days > 0:
            # occurred_at 为 ISO 文本；用 julianday 过滤天数窗口。
            where += " AND (occurred_at IS NULL OR julianday(occurred_at) >= julianday('now', ?))"
            params.append(f"-{int(days)} days")
        if extra_where:
            where += f" AND ({extra_where})"
            params.extend(extra_params or [])
        return self._query(category, where, params, limit=limit)

    def find_by_call_id(self, category: str, call_id: str) -> list[dict[str, Any]]:
        return self._query(
            category, "call_id = ?", [call_id], order_by="occurred_at DESC", limit=10
        )

    def recent_sms(
        self,
        *,
        actor_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        contact_id: str | None = None,
        keyword: str | None = None,
        direction: str | None = None,
        source_id: str | None = None,
        target_id: str | None = None,
        group_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where = "(source_id = ? OR target_id = ?)"
        params: list[Any] = [actor_id, actor_id]
        if start_time:
            where += " AND (occurred_at IS NULL OR occurred_at >= ?)"
            params.append(start_time)
        if end_time:
            where += " AND (occurred_at IS NULL OR occurred_at <= ?)"
            params.append(end_time)
        if contact_id:
            where += " AND (source_id = ? OR target_id = ?)"
            params.extend([contact_id, contact_id])
        if keyword:
            where += " AND text LIKE ?"
            params.append(f"%{keyword}%")
        if direction:
            where += " AND direction = ?"
            params.append(direction)
        if source_id:
            where += " AND source_id = ?"
            params.append(source_id)
        if target_id:
            where += " AND target_id = ?"
            params.append(target_id)
        if group_id:
            where += " AND group_id = ?"
            params.append(group_id)
        return self._query("user_sms", where, params, limit=limit)

    def stats(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for category in CATEGORY_TO_DB:
            try:
                result[category] = {
                    "path": str(self.db_path(category)),
                    "count": self.count(category),
                }
            except Exception as exc:  # 单库查询失败不影响其余库上报
                result[category] = {"error": f"{type(exc).__name__}: {exc}"}
        return result

    def close(self) -> None:
        """连接均为按需短连接，无需显式释放；保留接口便于测试钩子。"""
        return None
