# =============================================================================
# 【维护注释】文件：voice_agent/memory/repository.py
# 所属层次：长期记忆层
# 主要职责：负责记忆候选、检索、验证、向量/全文索引与生命周期；记忆不能直接触发设备命令。
# 项目内直接依赖：shared_contracts.models, voice_agent.storage.runtime, shared_runtime.json_utils, .models
# 主要公开符号：MemoryRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""SQLite repository enforcing memory lifecycle, evidence, permissions and retention."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from shared_runtime.sqlite_compat import ClosingConnection, configure_durable_journal
from shared_contracts.models import utc_now_iso
from voice_agent.storage.runtime import mirror_record

from shared_runtime.json_utils import canonical_json
from .models import (
    MemoryItem,
    MemoryStatus,
    MemoryTransition,
    MemoryType,
    transition_allowed,
)

_VERIFY_ROLES = {"memory_verifier", "admin"}
_DISPUTE_ROLES = {"memory_verifier", "privacy_officer", "admin", "subject_owner"}
_RETIRE_ROLES = {"privacy_officer", "admin"}

# 迁移自愈用的 DDL 产物提取：名字兼容 "name" / [name] / `name` / 裸标识符。
_SQL_NAME_PATTERN = r'"[^"]+"|\[[^\]]+\]|`[^`]+`|[\w.]+'
_ALTER_ADD_COLUMN_RE = re.compile(
    rf"ALTER\s+TABLE\s+(?P<table>{_SQL_NAME_PATTERN})\s+ADD\s+(?:COLUMN\s+)?(?P<column>{_SQL_NAME_PATTERN})",
    re.IGNORECASE,
)
_CREATE_OBJECT_RE = re.compile(
    rf"CREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?(?P<kind>TABLE|INDEX|TRIGGER|VIEW)\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_SQL_NAME_PATTERN})",
    re.IGNORECASE,
)
_SQLITE_MASTER_KINDS = {"TABLE": "table", "INDEX": "index", "TRIGGER": "trigger", "VIEW": "view"}


def _unquote_sql_name(name: str) -> str:
    text = str(name).strip()
    if len(text) >= 2 and (
        (text.startswith('"') and text.endswith('"'))
        or (text.startswith("[") and text.endswith("]"))
        or (text.startswith("`") and text.endswith("`"))
    ):
        return text[1:-1]
    return text


def _iter_sql_statements(sql_text: str) -> Iterator[str]:
    """按 sqlite 解析器语义切分迁移脚本为单条完整语句。"""
    buffer = ""
    for line in sql_text.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer
            buffer = ""
    if buffer.strip():
        yield buffer


def _statement_artifacts(statement: str) -> list[tuple[str, str, str]]:
    """提取单条 DDL 语句必须产生的 schema 产物。

    列新增记为 ("column", 表名, 列名)；对象创建记为 (sqlite_master 类型,
    名称, "")。无 schema 产物的语句（PRAGMA、INSERT OR IGNORE 等）返回空
    列表——按迁移契约它们自身必须幂等，重放时直接执行。
    """
    artifacts: list[tuple[str, str, str]] = []
    create = _CREATE_OBJECT_RE.search(statement)
    if create:
        kind = _SQLITE_MASTER_KINDS[create.group("kind").upper()]
        artifacts.append((kind, _unquote_sql_name(create.group("name")), ""))
    alter = _ALTER_ADD_COLUMN_RE.search(statement)
    if alter:
        artifacts.append(
            ("column", _unquote_sql_name(alter.group("table")), _unquote_sql_name(alter.group("column")))
        )
    return artifacts


def _artifact_exists(connection: sqlite3.Connection, artifact: tuple[str, str, str]) -> bool:
    kind, name, column = artifact
    if kind == "column":
        quoted = name.replace('"', '""')
        rows = connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()
        return any(str(row[1]) == column for row in rows)
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return row is not None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class MemoryRepository:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._fts_available = False
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
            raise RuntimeError("memory migrations are missing")
        with self._lock, self._connect() as connection:
            configure_durable_journal(connection, self.path)
            # 版号表先于所有迁移存在（001 亦会幂等创建）。迁移脚本可能包含
            # 非幂等语句（如 002 的 ALTER TABLE ADD COLUMN），版本号已记录且
            # 产物齐备才跳过；产物缺失时容错重放以自愈半迁移/脏版本号库。
            connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_migration_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)"
            )
            applied = {
                int(row[0])
                for row in connection.execute("SELECT version FROM memory_migration_version")
            }
            for migration in migrations:
                version = int(migration.name.split("_", 1)[0])
                statements = [
                    (statement, _statement_artifacts(statement))
                    for statement in _iter_sql_statements(migration.read_text(encoding="utf-8"))
                ]
                if version in applied and all(
                    _artifact_exists(connection, artifact)
                    for _, artifacts in statements
                    for artifact in artifacts
                ):
                    continue
                # 半迁移库（ALTER 已生效但版本号未写入）与脏版本号库（版本号
                # 在但列缺失）都必须能自愈：按语句重放，已存在的 schema 产物
                # 直接跳过，避免 ALTER TABLE ADD COLUMN 撞 "duplicate column
                # name" 阻断启动。
                for statement, artifacts in statements:
                    if any(not _artifact_exists(connection, artifact) for artifact in artifacts):
                        connection.execute(statement)
                # 迁移文件内自带 INSERT OR IGNORE；这里再补一次以防未来
                # 新迁移忘记写版本记录导致反复重放。
                connection.execute(
                    "INSERT OR IGNORE INTO memory_migration_version(version, applied_at, description) "
                    "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
                    (version, migration.stem),
                )
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, summary, content, predicate, object_text)"
                )
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    def create(self, item: MemoryItem | dict[str, Any]) -> MemoryItem:
        memory = item if isinstance(item, MemoryItem) else MemoryItem.model_validate(item)
        if memory.verification_status == MemoryStatus.VERIFIED and memory.source_type not in {"directory", "human"}:
            raise ValueError("model/extractor output cannot be created directly as VERIFIED")
        values = memory.model_dump(mode="json")
        row = {
            **{key: values[key] for key in (
                "memory_id", "schema_version", "namespace", "memory_type", "tenant_id",
                "actor_id", "terminal_id", "subject_entity_id", "predicate", "summary",
                "content", "confidence", "verification_status", "valid_from", "valid_to",
                "expires_at", "source_type", "source_model", "source_operation_id",
                "source_call_id", "sensitivity", "created_at", "updated_at",
            )},
            "object_json": canonical_json(values["object"]),
            "evidence_event_ids_json": canonical_json(values["evidence_event_ids"]),
            "provenance_json": canonical_json(values["provenance"]),
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = list(row)
                connection.execute(
                    f"INSERT INTO memory_item({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
                if self._fts_available:
                    connection.execute(
                        "INSERT INTO memory_fts(memory_id,summary,content,predicate,object_text) VALUES(?,?,?,?,?)",
                        (memory.memory_id, memory.summary, memory.content, memory.predicate or "", memory.object_json),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        stored = self.get(memory.memory_id)
        assert stored is not None
        payload = stored.model_dump(mode="json", exclude_none=True)
        mirror_record("memory", stored.memory_id, payload, tenant_id=stored.tenant_id, actor_id=stored.actor_id, terminal_id=stored.terminal_id)
        return stored

    def get(self, memory_id: str) -> MemoryItem | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_item WHERE memory_id=?", (str(memory_id),)).fetchone()
        return None if row is None else self._row(row)

    def get_authorized(
        self,
        memory_id: str,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
    ) -> MemoryItem | None:
        """Return the internal model only for trusted in-process callers.

        HTTP/MCP query paths must use :meth:`get_authorized_public` so restricted
        content is field-redacted before it leaves the repository boundary.
        """
        rows = self.search(
            tenant_id=tenant_id, actor_id=actor_id, terminal_id=terminal_id,
            memory_ids=(str(memory_id),), statuses=tuple(status.value for status in MemoryStatus),
            limit=1,
        )
        return None if not rows else self.get(str(rows[0]["memory_id"]))

    def get_authorized_public(
        self,
        memory_id: str,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
    ) -> dict[str, Any] | None:
        rows = self.search(
            tenant_id=tenant_id, actor_id=actor_id, terminal_id=terminal_id,
            memory_ids=(str(memory_id),), statuses=tuple(status.value for status in MemoryStatus),
            limit=1,
        )
        return None if not rows else rows[0]

    @staticmethod
    def _scope_clause(
        *, tenant_id: str | None, actor_id: str | None, terminal_id: str | None
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("(tenant_id IS NULL OR tenant_id=?)")
            params.append(str(tenant_id))
        else:
            clauses.append("tenant_id IS NULL")
        identity_values = [str(value) for value in (actor_id, terminal_id) if value is not None and str(value).strip()]
        if identity_values:
            placeholders = ",".join("?" for _ in identity_values)
            clauses.append(
                f"(actor_id IS NULL AND terminal_id IS NULL OR actor_id IN ({placeholders}) OR terminal_id IN ({placeholders}))"
            )
            params.extend(identity_values)
            params.extend(identity_values)
        else:
            clauses.append("actor_id IS NULL AND terminal_id IS NULL")
        return clauses, params

    def search(
        self,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        terminal_id: str | None,
        query: str | None = None,
        statuses: Iterable[str] = ("VERIFIED",),
        memory_types: Iterable[str] | None = None,
        namespace: str | None = None,
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        memory_ids: Iterable[str] | None = None,
        limit: int = 30,
        offset: int = 0,
        use_fts: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = self._scope_clause(
            tenant_id=tenant_id, actor_id=actor_id, terminal_id=terminal_id
        )
        status_values = [str(value) for value in statuses]
        if not status_values:
            return []
        clauses.append("verification_status IN (" + ",".join("?" for _ in status_values) + ")")
        params.extend(status_values)
        if memory_types is not None:
            types = [str(value) for value in memory_types]
            if not types:
                return []
            clauses.append("memory_type IN (" + ",".join("?" for _ in types) + ")")
            params.extend(types)
        for column, value in (("namespace", namespace), ("subject_entity_id", subject_entity_id), ("predicate", predicate)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(str(value))
        if memory_ids is not None:
            ids = [str(value) for value in memory_ids]
            if not ids:
                return []
            clauses.append("memory_id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)
        now = utc_now_iso()
        clauses.append("(expires_at IS NULL OR expires_at>?)")
        params.append(now)
        order = "updated_at DESC, confidence DESC, memory_id DESC"
        rank: dict[str, int] = {}
        fts_ids: list[str] = []
        if query and use_fts and self._fts_available:
            try:
                with self._connect() as connection:
                    matches = connection.execute(
                        "SELECT memory_id, bm25(memory_fts) AS score FROM memory_fts WHERE memory_fts MATCH ? ORDER BY score LIMIT ?",
                        (str(query), max(1, min(int(limit) * 5, 1000))),
                    ).fetchall()
                fts_ids = [str(row["memory_id"]) for row in matches]
            except sqlite3.OperationalError:
                fts_ids = []
        if fts_ids:
            clauses.append("memory_id IN (" + ",".join("?" for _ in fts_ids) + ")")
            params.extend(fts_ids)
            rank = {memory_id: index for index, memory_id in enumerate(fts_ids)}
        elif query:
            # FTS tokenization is language dependent (notably for unsegmented CJK text);
            # preserve deterministic recall with an escaped parameterized LIKE fallback.
            clauses.append("(summary LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR predicate LIKE ? ESCAPE '\\')")
            escaped = str(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%"] * 3)
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_item WHERE " + " AND ".join(clauses) + f" ORDER BY {order} LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        values = [self._public(row) for row in rows]
        if rank:
            values.sort(key=lambda row: (rank.get(str(row["memory_id"]), 10**9), -float(row["confidence"])))
        return values

    def transition(
        self,
        memory_id: str,
        to_status: MemoryStatus | str,
        *,
        actor_role: str,
        actor_id: str | None,
        reason: str,
        scope: Any | None = None,
    ) -> MemoryItem:
        target = to_status if isinstance(to_status, MemoryStatus) else MemoryStatus(str(to_status))
        role = str(actor_role).strip().lower()
        if target == MemoryStatus.VERIFIED and role not in _VERIFY_ROLES:
            raise PermissionError("VERIFIED requires memory_verifier or admin")
        if target == MemoryStatus.DISPUTED and role not in _DISPUTE_ROLES:
            raise PermissionError("DISPUTED transition is not authorized")
        if target == MemoryStatus.RETIRED and role not in _RETIRE_ROLES:
            raise PermissionError("RETIRED requires privacy_officer or admin")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM memory_item WHERE memory_id=?", (str(memory_id),)).fetchone()
                if row is None:
                    raise KeyError(f"memory not found: {memory_id}")
                current = MemoryStatus(str(row["verification_status"]))
                if not transition_allowed(current, target):
                    raise ValueError(f"invalid memory transition: {current.value}->{target.value}")
                if target == MemoryStatus.VERIFIED and str(row["memory_type"]) == MemoryType.PROCEDURAL_CANDIDATE.value:
                    raise ValueError("procedural candidates remain offline review artifacts")
                now = utc_now_iso()
                connection.execute(
                    "UPDATE memory_item SET verification_status=?,updated_at=? WHERE memory_id=?",
                    (target.value, now, str(memory_id)),
                )
                transition = MemoryTransition(
                    memory_id=str(memory_id), from_status=current, to_status=target,
                    actor_role=role, actor_id=actor_id, reason=str(reason), occurred_at=now,
                ).model_dump(mode="json")
                connection.execute(
                    "INSERT INTO memory_transition(schema_version,memory_id,from_status,to_status,actor_role,actor_id,reason,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                    tuple(transition[key] for key in (
                        "schema_version", "memory_id", "from_status", "to_status", "actor_role",
                        "actor_id", "reason", "occurred_at",
                    )),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        stored = self.get(str(memory_id))
        assert stored is not None
        payload = stored.model_dump(mode="json", exclude_none=True)
        mirror_record("memory", stored.memory_id, payload, tenant_id=stored.tenant_id, actor_id=stored.actor_id, terminal_id=stored.terminal_id)
        return stored

    def expire_due(self, *, now: str | None = None, actor_id: str = "system", scope: Any | None = None) -> int:
        reference = _parse_time(now or utc_now_iso()) or datetime.now(timezone.utc)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT memory_id,expires_at FROM memory_item WHERE verification_status IN ('CANDIDATE','VERIFIED','DISPUTED') AND expires_at IS NOT NULL"
            ).fetchall()
        count = 0
        for row in rows:
            expires = _parse_time(str(row["expires_at"]))
            if expires is not None and expires <= reference:
                self.transition(
                    str(row["memory_id"]), MemoryStatus.EXPIRED,
                    actor_role="admin", actor_id=actor_id, reason="retention_expired",
                )
                count += 1
        return count

    def conflicts(self, memory_id: str, *, scope: Any | None = None) -> list[dict[str, Any]]:
        item = self.get(memory_id)
        if item is None or not item.conflict_key:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_item
                WHERE namespace=? AND subject_entity_id=? AND predicate=?
                  AND memory_id<>? AND verification_status IN ('CANDIDATE','VERIFIED','DISPUTED')
                  AND object_json<>?
                ORDER BY updated_at DESC
                """,
                (item.namespace, item.subject_entity_id, item.predicate, item.memory_id, item.object_json),
            ).fetchall()
        return [self._public(row) for row in rows]

    def transitions(self, memory_id: str, *, scope: Any | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_transition WHERE memory_id=? ORDER BY transition_id",
                (str(memory_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> MemoryItem:
        data = dict(row)
        data["object"] = json.loads(data.pop("object_json") or "null")
        data["evidence_event_ids"] = json.loads(data.pop("evidence_event_ids_json") or "[]")
        data["provenance"] = json.loads(data.pop("provenance_json") or "{}")
        return MemoryItem.model_validate(data)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["object"] = json.loads(data.pop("object_json") or "null")
        data["evidence_event_ids"] = json.loads(data.pop("evidence_event_ids_json") or "[]")
        data["provenance"] = json.loads(data.pop("provenance_json") or "{}")
        if data.get("sensitivity") in {"RESTRICTED", "SECRET"}:
            data["content"] = "<redacted>"
        return data
