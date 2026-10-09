# =============================================================================
# 【维护注释】文件：voice_agent/context/repository.py
# 所属层次：决策上下文层
# 主要职责：负责事件、历史、终端状态等上下文的构建、裁剪、脱敏和快照，为规划器提供受控信息。
# 项目内直接依赖：voice_agent.calls.models, voice_agent.storage.runtime, .models, .redaction
# 主要公开符号：ContextRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Parameterized, permission-scoped history queries and ContextSnapshot storage."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared_runtime.sqlite_compat import ClosingConnection, configure_durable_journal
from voice_agent.calls.models import CallSelector
from voice_agent.storage.runtime import mirror_record

from .models import ContextSnapshot
from .redaction import canonical_json


class ContextRepository:
    """Read-only federation over fact stores plus an independent snapshot table."""

    def __init__(
        self,
        path: str | Path,
        *,
        call_repository: Any | None = None,
        operation_repository: Any | None = None,
        command_repository: Any | None = None,
        event_journal: Any | None = None,
        memory_repository: Any | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.call_repository = call_repository
        self.operation_repository = operation_repository
        self.command_repository = command_repository
        self.event_journal = event_journal
        self.memory_repository = memory_repository
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None, check_same_thread=False,
            factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        migrations = sorted(Path(__file__).with_name("migrations").glob("[0-9][0-9][0-9]_*.sql"))
        if not migrations:
            raise RuntimeError("context migrations are missing")
        with self._lock, self._connect() as connection:
            configure_durable_journal(connection, self.path)
            for migration in migrations:
                connection.executescript(migration.read_text(encoding="utf-8"))

    @staticmethod
    def _after_days(days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat().replace("+00:00", "Z")

    def recent_calls(self, *, terminal_id: str, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
        if self.call_repository is None or not str(terminal_id).strip():
            return []
        selector = CallSelector(
            authorized_terminal_id=str(terminal_id),
            started_after=self._after_days(days),
            limit=max(1, min(int(limit), 200)),
        )
        return list(self.call_repository.list_sessions(selector))

    def call_turns(self, call_id: str) -> list[dict[str, Any]]:
        """Return transcript turns for one call through the context boundary.

        History selection must distinguish real transcript-bearing communication
        from synthetic ``/text/input`` commands.  Keeping that lookup here avoids
        making ``HistoryQueryService`` depend directly on ``CallStoreRepository``
        while preserving the existing permission-scoped recent-call query.
        """

        if self.call_repository is None or not str(call_id).strip():
            return []
        turns = getattr(self.call_repository, "turns", None)
        if not callable(turns):
            return []
        return list(turns(str(call_id)))

    def summarizable_call_ids(self, call_ids: list[str]) -> set[str] | None:
        """Return call ids with real transcript content when bulk lookup exists.

        ``None`` means the configured call repository does not expose the new
        bulk capability, allowing ``HistoryQueryService`` to preserve its
        legacy per-call compatibility path.
        """

        if self.call_repository is None:
            return None
        reader = getattr(self.call_repository, "summarizable_call_ids", None)
        if not callable(reader):
            return None
        return set(reader(call_ids))

    def active_call(self, *, terminal_id: str) -> dict[str, Any] | None:
        if self.call_repository is None or not str(terminal_id).strip():
            return None
        rows = self.call_repository.list_sessions(
            CallSelector(
                authorized_terminal_id=str(terminal_id),
                statuses=["observed", "requested", "ringing", "answered", "active"],
                limit=10,
            )
        )
        return rows[0] if rows else None

    def recent_operations(self, *, actor_id: str, limit: int = 30) -> list[dict[str, Any]]:
        if self.operation_repository is None or not str(actor_id).strip():
            return []
        return list(self.operation_repository.query(actor_id=str(actor_id), limit=limit))

    def similar_outcomes(
        self, *, actor_id: str, operation_type: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        if self.operation_repository is None or not str(actor_id).strip() or not str(operation_type).strip():
            return []
        return list(
            self.operation_repository.query(
                actor_id=str(actor_id), operation_type=str(operation_type), limit=limit
            )
        )

    def open_tasks(self, *, actor_id: str, limit: int = 30) -> list[dict[str, Any]]:
        if self.command_repository is None or not str(actor_id).strip():
            return []
        statuses = ("CREATED", "CLASSIFIED", "PLANNED", "RUNNING", "WAITING")
        query_statuses = getattr(self.command_repository, "query_statuses", None)
        if callable(query_statuses):
            return list(
                query_statuses(
                    actor_id=str(actor_id),
                    statuses=statuses,
                    limit=max(1, int(limit)),
                )
            )

        # Compatibility fallback for existing mocks/adapters that only expose
        # the original single-status ``query`` method.
        values: list[dict[str, Any]] = []
        for status in statuses:
            values.extend(self.command_repository.query(actor_id=str(actor_id), status=status, limit=limit))
        values.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("command_id") or "")), reverse=True)
        return values[: max(1, int(limit))]

    def entity_timeline(
        self, *, entity_id: str, terminal_id: str, actor_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Union only predefined parameterized queries; callers cannot provide SQL."""
        entity = str(entity_id).strip()
        if not entity:
            return []
        rows: list[dict[str, Any]] = []
        if self.call_repository is not None and str(terminal_id).strip():
            calls = self.call_repository.list_sessions(
                CallSelector(
                    authorized_terminal_id=str(terminal_id), contact_id=entity,
                    limit=max(1, min(int(limit), 200)),
                )
            )
            rows.extend({"kind": "call", "occurred_at": row.get("requested_at") or row.get("created_at"), **row} for row in calls)
        if self.operation_repository is not None and str(actor_id).strip():
            operations = self.operation_repository.query(actor_id=str(actor_id), target_id=entity, limit=limit)
            rows.extend({"kind": "operation", "occurred_at": row.get("created_at"), **row} for row in operations)
        rows.sort(key=lambda row: (str(row.get("occurred_at") or ""), str(row.get("call_id") or row.get("operation_id") or "")), reverse=True)
        return rows[: max(1, int(limit))]

    def verified_memories(
        self,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
        query: str | None = None,
        limit: int = 30,
        use_semantic: bool = False,
    ) -> list[dict[str, Any]]:
        if self.memory_repository is None:
            return []
        return self.memory_repository.search(
            tenant_id=tenant_id,
            actor_id=actor_id,
            terminal_id=terminal_id,
            query=query,
            statuses=("VERIFIED",),
            limit=limit,
            use_fts=use_semantic,
        )

    def save_snapshot(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        values = snapshot.model_dump(mode="json")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO context_snapshot(
                    snapshot_id,schema_version,command_id,actor_id,terminal_id,tenant_id,
                    item_ids_json,event_ids_json,memory_ids_json,content_hash,context_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    values["snapshot_id"], values["schema_version"], values["command_id"],
                    values["actor_id"], values["terminal_id"], values["tenant_id"],
                    canonical_json(values["item_ids"]), canonical_json(values["event_ids"]),
                    canonical_json(values["memory_ids"]), values["content_hash"],
                    canonical_json(values["context"]), values["created_at"],
                ),
            )
        mirror_record(
            "context_snapshot", snapshot.snapshot_id, values,
            tenant_id=snapshot.tenant_id, actor_id=snapshot.actor_id, terminal_id=snapshot.terminal_id,
        )
        return snapshot

    def get_snapshot(
        self,
        snapshot_id: str,
        *,
        actor_id: str | None = None,
        terminal_id: str | None = None,
        tenant_id: str | None = None,
    ) -> ContextSnapshot | None:
        clauses = ["snapshot_id=?"]
        params: list[Any] = [str(snapshot_id)]
        for column, value in (("actor_id", actor_id), ("terminal_id", terminal_id), ("tenant_id", tenant_id)):
            if value is not None:
                clauses.append(f"({column} IS NULL OR {column}=?)")
                params.append(str(value))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM context_snapshot WHERE " + " AND ".join(clauses), params
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        return ContextSnapshot.model_validate(
            {
                "schema_version": data["schema_version"],
                "snapshot_id": data["snapshot_id"],
                "command_id": data["command_id"],
                "actor_id": data["actor_id"],
                "terminal_id": data["terminal_id"],
                "tenant_id": data["tenant_id"],
                "item_ids": json.loads(data["item_ids_json"]),
                "event_ids": json.loads(data["event_ids_json"]),
                "memory_ids": json.loads(data["memory_ids_json"]),
                "content_hash": data["content_hash"],
                "context": json.loads(data["context_json"]),
                "created_at": data["created_at"],
            }
        )

    def replay(self, snapshot_id: str, **scope: Any) -> dict[str, Any] | None:
        snapshot = self.get_snapshot(snapshot_id, **scope)
        if snapshot is None:
            return None
        payload = snapshot.context.planner_payload()
        return {
            "snapshot_id": snapshot.snapshot_id,
            "content_hash": snapshot.content_hash,
            "recomputed_hash": snapshot.context.content_hash(),
            "matches": snapshot.content_hash == snapshot.context.content_hash(),
            "context": payload,
        }
