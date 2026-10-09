# =============================================================================
# 【维护注释】文件：voice_agent/operations/repository.py
# 所属层次：原子操作执行层
# 主要职责：负责把计划步骤固化为可审计、可幂等、可重试的原子操作并记录执行结果。
# 项目内直接依赖：shared_contracts.models, voice_agent.storage.runtime, shared_runtime.json_utils, .models, .schema
# 主要公开符号：IdempotencyConflictError, OperationRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Transactional operation/attempt/wait audit repository."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from shared_runtime.sqlite_compat import configure_durable_journal
from shared_contracts.models import utc_now_iso
from voice_agent.storage.runtime import mirror_record

from shared_runtime.json_utils import canonical_json
from .models import OPERATION_STATUSES, AtomicOperation, OperationAttempt, OperationResult, WaitCondition
from .schema import SCHEMA_SQL

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "UNKNOWN"}


class _ClosingConnection(sqlite3.Connection):
    """退出 with 块时真正关闭连接。

    sqlite3.Connection 作为上下文管理器默认只提交/回滚事务、不关闭连接；
    Windows 下句柄滞留会锁住数据文件，导致测试临时目录无法清理。
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class IdempotencyConflictError(ValueError):
    """The same idempotency key was reused with different resolved arguments."""



def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class OperationRepository:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None, check_same_thread=False,
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
                ("operation", 1, utc_now_iso()),
            )

    def create_or_get(self, operation: AtomicOperation) -> tuple[bool, AtomicOperation]:
        resolved = operation.resolved_arguments or operation.arguments
        arguments_hash = _hash(resolved)
        values = operation.model_dump(mode="json")
        row_values = {
            "operation_id": values["operation_id"],
            "schema_version": values["schema_version"],
            "origin_command_id": values["origin_command_id"],
            "operation_type": values["operation_type"],
            "step_id": values["step_id"],
            "actor_id": values["actor_id"],
            "target_type": values["target_type"],
            "target_id": values["target_id"],
            "arguments_json": canonical_json(values["arguments"]),
            "resolved_arguments_json": canonical_json(values["resolved_arguments"]),
            "arguments_hash": arguments_hash,
            "executor_kind": values["executor_kind"],
            "tool_name": values["tool_name"],
            "risk_level": values["risk_level"],
            "idempotency_key": values["idempotency_key"],
            "depends_on_json": canonical_json(values["depends_on"]),
            "status": values["status"],
            "timeout_seconds": values["timeout_seconds"],
            "max_retries": values["max_retries"],
            "side_effect": 1 if values["side_effect"] else 0,
            "created_at": values["created_at"],
            "updated_at": values["updated_at"],
            "completed_at": values["completed_at"],
            "final_result_json": None,
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM operation_record WHERE idempotency_key=?",
                    (operation.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if str(existing["arguments_hash"]) != arguments_hash:
                        raise IdempotencyConflictError(
                            "idempotency_key already exists with different resolved arguments"
                        )
                    connection.execute("COMMIT")
                    return False, self._row(existing)
                columns = list(row_values)
                connection.execute(
                    f"INSERT INTO operation_record({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [row_values[name] for name in columns],
                )
                connection.execute(
                    "INSERT INTO operation_transition(operation_id,from_status,to_status,reason,occurred_at) VALUES(?,?,?,?,?)",
                    (operation.operation_id, None, operation.status, "created", utc_now_iso()),
                )
                row = connection.execute(
                    "SELECT * FROM operation_record WHERE operation_id=?", (operation.operation_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert row is not None
        stored = self._row(row)
        mirror_record("operation", stored.operation_id, stored.model_dump(mode="json", exclude_none=True), actor_id=stored.actor_id)
        return True, stored

    def get(self, operation_id: str) -> AtomicOperation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_record WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
        return None if row is None else self._row(row)

    def get_by_idempotency(self, key: str) -> AtomicOperation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_record WHERE idempotency_key=?", (str(key),)
            ).fetchone()
        return None if row is None else self._row(row)

    def claim(self, operation_id: str) -> bool:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status FROM operation_record WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if row is None or row["status"] not in {"CREATED", "READY"}:
                    connection.execute("COMMIT")
                    return False
                now = utc_now_iso()
                connection.execute(
                    "UPDATE operation_record SET status='RUNNING',updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
                connection.execute(
                    "INSERT INTO operation_transition(operation_id,from_status,to_status,reason,occurred_at) VALUES(?,?,?,?,?)",
                    (operation_id, row["status"], "RUNNING", "claimed", now),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def transition(
        self,
        operation_id: str,
        status: str,
        *,
        reason: str | None = None,
        final_result: dict[str, Any] | None = None,
    ) -> AtomicOperation | None:
        if status not in OPERATION_STATUSES:
            raise ValueError(f"invalid operation status: {status}")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status FROM operation_record WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                old = str(row["status"])
                now = utc_now_iso()
                completed = now if status in _TERMINAL else None
                connection.execute(
                    "UPDATE operation_record SET status=?,updated_at=?,completed_at=COALESCE(?,completed_at),final_result_json=COALESCE(?,final_result_json) WHERE operation_id=?",
                    (status, now, completed, None if final_result is None else canonical_json(final_result), operation_id),
                )
                connection.execute(
                    "INSERT INTO operation_transition(operation_id,from_status,to_status,reason,occurred_at) VALUES(?,?,?,?,?)",
                    (operation_id, old, status, reason, now),
                )
                updated = connection.execute(
                    "SELECT * FROM operation_record WHERE operation_id=?", (operation_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        stored = None if updated is None else self._row(updated)
        if stored is not None:
            mirror_record("operation", stored.operation_id, stored.model_dump(mode="json", exclude_none=True), actor_id=stored.actor_id)
        return stored

    def start_attempt(self, operation_id: str, executor_mode: str) -> OperationAttempt:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM operation_attempt WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                attempt_no = int(row["n"])
                attempt = OperationAttempt(
                    attempt_id=f"attempt_{operation_id}_{attempt_no}",
                    operation_id=operation_id,
                    attempt_no=attempt_no,
                    executor_mode=executor_mode,
                )
                values = attempt.model_dump(mode="json")
                values["accepted"] = None
                values["dispatched"] = None
                values["device_confirmed"] = None
                response = values.pop("response")
                values["response_json"] = None if response is None else canonical_json(response)
                columns = list(values)
                connection.execute(
                    f"INSERT INTO operation_attempt({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [values[name] for name in columns],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return attempt

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        accepted: bool | None,
        dispatched: bool | None,
        device_confirmed: bool | None,
        response: dict[str, Any] | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE operation_attempt SET ended_at=?,accepted=?,dispatched=?,device_confirmed=?,
                    response_json=?,error_code=?,error_message=? WHERE attempt_id=?
                """,
                (
                    utc_now_iso(),
                    None if accepted is None else int(accepted),
                    None if dispatched is None else int(dispatched),
                    None if device_confirmed is None else int(device_confirmed),
                    None if response is None else canonical_json(response),
                    error_code,
                    None if error_message is None else str(error_message)[:2000],
                    attempt_id,
                ),
            )

    def add_wait(self, condition: WaitCondition) -> bool:
        values = condition.model_dump(mode="json")
        columns = list(values)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO wait_condition({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                [values[name] for name in columns],
            )
        return int(cursor.rowcount) == 1

    def waits_by_subject(self, *, subject_type: str, subject_id: str, predicate: str | None = None, status: str = "WAITING") -> list[dict[str, Any]]:
        clauses = ["subject_type=?", "subject_id=?", "status=?"]
        params: list[Any] = [str(subject_type), str(subject_id), str(status)]
        if predicate is not None:
            clauses.append("predicate=?")
            params.append(str(predicate))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM wait_condition WHERE " + " AND ".join(clauses) + " ORDER BY created_at,wait_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def satisfy_waits(self, *, subject_type: str, subject_id: str, predicate: str, event: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Satisfy only waits matching the exact subject type/id/predicate triple."""
        now = utc_now_iso()
        matched: list[dict[str, Any]] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT * FROM wait_condition WHERE subject_type=? AND subject_id=? AND predicate=? AND status='WAITING' ORDER BY created_at,wait_id",
                    (str(subject_type), str(subject_id), str(predicate)),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "UPDATE wait_condition SET status='SATISFIED',satisfied_at=? WHERE wait_id=? AND status='WAITING'",
                        (now, row["wait_id"]),
                    )
                    data = dict(row)
                    data["status"] = "SATISFIED"
                    data["satisfied_at"] = now
                    data["event"] = dict(event or {})
                    matched.append(data)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return matched

    def cancel_for_command(self, command_id: str, *, reason: str) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM operation_record WHERE origin_command_id=? AND status IN ('CREATED','READY')",
                (command_id,),
            ).fetchall()
        count = 0
        for row in rows:
            if self.transition(str(row["operation_id"]), "CANCELLED", reason=reason) is not None:
                count += 1
        return count

    def recover_running(self) -> dict[str, int]:
        """Never blindly redo uncertain side effects after a process restart."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id,side_effect FROM operation_record WHERE status='RUNNING'"
            ).fetchall()
        result = {"ready": 0, "unknown": 0}
        for row in rows:
            next_status = "UNKNOWN" if int(row["side_effect"]) else "READY"
            self.transition(str(row["operation_id"]), next_status, reason="process_restart_recovery")
            result["unknown" if next_status == "UNKNOWN" else "ready"] += 1
        return result

    def attempts(self, operation_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_attempt WHERE operation_id=? ORDER BY attempt_no",
                (operation_id,),
            ).fetchall()
        return [self._attempt_dict(row) for row in rows]

    def query(
        self,
        *,
        command_id: str | None = None,
        operation_id: str | None = None,
        actor_id: str | None = None,
        target_id: str | None = None,
        operation_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("origin_command_id", command_id), ("operation_id", operation_id),
            ("actor_id", actor_id), ("target_id", target_id),
            ("operation_type", operation_type), ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(str(value))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_record" + where + " ORDER BY created_at DESC, operation_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._public_dict(row) for row in rows]

    def final_result(self, operation_id: str) -> OperationResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT final_result_json FROM operation_record WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None or not row["final_result_json"]:
            return None
        return OperationResult.model_validate(json.loads(row["final_result_json"]))

    @staticmethod
    def _row(row: sqlite3.Row) -> AtomicOperation:
        data = dict(row)
        data["arguments"] = json.loads(data.pop("arguments_json") or "{}")
        data["resolved_arguments"] = json.loads(data.pop("resolved_arguments_json") or "{}")
        data["depends_on"] = json.loads(data.pop("depends_on_json") or "[]")
        data["side_effect"] = bool(data["side_effect"])
        data.pop("arguments_hash", None)
        data.pop("final_result_json", None)
        return AtomicOperation.model_validate(data)

    @staticmethod
    def _public_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        args = json.loads(data.pop("arguments_json") or "{}")
        resolved = json.loads(data.pop("resolved_arguments_json") or "{}")
        # Sensitive content is not returned from list diagnostics.
        for source in (args, resolved):
            for key in list(source):
                if key.lower() in {"content", "token", "api_key", "audio", "transcript"}:
                    source[key] = "***REDACTED***"
        data["arguments"] = args
        data["resolved_arguments"] = resolved
        data["depends_on"] = json.loads(data.pop("depends_on_json") or "[]")
        data["side_effect"] = bool(data["side_effect"])
        if data.get("final_result_json"):
            data["final_result"] = {"present": True}
        data.pop("final_result_json", None)
        data.pop("arguments_hash", None)
        return data

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["response"] = json.loads(data.pop("response_json")) if data.get("response_json") else None
        for field in ("accepted", "dispatched", "device_confirmed"):
            if data[field] is not None:
                data[field] = bool(data[field])
        return data
