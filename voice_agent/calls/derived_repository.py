# =============================================================================
# 【维护注释】文件：voice_agent/calls/derived_repository.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.models
# 主要公开符号：DerivedContentRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Persistence and idempotency for generated call content and delivery attempts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from shared_runtime.sqlite_compat import ClosingConnection, configure_durable_journal
from shared_contracts.models import utc_now_iso

from .migration_runner import run_call_store_migrations


class DerivedContentRepository:
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
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        migration_dir = Path(__file__).with_name("migrations")
        with self._lock, self._connect() as connection:
            configure_durable_journal(connection, self.path)
            run_call_store_migrations(connection, migration_dir)

    def get_by_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM derived_content WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
        return None if row is None else self._decode(row)

    def get(self, derived_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM derived_content WHERE derived_id=?", (str(derived_id),)
            ).fetchone()
        return None if row is None else self._decode(row)

    def save(
        self,
        *,
        operation_id: str,
        operation_type: str,
        call_id: str,
        ok: bool,
        content: str,
        source_turn_ids: list[str],
        model: str | None,
        prompt_hash: str,
        options: dict[str, Any],
        generated_at: str,
        error: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        derived_id = "drv_" + uuid.uuid4().hex
        content_hash = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM derived_content WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return False, self._decode(existing)
                connection.execute(
                    """
                    INSERT INTO derived_content(
                        derived_id,schema_version,operation_id,operation_type,call_id,ok,
                        content,content_hash,source_turn_ids_json,model,prompt_hash,
                        options_json,generated_at,error
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        derived_id,
                        1,
                        operation_id,
                        operation_type,
                        call_id,
                        1 if ok else 0,
                        content,
                        content_hash,
                        json.dumps(source_turn_ids, ensure_ascii=False, separators=(",", ":")),
                        model,
                        prompt_hash,
                        json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        generated_at,
                        error,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM derived_content WHERE derived_id=?", (derived_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert row is not None
        return True, self._decode(row)

    def get_delivery(
        self, *, operation_id: str, target_id: str, target_type: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM result_delivery
                WHERE operation_id=? AND target_id=? AND target_type=?
                """,
                (operation_id, target_id, target_type),
            ).fetchone()
        return None if row is None else dict(row)

    def record_delivery(
        self,
        *,
        operation_id: str,
        derived_id: str,
        target_id: str,
        target_type: str,
        ok: bool,
        error: str | None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        delivery_id = "del_" + uuid.uuid4().hex
        status = "delivered" if ok else "failed"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO result_delivery(
                    delivery_id,schema_version,operation_id,derived_id,target_id,target_type,
                    status,attempts,last_error,delivered_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(operation_id,target_id,target_type) DO UPDATE SET
                    status=excluded.status,
                    attempts=result_delivery.attempts+1,
                    last_error=excluded.last_error,
                    delivered_at=CASE WHEN excluded.status='delivered' THEN excluded.delivered_at ELSE result_delivery.delivered_at END,
                    updated_at=excluded.updated_at
                """,
                (
                    delivery_id,
                    1,
                    operation_id,
                    derived_id,
                    target_id,
                    target_type,
                    status,
                    1,
                    error,
                    now if ok else None,
                    now,
                    now,
                ),
            )
        stored = self.get_delivery(
            operation_id=operation_id, target_id=target_id, target_type=target_type
        )
        assert stored is not None
        return stored

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["ok"] = bool(value["ok"])
        try:
            value["source_turn_ids"] = json.loads(value.pop("source_turn_ids_json"))
        except (json.JSONDecodeError, TypeError):
            value["source_turn_ids"] = []
        try:
            value["options"] = json.loads(value.pop("options_json"))
        except (json.JSONDecodeError, TypeError):
            value["options"] = {}
        return value
