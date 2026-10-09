# =============================================================================
# 【维护注释】文件：shared_runtime/event_journal.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：shared_contracts.event_models
# 主要公开符号：JournalAppendResult, EventJournal
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""SQLite WAL append-only event journal."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sqlite_compat import ClosingConnection, configure_durable_journal
from shared_contracts.event_models import DomainEvent, EventQuery, upcast_event_payload
from shared_runtime.json_utils import canonical_json


@dataclass(frozen=True)
class JournalAppendResult:
    event_id: str
    inserted: bool
    duplicate: bool


_SENSITIVE_KEYS = {
    "audio_payload_b64",
    "audio_b64",
    "udp_payload_b64",
    "authorization",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "secret",
    "password",
}
_QUERY_REDACT_KEYS = _SENSITIVE_KEYS | {"content", "text", "transcript", "raw_text"}


def _redact(value: Any, *, query_mode: bool = False) -> Any:
    keys = _QUERY_REDACT_KEYS if query_mode else _SENSITIVE_KEYS
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in keys or any(token in lowered for token in ("base64", "b64", "token", "secret", "password")):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(item, query_mode=query_mode)
        return redacted
    if isinstance(value, list):
        return [_redact(item, query_mode=query_mode) for item in value]
    return value


class EventJournal:
    """One-row-per-fact event store. Existing rows are never updated."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
            factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "voice_agent"
            / "context"
            / "migrations"
            / "001_event_log.sql"
        )
        sql = migration.read_text(encoding="utf-8")
        with self._lock, self._connect() as connection:
            configure_durable_journal(connection, self.path)
            connection.executescript(sql)
            connection.execute(
                "INSERT OR REPLACE INTO event_journal_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )

    def append(self, event: DomainEvent | dict[str, Any]) -> JournalAppendResult:
        domain_event = event if isinstance(event, DomainEvent) else DomainEvent.model_validate(event)
        stored = domain_event.model_dump(mode="json", exclude_none=True)
        stored["data"] = _redact(stored.get("data") or {})
        payload_json = canonical_json(stored)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO event_log(
                        event_id,event_type,schema_version,occurred_at,recorded_at,
                        source_service,trace_id,correlation_id,causation_id,command_id,
                        operation_id,call_id,actor_id,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        domain_event.event_id,
                        domain_event.event_type,
                        domain_event.schema_version,
                        domain_event.occurred_at,
                        domain_event.recorded_at,
                        domain_event.source_service,
                        domain_event.trace_id,
                        domain_event.correlation_id,
                        domain_event.causation_id,
                        domain_event.command_id,
                        domain_event.operation_id,
                        domain_event.call_id,
                        domain_event.actor_id,
                        payload_json,
                        payload_hash,
                    ),
                )
                inserted = int(cursor.rowcount) == 1
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return JournalAppendResult(
            event_id=domain_event.event_id,
            inserted=inserted,
            duplicate=not inserted,
        )

    def get(self, event_id: str, *, redacted: bool = True) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_log WHERE event_id=?", (str(event_id),)
            ).fetchone()
        return None if row is None else self._row_to_event(row, redacted=redacted)

    def query(self, query: EventQuery | dict[str, Any]) -> list[dict[str, Any]]:
        q = query if isinstance(query, EventQuery) else EventQuery.model_validate(query)
        clauses: list[str] = []
        params: list[Any] = []
        for column in ("event_id", "trace_id", "correlation_id", "call_id", "event_type"):
            value = getattr(q, column)
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT * FROM event_log"
            + where
            + " ORDER BY occurred_at ASC, event_id ASC LIMIT ? OFFSET ?"
        )
        params.extend([q.limit, q.offset])
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_event(row, redacted=True) for row in rows]

    def read_after_offset(self, offset: int, *, limit: int = 200) -> list[dict[str, Any]]:
        """Read unredacted events by stable SQLite rowid for local projectors."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rowid AS journal_offset, * FROM event_log "
                "WHERE rowid>? ORDER BY rowid ASC LIMIT ?",
                (max(0, int(offset)), max(1, int(limit))),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            event = self._row_to_event(row, redacted=False)
            event["journal_offset"] = int(row["journal_offset"])
            values.append(event)
        return values

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at, "
                "COALESCE(MAX(rowid),0) AS max_offset FROM event_log"
            ).fetchone()
        return {
            "path": str(self.path),
            "total": int(row["total"]),
            "first_at": row["first_at"],
            "last_at": row["last_at"],
            "max_offset": int(row["max_offset"]),
            "schema_version": self.SCHEMA_VERSION,
        }

    @staticmethod
    def _row_to_event(row: sqlite3.Row, *, redacted: bool) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload = upcast_event_payload(payload)
        if redacted:
            payload["data"] = _redact(payload.get("data") or {}, query_mode=True)
        payload["payload_hash"] = str(row["payload_hash"])
        return payload
