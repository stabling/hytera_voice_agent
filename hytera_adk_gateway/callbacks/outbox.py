# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/outbox.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：shared_contracts.models
# 主要公开符号：OutboxState, OutboxCapacityError, OutboxRecord, CallbackOutbox
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from shared_contracts.models import ensure_voice_event_contract

logger = logging.getLogger(__name__)


def _default_synchronous() -> str:
    """Outbox 写入的 fsync 策略。

    WAL + NORMAL 仅在断电时可能丢最近一次写入，且回调链路本身是
    at-least-once（网关在收到 Agent ACK 前会重发），因此默认 NORMAL
    以消除每条约 45ms 的 fsync 开销；需要回到旧行为时设 FULL。
    """
    value = str(os.environ.get("VOICE_CALLBACK_OUTBOX_SYNCHRONOUS", "NORMAL")).strip().upper()
    return value if value in {"OFF", "NORMAL", "FULL"} else "NORMAL"


class OutboxState(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    RETRY = "RETRY"
    ACKED = "ACKED"
    DEAD_LETTER = "DEAD_LETTER"


class OutboxCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutboxRecord:
    event_id: str
    terminal_id: str | None
    session_id: str | None
    created_at: str
    payload_hash: str
    payload_bytes: int
    payload: dict[str, Any]
    state: OutboxState
    attempts: int
    next_attempt_at: float
    last_error: str | None
    lease_until: float | None
    mapping_ref: str = "default"


class CallbackOutbox:
    """SQLite-backed callback Outbox with leases and bounded capacity."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: Path | str,
        *,
        max_events: int = 10_000,
        max_bytes: int = 512 * 1024 * 1024,
        retention_seconds: float = 7 * 24 * 3600,
        lease_seconds: float = 30.0,
        clock=time.time,
    ) -> None:
        # Resolve once at construction time.  The 32-bit ADK runtime temporarily
        # changes the process-wide working directory while loading vendor DLLs; a
        # relative SQLite path would otherwise point at a different directory on
        # later connections and fail with ``unable to open database file``.
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        self.path = candidate.resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_events = max(1, int(max_events))
        self.max_bytes = max(1, int(max_bytes))
        self.retention_seconds = max(60.0, float(retention_seconds))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self.synchronous = _default_synchronous()
        # 持久连接：原实现每次操作新建并关闭连接，关闭时触发 WAL
        # checkpoint（主库已增长到数十 MB），实测单次入队从 ~46ms 膨胀到
        # ~1.2s。改为进程内单连接复用，所有访问都在 self._lock 下串行。
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread: threading.Thread | None = None
        # 容量计数内存化：enqueue 的容量检查不再每次回表扫描大 payload 行
        # （生产库 311 条 ACKED 历史 × ~37KB，冷缓存下单次 SUM 查询实测 564ms+，
        # 是 outbox_enqueue ~1.2s 的真正根因）。启动时查一次，之后增量维护，
        # 并由后台线程周期校准以容忍外部工具修改表数据。
        self._active_events = 0
        self._active_bytes = 0
        self._last_purge_at = 0.0
        self._initialize()
        self._start_checkpoint_thread()

    def _connect(self) -> sqlite3.Connection:
        """打开一条新连接并应用 PRAGMA（供持久连接与重建使用）。"""
        connection = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA synchronous={self.synchronous}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _connection(self) -> sqlite3.Connection:
        """返回复用的持久连接；连接失效时在锁内重建。"""
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _reset_connection(self) -> None:
        """关闭并丢弃当前连接，下一次访问时重建。"""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _start_checkpoint_thread(self) -> None:
        """低频后台 checkpoint，避免 WAL 无限增长。

        原实现依赖“连接关闭时 checkpoint”，改为持久连接后必须显式
        周期性执行 PASSIVE checkpoint（不阻塞读写）。
        """
        if self._checkpoint_thread is not None:
            return

        def _loop() -> None:
            while not self._checkpoint_stop.wait(30.0):
                try:
                    with self._lock:
                        if self._closed or self._conn is None:
                            continue
                        connection = self._connection()
                        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        # 覆盖索引下 SUM/COUNT 不再回表，校准计数只需毫秒级。
                        self._refresh_active_counters(connection)
                        # 周期清理过期终态行（purge_expired 原先无任何调用方，
                        # 导致 311 条 ACKED 历史堆积并拖慢容量检查）。
                        if self._clock() - self._last_purge_at >= 600.0:
                            self.purge_expired()
                            self._last_purge_at = self._clock()
                except sqlite3.Error:
                    # checkpoint 失败不影响投递主链路，下个周期重试。
                    self._reset_connection()

        self._checkpoint_thread = threading.Thread(
            target=_loop, name="hytera-callback-outbox-checkpoint", daemon=True
        )
        self._checkpoint_thread.start()

    def close(self) -> None:
        """停止后台 checkpoint 并关闭持久连接（进程退出前可调用）。"""
        self._checkpoint_stop.set()
        with self._lock:
            self._closed = True
            self._reset_connection()

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connection()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA synchronous={self.synchronous}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS callback_outbox (
                    event_id TEXT PRIMARY KEY,
                    terminal_id TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL CHECK(payload_bytes >= 0),
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT,
                    lease_until REAL,
                    acked_at REAL,
                    updated_at REAL NOT NULL,
                    CHECK(state IN ('PENDING','SENDING','RETRY','ACKED','DEAD_LETTER'))
                );
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_due
                    ON callback_outbox(state, next_attempt_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_terminal
                    ON callback_outbox(terminal_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_active_bytes
                    ON callback_outbox(state, payload_bytes);
                CREATE TABLE IF NOT EXISTS callback_outbox_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO callback_outbox_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )
            self.recover_expired_leases(connection=connection)
            self._refresh_active_counters(connection)
            self._last_purge_at = self._clock()

    @staticmethod
    def _is_active_state(state: str | None) -> bool:
        return state is not None and state not in ("ACKED", "DEAD_LETTER")

    def _refresh_active_counters(self, connection: sqlite3.Connection) -> None:
        """从覆盖索引重算活跃计数（启动时与后台周期校准时调用）。"""
        row = connection.execute(
            """
            SELECT COUNT(*) AS event_count, COALESCE(SUM(payload_bytes),0) AS total_bytes
            FROM callback_outbox
            WHERE state NOT IN ('ACKED','DEAD_LETTER')
            """
        ).fetchone()
        self._active_events = int(row["event_count"])
        self._active_bytes = int(row["total_bytes"])

    def enqueue(self, payload: dict[str, Any]) -> tuple[OutboxRecord, bool]:
        event = ensure_voice_event_contract(payload, generate_event_id=True)
        event_id = str(event["event_id"])
        payload_json = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = self._clock()
        with self._lock:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM callback_outbox WHERE event_id=?", (event_id,)
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return self._row_to_record(existing), False

                # 容量检查走内存计数（后台线程周期校准），消除每次入队对
                # 大 payload 行的回表扫描。
                payload_bytes = int(event.get("payload_bytes") or 0)
                if (
                    self._active_events >= self.max_events
                    or self._active_bytes + payload_bytes > self.max_bytes
                ):
                    raise OutboxCapacityError(
                        "callback outbox capacity reached: events=%d/%d bytes=%d/%d"
                        % (self._active_events, self.max_events, self._active_bytes, self.max_bytes)
                    )

                connection.execute(
                    """
                    INSERT INTO callback_outbox(
                        event_id, terminal_id, session_id, created_at, payload_hash,
                        payload_bytes, payload_json, state, attempts, next_attempt_at,
                        last_error, lease_until, acked_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,0,?,NULL,NULL,NULL,?)
                    """,
                    (
                        event_id,
                        self._text_or_none(event.get("terminal_id") or event.get("source_id")),
                        self._text_or_none(event.get("session_id")),
                        str(event.get("created_at") or ""),
                        str(event.get("payload_hash") or ""),
                        payload_bytes,
                        payload_json,
                        OutboxState.PENDING.value,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM callback_outbox WHERE event_id=?", (event_id,)
                ).fetchone()
                connection.execute("COMMIT")
                self._active_events += 1
                self._active_bytes += payload_bytes
            except Exception:
                self._rollback_quietly()
                raise
        assert row is not None
        return self._row_to_record(row), True

    def _rollback_quietly(self) -> None:
        """回滚当前事务；连接不可用时直接丢弃以便下次重建。"""
        conn = self._conn
        if conn is None:
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            self._reset_connection()

    def lease_due(self, *, limit: int = 1) -> list[OutboxRecord]:
        now = self._clock()
        limit = max(1, int(limit))
        with self._lock:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE callback_outbox
                    SET state='RETRY', lease_until=NULL, next_attempt_at=?,
                        last_error=COALESCE(last_error,'lease_expired'), updated_at=?
                    WHERE state='SENDING' AND lease_until IS NOT NULL AND lease_until <= ?
                    """,
                    (now, now, now),
                )
                rows = connection.execute(
                    """
                    SELECT * FROM callback_outbox
                    WHERE state IN ('PENDING','RETRY') AND next_attempt_at <= ?
                    ORDER BY next_attempt_at ASC, created_at ASC
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
                records: list[OutboxRecord] = []
                for row in rows:
                    connection.execute(
                        """
                        UPDATE callback_outbox
                        SET state='SENDING', attempts=attempts+1, lease_until=?, updated_at=?
                        WHERE event_id=? AND state IN ('PENDING','RETRY')
                        """,
                        (now + self.lease_seconds, now, row["event_id"]),
                    )
                if rows:
                    placeholders = ",".join("?" for _ in rows)
                    refreshed = connection.execute(
                        f"SELECT * FROM callback_outbox WHERE event_id IN ({placeholders})",
                        tuple(row["event_id"] for row in rows),
                    ).fetchall()
                    by_id = {row["event_id"]: row for row in refreshed}
                    records = [self._row_to_record(by_id[row["event_id"]]) for row in rows]
                connection.execute("COMMIT")
                return records
            except Exception:
                self._rollback_quietly()
                raise

    def mark_acked(self, event_id: str) -> None:
        now = self._clock()
        self._update_state(
            event_id,
            state=OutboxState.ACKED,
            next_attempt_at=now,
            last_error=None,
            lease_until=None,
            acked_at=now,
        )

    def mark_retry(self, event_id: str, *, error: str, delay_seconds: float) -> None:
        now = self._clock()
        self._update_state(
            event_id,
            state=OutboxState.RETRY,
            next_attempt_at=now + max(0.05, float(delay_seconds)),
            last_error=error[:1024],
            lease_until=None,
            acked_at=None,
        )

    def mark_dead_letter(self, event_id: str, *, error: str) -> None:
        now = self._clock()
        self._update_state(
            event_id,
            state=OutboxState.DEAD_LETTER,
            next_attempt_at=now,
            last_error=error[:1024],
            lease_until=None,
            acked_at=None,
        )

    def _update_state(
        self,
        event_id: str,
        *,
        state: OutboxState,
        next_attempt_at: float,
        last_error: str | None,
        lease_until: float | None,
        acked_at: float | None,
    ) -> None:
        now = self._clock()
        with self._lock:
            connection = self._connection()
            # 主键快查前态，用于增量维护内存容量计数（活跃↔终态转换时调整）。
            prev = connection.execute(
                "SELECT state, payload_bytes FROM callback_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE callback_outbox
                SET state=?, next_attempt_at=?, last_error=?, lease_until=?, acked_at=?, updated_at=?
                WHERE event_id=?
                """,
                (
                    state.value,
                    next_attempt_at,
                    last_error,
                    lease_until,
                    acked_at,
                    now,
                    event_id,
                ),
            )
            if prev is not None:
                was_active = self._is_active_state(str(prev["state"]))
                now_active = self._is_active_state(state.value)
                if was_active and not now_active:
                    self._active_events -= 1
                    self._active_bytes -= int(prev["payload_bytes"])
                elif now_active and not was_active:
                    self._active_events += 1
                    self._active_bytes += int(prev["payload_bytes"])

    def recover_expired_leases(self, *, connection: sqlite3.Connection | None = None) -> int:
        now = self._clock()
        with self._lock:
            conn = connection or self._connection()
            cursor = conn.execute(
                """
                UPDATE callback_outbox
                SET state='RETRY', lease_until=NULL, next_attempt_at=?,
                    last_error=COALESCE(last_error,'lease_expired'), updated_at=?
                WHERE state='SENDING' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (now, now, now),
            )
            return max(0, int(cursor.rowcount))

    def purge_expired(self) -> int:
        cutoff = self._clock() - self.retention_seconds
        with self._lock:
            connection = self._connection()
            cursor = connection.execute(
                """
                DELETE FROM callback_outbox
                WHERE state IN ('ACKED','DEAD_LETTER') AND updated_at < ?
                """,
                (cutoff,),
            )
            return max(0, int(cursor.rowcount))

    def get(self, event_id: str) -> OutboxRecord | None:
        with self._lock:
            connection = self._connection()
            row = connection.execute(
                "SELECT * FROM callback_outbox WHERE event_id=?", (event_id,)
            ).fetchone()
        return None if row is None else self._row_to_record(row)

    def list_dead_letters(self, *, limit: int = 100) -> list[OutboxRecord]:
        with self._lock:
            connection = self._connection()
            rows = connection.execute(
                """
                SELECT * FROM callback_outbox WHERE state='DEAD_LETTER'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            connection = self._connection()
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS count, COALESCE(SUM(payload_bytes),0) AS bytes
                FROM callback_outbox GROUP BY state
                """
            ).fetchall()
        by_state = {
            state.value: {"count": 0, "bytes": 0}
            for state in OutboxState
        }
        for row in rows:
            by_state[str(row["state"])] = {
                "count": int(row["count"]),
                "bytes": int(row["bytes"]),
            }
        active_count = sum(
            by_state[state.value]["count"]
            for state in (OutboxState.PENDING, OutboxState.SENDING, OutboxState.RETRY)
        )
        active_bytes = sum(
            by_state[state.value]["bytes"]
            for state in (OutboxState.PENDING, OutboxState.SENDING, OutboxState.RETRY)
        )
        return {
            "path": str(self.path),
            "schema_version": self.SCHEMA_VERSION,
            "synchronous": self.synchronous,
            "states": by_state,
            "active_count": active_count,
            "active_bytes": active_bytes,
            "max_events": self.max_events,
            "max_bytes": self.max_bytes,
        }

    @staticmethod
    def _text_or_none(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)[:256]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> OutboxRecord:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("outbox payload_json must contain a JSON object")
        return OutboxRecord(
            event_id=str(row["event_id"]),
            terminal_id=row["terminal_id"],
            session_id=row["session_id"],
            created_at=str(row["created_at"]),
            payload_hash=str(row["payload_hash"]),
            payload_bytes=int(row["payload_bytes"]),
            payload=payload,
            state=OutboxState(str(row["state"])),
            attempts=int(row["attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            last_error=row["last_error"],
            lease_until=None if row["lease_until"] is None else float(row["lease_until"]),
        )
