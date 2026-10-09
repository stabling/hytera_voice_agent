# =============================================================================
# 【维护注释】文件：voice_agent/callback_spool.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：shared_contracts.models
# 主要公开符号：CallbackSpoolError, CallbackSpoolCapacityError, CallbackPayloadTooLarge, CallbackPayloadInvalid, SpoolAcceptResult, SpoolRecord, CallbackSpool
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared_contracts.models import ensure_voice_event_contract


def _default_synchronous() -> str:
    """Spool 写入的 fsync 策略（与网关 outbox 同模式）。

    WAL + NORMAL 仅在断电时可能丢最近一次事务，而回调链路是 at-least-once
    （网关在收到 ACK 前会重发，spool 靠 event_id 幂等去重），因此默认
    NORMAL 以消除每次 accept 约 45ms 量级的提交 fsync；需要回到旧行为
    时设 VOICE_CALLBACK_SPOOL_SYNCHRONOUS=FULL。
    """
    value = str(os.environ.get("VOICE_CALLBACK_SPOOL_SYNCHRONOUS", "NORMAL")).strip().upper()
    return value if value in {"OFF", "NORMAL", "FULL"} else "NORMAL"


class CallbackSpoolError(RuntimeError):
    status_code = 503
    reason = "spool_error"


class CallbackSpoolCapacityError(CallbackSpoolError):
    status_code = 503
    reason = "spool_full"


class CallbackPayloadTooLarge(CallbackSpoolError):
    status_code = 413
    reason = "payload_too_large"


class CallbackPayloadInvalid(CallbackSpoolError):
    status_code = 400
    reason = "invalid_audio_payload"


@dataclass(frozen=True)
class SpoolAcceptResult:
    event_id: str
    request_id: int | str
    duplicate: bool
    queued: bool
    spool_path: Path
    queue_depth: int
    payload_hash: str
    payload_bytes: int


@dataclass(frozen=True)
class SpoolRecord:
    event_id: str
    request_id: str
    payload_hash: str
    payload_bytes: int
    spool_path: Path
    metadata: dict[str, Any]
    status: str
    attempts: int
    last_error: str | None
    received_at: float = 0.0
    updated_at: float = 0.0


class CallbackSpool:
    """Durable inbound callback spool and idempotency registry.

    Audio bytes are written to files; SQLite stores only metadata and processing state.
    The in-memory ASR queue therefore carries event IDs and paths, never Base64 audio.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: Path | str,
        *,
        max_events: int = 10_000,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
        max_audio_bytes: int = 16 * 1024 * 1024,
        retention_seconds: float = 7 * 24 * 3600,
        clock=time.time,
    ) -> None:
        self.root = Path(root)
        self.audio_dir = self.root / "audio"
        self.db_path = self.root / "callback_spool.sqlite3"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.max_events = max(1, int(max_events))
        self.max_bytes = max(1, int(max_bytes))
        self.max_audio_bytes = max(1, int(max_audio_bytes))
        self.retention_seconds = max(60.0, float(retention_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        """返回进程内复用的持久连接；损坏时重建。

        历史：每次操作新建 SQLite 连接（含 WAL 恢复扫描）在冷页缓存下
        首条请求 persist 可达数百毫秒；持久连接后降为毫秒级（同网关
        outbox 优化经验）。
        """
        if self._conn is None:
            connection = sqlite3.connect(
                str(self.db_path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA synchronous={_default_synchronous()}")
            self._conn = connection
        return self._conn

    def _reset_connection(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def close(self) -> None:
        """关闭持久连接（主要用于测试与进程退出清理）。"""
        with self._lock:
            self._closed = True
            self._reset_connection()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connection()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_voice_events (
                    event_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    terminal_id TEXT,
                    session_id TEXT,
                    created_at TEXT,
                    metadata_json TEXT NOT NULL,
                    spool_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    received_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    last_error TEXT,
                    CHECK(status IN ('RECEIVED','QUEUED','PROCESSING','DONE','FAILED'))
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_voice_status
                    ON inbound_voice_events(status, received_at);
                CREATE TABLE IF NOT EXISTS inbound_voice_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO inbound_voice_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )
            # A hard kill can leave PROCESSING records behind. Requeue them safely;
            # event_id idempotency prevents a second execution from a network retry.
            connection.execute(
                """
                UPDATE inbound_voice_events SET status='RECEIVED', updated_at=?,
                    last_error=COALESCE(last_error,'process_restart_recovery')
                WHERE status='PROCESSING'
                """,
                (self._clock(),),
            )

    def accept(
        self,
        data: dict[str, Any],
        *,
        request_id: int | str,
        queue_depth: int,
    ) -> SpoolAcceptResult:
        event = ensure_voice_event_contract(
            data,
            generate_event_id=True,
            deterministic_legacy_id=True,
        )
        event_id = str(event["event_id"])
        payload_hash = str(event["payload_hash"])
        raw_audio = self._decode_audio(event)
        payload_bytes = len(raw_audio)
        if payload_bytes > self.max_audio_bytes:
            raise CallbackPayloadTooLarge(
                f"decoded audio exceeds limit: {payload_bytes}>{self.max_audio_bytes}"
            )
        event["payload_bytes"] = payload_bytes
        metadata = self._metadata_without_audio(event)
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        spool_path = self.audio_dir / f"{event_id}.bin"
        now = self._clock()

        with self._lock:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            temp_path: Path | None = None
            try:
                existing = connection.execute(
                    "SELECT * FROM inbound_voice_events WHERE event_id=?", (event_id,)
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return SpoolAcceptResult(
                        event_id=event_id,
                        request_id=existing["request_id"],
                        duplicate=True,
                        queued=False,
                        spool_path=Path(str(existing["spool_path"])),
                        queue_depth=queue_depth,
                        payload_hash=str(existing["payload_hash"]),
                        payload_bytes=int(existing["payload_bytes"]),
                    )

                usage = connection.execute(
                    """
                    SELECT COUNT(*) AS event_count, COALESCE(SUM(payload_bytes),0) AS total_bytes
                    FROM inbound_voice_events WHERE status != 'DONE'
                    """
                ).fetchone()
                event_count = int(usage["event_count"])
                total_bytes = int(usage["total_bytes"])
                if event_count >= self.max_events or total_bytes + payload_bytes > self.max_bytes:
                    raise CallbackSpoolCapacityError(
                        "callback spool capacity reached: events=%d/%d bytes=%d/%d"
                        % (event_count, self.max_events, total_bytes, self.max_bytes)
                    )

                temp_path = spool_path.with_suffix(".tmp")
                # 不对音频文件做显式 fsync：os.replace 原子生效，DB 记录是
                # 真正的归属凭证（异常回滚会清理文件）；崩溃残留的孤儿文件
                # 由 retention 清理。DB 本身按 _default_synchronous 提交。
                with temp_path.open("wb") as fp:
                    fp.write(raw_audio)
                os.replace(str(temp_path), str(spool_path))

                connection.execute(
                    """
                    INSERT INTO inbound_voice_events(
                        event_id, request_id, payload_hash, payload_bytes, terminal_id,
                        session_id, created_at, metadata_json, spool_path, status,
                        attempts, received_at, updated_at, completed_at, last_error
                    ) VALUES(?,?,?,?,?,?,?,?,?,'RECEIVED',0,?,?,NULL,NULL)
                    """,
                    (
                        event_id,
                        str(request_id),
                        payload_hash,
                        payload_bytes,
                        self._text_or_none(event.get("terminal_id") or event.get("source_id")),
                        self._text_or_none(event.get("session_id")),
                        self._text_or_none(event.get("created_at")),
                        metadata_json,
                        str(spool_path),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                if isinstance(temp_path, Path) and temp_path.exists():
                    temp_path.unlink(missing_ok=True)
                if spool_path.exists():
                    # The DB insert failed after atomic rename. No record owns the file.
                    spool_path.unlink(missing_ok=True)
                raise

        return SpoolAcceptResult(
            event_id=event_id,
            request_id=request_id,
            duplicate=False,
            queued=True,
            spool_path=spool_path,
            queue_depth=queue_depth,
            payload_hash=payload_hash,
            payload_bytes=payload_bytes,
        )

    def cancel_pending_except(
        self,
        keep_event_id: str | None,
        *,
        reason: str,
    ) -> list[str]:
        """Mark all not-yet-processing events except one as completed/cancelled."""
        now = self._clock()
        with self._lock:
            connection = self._connection()
            if keep_event_id is None:
                rows = connection.execute(
                    "SELECT event_id, spool_path FROM inbound_voice_events "
                    "WHERE status IN ('RECEIVED','QUEUED','FAILED')"
                ).fetchall()
            else:
                keep = connection.execute(
                    "SELECT rowid FROM inbound_voice_events WHERE event_id=?",
                    (keep_event_id,),
                ).fetchone()
                if keep is None:
                    rows = []
                else:
                    rows = connection.execute(
                        "SELECT event_id, spool_path FROM inbound_voice_events "
                        "WHERE status IN ('RECEIVED','QUEUED','FAILED') AND rowid < ?",
                        (int(keep["rowid"]),),
                    ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                connection.execute(
                    f"UPDATE inbound_voice_events SET status='DONE', completed_at=?, "
                    f"updated_at=?, last_error=? WHERE event_id IN ({placeholders}) "
                    "AND status IN ('RECEIVED','QUEUED','FAILED')",
                    (now, now, reason[:1024], *event_ids),
                )
        for row in rows:
            Path(str(row["spool_path"])).unlink(missing_ok=True)
        return event_ids

    def mark_queued(self, event_id: str) -> bool:
        """Transition a pending record to QUEUED without downgrading active/completed work.

        The in-memory queue and the durable spool are updated by different threads.
        A worker can therefore win the race and move a record to PROCESSING before
        the producer/recovery feeder persists QUEUED.  The old unconditional update
        could then move PROCESSING back to QUEUED, allowing the same callback to be
        processed again.  Keep QUEUED idempotent, but only transition RECEIVED/FAILED.
        """
        now = self._clock()
        with self._lock:
            connection = self._connection()
            row = connection.execute(
                "SELECT status FROM inbound_voice_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            status = str(row["status"] or "")
            if status == "QUEUED":
                return True
            if status not in {"RECEIVED", "FAILED"}:
                return False
            cursor = connection.execute(
                """
                UPDATE inbound_voice_events
                SET status='QUEUED', updated_at=?
                WHERE event_id=? AND status IN ('RECEIVED','FAILED')
                """,
                (now, event_id),
            )
            return cursor.rowcount > 0

    def mark_processing(self, event_id: str) -> bool:
        """原子地把记录从待处理状态转为 PROCESSING（CAS 语义）。

        Returns:
            True 表示本次调用抢到了处理权；False 表示记录已在处理中/
            已完成/不存在，调用方必须跳过执行，避免同一回调被处理两次。
        """
        now = self._clock()
        with self._lock:
            connection = self._connection()
            cursor = connection.execute(
                """
                UPDATE inbound_voice_events
                SET status='PROCESSING', attempts=attempts+1, updated_at=?
                WHERE event_id=? AND status IN ('RECEIVED','QUEUED','FAILED')
                """,
                (now, event_id),
            )
            return cursor.rowcount > 0

    def mark_done(self, event_id: str, *, cleanup_audio: bool = True) -> None:
        now = self._clock()
        record = self.get(event_id)
        with self._lock:
            connection = self._connection()
            connection.execute(
                """
                UPDATE inbound_voice_events
                SET status='DONE', completed_at=?, updated_at=?, last_error=NULL
                WHERE event_id=?
                """,
                (now, now, event_id),
            )
        if cleanup_audio and record is not None:
            record.spool_path.unlink(missing_ok=True)

    def mark_failed(self, event_id: str, error: str) -> None:
        now = self._clock()
        with self._lock:
            connection = self._connection()
            connection.execute(
                """
                UPDATE inbound_voice_events
                SET status='FAILED', updated_at=?, last_error=? WHERE event_id=?
                """,
                (now, error[:1024], event_id),
            )

    def get(self, event_id: str) -> SpoolRecord | None:
        with self._lock:
            connection = self._connection()
            row = connection.execute(
                "SELECT * FROM inbound_voice_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return None if row is None else self._row_to_record(row)

    def load_task_data(self, event_id: str) -> dict[str, Any]:
        record = self.get(event_id)
        if record is None:
            raise KeyError(f"unknown callback event_id: {event_id}")
        raw_audio = record.spool_path.read_bytes()
        data = dict(record.metadata)
        data["audio_payload_b64"] = base64.b64encode(raw_audio).decode("ascii")
        data["payload_bytes"] = len(raw_audio)
        return data

    def recover_pending(self, *, limit: int | None = None) -> list[SpoolRecord]:
        sql = (
            "SELECT * FROM inbound_voice_events "
            "WHERE status IN ('RECEIVED','QUEUED','FAILED') ORDER BY received_at ASC"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(1, int(limit)),)
        with self._lock:
            connection = self._connection()
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def pending_event_ids(self, *, limit: int | None = None) -> list[str]:
        return [record.event_id for record in self.recover_pending(limit=limit)]

    def purge_expired(self) -> int:
        cutoff = self._clock() - self.retention_seconds
        with self._lock:
            connection = self._connection()
            rows = connection.execute(
                "SELECT event_id, spool_path FROM inbound_voice_events "
                "WHERE status='DONE' AND updated_at < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                Path(str(row["spool_path"])).unlink(missing_ok=True)
            cursor = connection.execute(
                "DELETE FROM inbound_voice_events WHERE status='DONE' AND updated_at < ?",
                (cutoff,),
            )
            return max(0, int(cursor.rowcount))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            connection = self._connection()
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count, COALESCE(SUM(payload_bytes),0) AS bytes
                FROM inbound_voice_events GROUP BY status
                """
            ).fetchall()
        states = {
            name: {"count": 0, "bytes": 0}
            for name in ("RECEIVED", "QUEUED", "PROCESSING", "DONE", "FAILED")
        }
        for row in rows:
            states[str(row["status"])] = {
                "count": int(row["count"]),
                "bytes": int(row["bytes"]),
            }
        active_count = sum(states[name]["count"] for name in ("RECEIVED", "QUEUED", "PROCESSING", "FAILED"))
        active_bytes = sum(states[name]["bytes"] for name in ("RECEIVED", "QUEUED", "PROCESSING", "FAILED"))
        return {
            "root": str(self.root),
            "schema_version": self.SCHEMA_VERSION,
            "states": states,
            "active_count": active_count,
            "active_bytes": active_bytes,
            "max_events": self.max_events,
            "max_bytes": self.max_bytes,
            "max_audio_bytes": self.max_audio_bytes,
        }

    def _set_status(self, event_id: str, status: str) -> None:
        with self._lock:
            connection = self._connection()
            connection.execute(
                "UPDATE inbound_voice_events SET status=?, updated_at=? WHERE event_id=?",
                (status, self._clock(), event_id),
            )

    @staticmethod
    def _decode_audio(event: dict[str, Any]) -> bytes:
        value = event.get("audio_payload_b64") or event.get("audio_b64")
        if not value:
            raise CallbackPayloadInvalid("missing audio_payload_b64")
        try:
            decoded = base64.b64decode(str(value), validate=True)
        except Exception as exc:
            raise CallbackPayloadInvalid(f"invalid audio Base64: {type(exc).__name__}") from exc
        if not decoded:
            raise CallbackPayloadInvalid("empty audio payload")
        return decoded

    @classmethod
    def _metadata_without_audio(cls, event: dict[str, Any]) -> dict[str, Any]:
        def scrub(value: Any, key: str | None = None) -> Any:
            if key in {"audio_payload_b64", "audio_b64", "udp_payload_b64"}:
                return None
            if isinstance(value, dict):
                return {
                    k: cleaned
                    for k, v in value.items()
                    if (cleaned := scrub(v, k)) is not None
                }
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        cleaned = scrub(event)
        assert isinstance(cleaned, dict)
        return cleaned

    @staticmethod
    def _text_or_none(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)[:256]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SpoolRecord:
        metadata = json.loads(str(row["metadata_json"]))
        if not isinstance(metadata, dict):
            raise ValueError("callback spool metadata must be a JSON object")
        return SpoolRecord(
            event_id=str(row["event_id"]),
            request_id=str(row["request_id"]),
            payload_hash=str(row["payload_hash"]),
            payload_bytes=int(row["payload_bytes"]),
            spool_path=Path(str(row["spool_path"])),
            metadata=metadata,
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            received_at=float(row["received_at"]),
            updated_at=float(row["updated_at"]),
        )
