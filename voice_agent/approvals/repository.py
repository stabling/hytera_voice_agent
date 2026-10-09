# =============================================================================
# 【维护注释】文件：voice_agent/approvals/repository.py
# 所属层次：人工审批层
# 主要职责：负责高风险操作的审批请求、不可变审批事件和审核结果持久化。
# 项目内直接依赖：shared_contracts.models, shared_runtime.json_utils, .models
# 主要公开符号：ApprovalRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""SQLite approval repository with immutable decision events."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from shared_contracts.models import utc_now_iso
from shared_runtime.json_utils import canonical_json
from .models import Approval

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approval_migration_version(component TEXT PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approval_request(
 approval_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, decision_id TEXT NOT NULL, origin_command_id TEXT NOT NULL,
 thread_id TEXT NOT NULL, plan_hash TEXT NOT NULL, policy_id TEXT NOT NULL, risk_level TEXT NOT NULL,
 status TEXT NOT NULL, requested_payload_json TEXT NOT NULL, approved_payload_json TEXT,
 reviewer_id TEXT, reviewer_role TEXT, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_decision ON approval_request(decision_id);
CREATE INDEX IF NOT EXISTS idx_approval_thread ON approval_request(thread_id,status);
CREATE TABLE IF NOT EXISTS approval_event(
 id INTEGER PRIMARY KEY AUTOINCREMENT, approval_id TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
 actor_id TEXT, actor_role TEXT, payload_json TEXT, reason TEXT, occurred_at TEXT NOT NULL
);
"""



class _ClosingConnection(sqlite3.Connection):
    """退出 with 块时真正关闭连接（同 operations.repository._ClosingConnection）。"""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class ApprovalRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute("INSERT OR IGNORE INTO approval_migration_version VALUES(?,?,?)", ("approval", 1, utc_now_iso()))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, approval: Approval) -> Approval:
        values = approval.model_dump(mode="json")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute("SELECT * FROM approval_request WHERE decision_id=? AND status='PENDING'", (approval.decision_id,)).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return self._row(existing)
                connection.execute("""INSERT INTO approval_request(approval_id,schema_version,decision_id,origin_command_id,thread_id,plan_hash,policy_id,risk_level,status,requested_payload_json,approved_payload_json,reviewer_id,reviewer_role,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    approval.approval_id, approval.schema_version, approval.decision_id, approval.origin_command_id, approval.thread_id,
                    approval.plan_hash, approval.policy_id, approval.risk_level, approval.status, canonical_json(approval.requested_payload), None,
                    approval.reviewer_id, approval.reviewer_role, approval.reason, approval.created_at, approval.updated_at))
                connection.execute("INSERT INTO approval_event(approval_id,from_status,to_status,payload_json,occurred_at) VALUES(?,?,?,?,?)", (approval.approval_id, None, "PENDING", canonical_json(approval.requested_payload), approval.created_at))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return approval

    def get(self, approval_id: str) -> Approval | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM approval_request WHERE approval_id=?", (approval_id,)).fetchone()
        return None if row is None else self._row(row)

    def decide(self, approval_id: str, *, action: str, actor_id: str, actor_role: str, reason: str | None = None, edited_payload: dict[str, Any] | None = None) -> Approval:
        action = str(action).strip().lower()
        status_map = {"approve": "APPROVED", "edit": "EDITED", "reject": "REJECTED"}
        if action not in status_map:
            raise ValueError("approval action must be approve, edit, or reject")
        if not actor_id or not actor_role:
            raise PermissionError("reviewer identity and role are required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM approval_request WHERE approval_id=?", (approval_id,)).fetchone()
                if row is None:
                    raise KeyError("approval not found")
                current = str(row["status"])
                if current != "PENDING":
                    raise ValueError(f"approval is already {current}")
                new_status = status_map[action]
                payload = edited_payload if action == "edit" else json.loads(row["requested_payload_json"])
                if action == "edit" and not isinstance(edited_payload, dict):
                    raise ValueError("edited_payload is required for edit")
                now = utc_now_iso()
                connection.execute("UPDATE approval_request SET status=?,approved_payload_json=?,reviewer_id=?,reviewer_role=?,reason=?,updated_at=? WHERE approval_id=?", (new_status, None if new_status == "REJECTED" else canonical_json(payload), actor_id, actor_role, reason, now, approval_id))
                connection.execute("INSERT INTO approval_event(approval_id,from_status,to_status,actor_id,actor_role,payload_json,reason,occurred_at) VALUES(?,?,?,?,?,?,?,?)", (approval_id, current, new_status, actor_id, actor_role, None if payload is None else canonical_json(payload), reason, now))
                updated = connection.execute("SELECT * FROM approval_request WHERE approval_id=?", (approval_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row(updated)

    def events(self, approval_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM approval_event WHERE approval_id=? ORDER BY id", (approval_id,)).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json")) if data.get("payload_json") else None
            result.append(data)
        return result

    @staticmethod
    def _row(row: sqlite3.Row) -> Approval:
        data = dict(row)
        data["requested_payload"] = json.loads(data.pop("requested_payload_json") or "{}")
        raw_approved = data.pop("approved_payload_json", None)
        data["approved_payload"] = json.loads(raw_approved) if raw_approved else None
        return Approval.model_validate(data)
