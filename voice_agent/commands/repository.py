# =============================================================================
# 【维护注释】文件：voice_agent/commands/repository.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.models, voice_agent.storage.runtime, voice_agent.operations.schema, .models
# 主要公开符号：CommandRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""SQLite WAL persistence for Command records."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from shared_runtime.sqlite_compat import configure_durable_journal
from shared_contracts.models import utc_now_iso
from voice_agent.storage.runtime import mirror_record
from voice_agent.operations.schema import SCHEMA_SQL

from .models import COMMAND_STATUSES, Command


class _ClosingConnection(sqlite3.Connection):
    """退出 with 块时真正关闭连接（同 operations.repository._ClosingConnection）。"""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class CommandRepository:
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
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            configure_durable_journal(connection, self.path)
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO operation_migration_version(component,version,applied_at) VALUES(?,?,?)",
                ("command", 1, utc_now_iso()),
            )

    def create_or_get(self, command: Command) -> tuple[bool, Command]:
        values = command.model_dump(mode="json")
        payload_json = json.dumps(values.pop("payload_redacted"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        columns = [*values.keys(), "payload_redacted_json"]
        params = [values[name] for name in values] + [payload_json]
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = None
                if command.source_ref:
                    existing = connection.execute(
                        "SELECT * FROM command_record WHERE source_type=? AND source_ref=?",
                        (command.source_type, command.source_ref),
                    ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return False, self._row(existing)
                connection.execute(
                    f"INSERT INTO command_record({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    params,
                )
                row = connection.execute(
                    "SELECT * FROM command_record WHERE command_id=?", (command.command_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert row is not None
        stored = self._row(row)
        mirror_record("command", stored.command_id, stored.model_dump(mode="json", exclude_none=True), actor_id=stored.actor_id)
        return True, stored

    def get(self, command_id: str) -> Command | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM command_record WHERE command_id=?", (str(command_id),)
            ).fetchone()
        return None if row is None else self._row(row)

    def get_by_source(self, source_type: str, source_ref: str) -> Command | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM command_record WHERE source_type=? AND source_ref=?",
                (str(source_type), str(source_ref)),
            ).fetchone()
        return None if row is None else self._row(row)

    def update(
        self,
        command_id: str,
        *,
        status: str | None = None,
        raw_text: str | None = None,
        cancel_reason: str | None = None,
        completed: bool = False,
    ) -> Command | None:
        if status is not None and str(status) not in COMMAND_STATUSES:
            raise ValueError(f"invalid command status: {status}")
        updates: list[str] = ["updated_at=?"]
        params: list[Any] = [utc_now_iso()]
        if status is not None:
            updates.append("status=?")
            params.append(str(status))
        if raw_text is not None:
            updates.append("raw_text=?")
            params.append(str(raw_text)[:20_000])
        if cancel_reason is not None:
            updates.append("cancel_reason=?")
            params.append(str(cancel_reason)[:512])
        if completed:
            updates.append("completed_at=?")
            params.append(utc_now_iso())
        params.append(str(command_id))
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE command_record SET {','.join(updates)} WHERE command_id=?", params
            )
        stored = self.get(command_id)
        if stored is not None:
            mirror_record("command", stored.command_id, stored.model_dump(mode="json", exclude_none=True), actor_id=stored.actor_id)
        return stored

    def query(
        self,
        *,
        actor_id: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("actor_id", actor_id), ("source_type", source_type), ("status", status)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(str(value))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM command_record" + where + " ORDER BY created_at DESC, command_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._public_dict(row) for row in rows]

    def query_statuses(
        self,
        *,
        actor_id: str | None = None,
        statuses: Iterable[str],
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query multiple statuses with one SQL statement.

        This intentionally does not reinterpret or validate the supplied status
        values.  Callers that previously issued one ``query(status=...)`` per
        status therefore keep exactly the same matching semantics while
        avoiding repeated SQLite round-trips.
        """

        normalized_statuses = tuple(
            dict.fromkeys(str(value) for value in statuses if str(value).strip())
        )
        if not normalized_statuses:
            return []

        clauses: list[str] = []
        params: list[Any] = []
        if actor_id is not None:
            clauses.append("actor_id=?")
            params.append(str(actor_id))

        placeholders = ",".join("?" for _ in normalized_statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(normalized_statuses)
        where = " WHERE " + " AND ".join(clauses)
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM command_record"
                + where
                + " ORDER BY created_at DESC, command_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._public_dict(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> Command:
        data = dict(row)
        data["payload_redacted"] = json.loads(data.pop("payload_redacted_json") or "{}")
        return Command.model_validate(data)

    @staticmethod
    def _public_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload_redacted"] = json.loads(data.pop("payload_redacted_json") or "{}")
        # Raw text is operationally sensitive; list APIs expose only its length.
        raw = data.pop("raw_text", None)
        data["raw_text_length"] = len(raw or "")
        return data
