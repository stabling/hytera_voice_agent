# =============================================================================
# 【维护注释】文件：voice_agent/planning/repository.py
# 所属层次：计划治理层
# 主要职责：负责计划模型、校验、持久化、策略衔接、等待条件和计划执行前治理。
# 项目内直接依赖：shared_contracts.models, shared_runtime.json_utils, .models
# 主要公开符号：PlanningRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Durable capability plan and policy-decision audit store."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from shared_contracts.models import utc_now_iso
from shared_runtime.json_utils import canonical_json
from .models import OperationPlan

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS plan_record(
 decision_id TEXT PRIMARY KEY, origin_command_id TEXT NOT NULL,
 context_snapshot_id TEXT, context_snapshot_hash TEXT, content_hash TEXT NOT NULL,
 status TEXT NOT NULL, risk_level TEXT NOT NULL, approval_required INTEGER NOT NULL,
 planner_id TEXT NOT NULL, planner_model TEXT, policy_id TEXT,
 plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_command ON plan_record(origin_command_id, created_at);
CREATE TABLE IF NOT EXISTS policy_decision_record(
 id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, policy_id TEXT NOT NULL,
 authorization_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""



class _ClosingConnection(sqlite3.Connection):
    """退出 with 块时真正关闭连接（同 operations.repository._ClosingConnection）。"""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class PlanningRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def save_plan(self, plan: OperationPlan) -> OperationPlan:
        payload = plan.model_dump(mode="json")
        values = (
            plan.decision_id,
            plan.origin_command_id,
            plan.context_snapshot_id,
            plan.context_snapshot_hash,
            str(plan.content_hash),
            plan.status,
            plan.risk_level,
            int(plan.approval_required),
            plan.planner_id,
            plan.planner_model,
            plan.policy_id,
            canonical_json(payload),
            plan.created_at,
            utc_now_iso(),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO plan_record(
                decision_id,origin_command_id,context_snapshot_id,context_snapshot_hash,
                content_hash,status,risk_level,approval_required,planner_id,planner_model,
                policy_id,plan_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(decision_id) DO UPDATE SET
                status=excluded.status,risk_level=excluded.risk_level,
                approval_required=excluded.approval_required,policy_id=excluded.policy_id,
                plan_json=excluded.plan_json,updated_at=excluded.updated_at""",
                values,
            )
        return plan

    def get_plan(self, decision_id: str) -> OperationPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT plan_json FROM plan_record WHERE decision_id=?", (decision_id,)
            ).fetchone()
        return None if row is None else OperationPlan.model_validate(json.loads(row["plan_json"]))

    def record_policy(self, decision_id: str, policy_id: str, authorization: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO policy_decision_record(decision_id,policy_id,authorization_json,created_at) VALUES(?,?,?,?)",
                (decision_id, policy_id, canonical_json(authorization), utc_now_iso()),
            )
