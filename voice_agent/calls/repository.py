# =============================================================================
# 【维护注释】文件：voice_agent/calls/repository.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.models, voice_agent.storage.runtime, .models
# 主要公开符号：CallStoreRepository
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""SQLite WAL repository for the independent runtime."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from shared_runtime.sqlite_compat import configure_durable_journal
from shared_contracts.models import utc_now_iso

from voice_agent.storage.runtime import mirror_record
from .migration_runner import run_call_store_migrations
from .models import CallParticipant, CallSelector, CallSession, CallTurn


class CallStoreRepository:
    SCHEMA_VERSION = 3
    PROJECTOR_NAME = "call_lifecycle"

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

    def migration_versions(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version FROM migration_version ORDER BY version"
            ).fetchall()
        return [int(row["version"]) for row in rows]

    def upsert_session(self, session: CallSession) -> CallSession:
        values = session.model_dump(mode="json")
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        update_columns = [name for name in columns if name not in {"call_id", "created_at"}]
        update_sql = ",".join(f"{name}=excluded.{name}" for name in update_columns)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    f"INSERT INTO call_session({','.join(columns)}) VALUES({placeholders}) "
                    f"ON CONFLICT(call_id) DO UPDATE SET {update_sql}",
                    [values[name] for name in columns],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        stored = self.get_session(session.call_id)
        if stored is None:  # pragma: no cover - defensive
            raise RuntimeError(f"call session was not persisted: {session.call_id}")
        payload = stored.model_dump(mode="json", exclude_none=True)
        mirror_record(
            "call",
            stored.call_id,
            payload,
            actor_id=stored.observed_source_id or stored.resolved_source_id,
            terminal_id=stored.observed_target_id or stored.resolved_target_id,
        )
        return stored

    def get_session(self, call_id: str) -> CallSession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM call_session WHERE call_id=?", (str(call_id),)
            ).fetchone()
        return None if row is None else CallSession.model_validate(dict(row))

    def find_by_event_id(self, event_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT call_id FROM call_event_link WHERE event_id=? LIMIT 1",
                (str(event_id),),
            ).fetchone()
        return None if row is None else str(row["call_id"])

    def active_sessions(self, *, limit: int = 100) -> list[CallSession]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM call_session
                WHERE status NOT IN ('ended','failed','rejected','timeout')
                ORDER BY COALESCE(last_audio_at, answered_at, ringing_at, requested_at, created_at) DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [CallSession.model_validate(dict(row)) for row in rows]

    def list_sessions(self, selector: CallSelector | dict[str, Any]) -> list[dict[str, Any]]:
        q = selector if isinstance(selector, CallSelector) else CallSelector.model_validate(selector)
        clauses: list[str] = []
        params: list[Any] = []
        equality = {
            "call_id": q.call_id,
            "observed_source_id": q.source_id,
            "observed_target_id": q.target_id,
            "group_id": q.group_id,
            "initial_channel_id": q.channel_id,
            "status": q.status,
            "direction": q.direction,
        }
        for column, value in equality.items():
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(str(value))
        start_expr = "COALESCE(requested_at, ringing_at, answered_at, first_audio_at, created_at)"
        started_after = q.start_time or q.started_after
        started_before = q.end_time or q.started_before
        if started_after:
            clauses.append(f"{start_expr}>=?")
            params.append(started_after)
        if started_before:
            clauses.append(f"{start_expr}<=?")
            params.append(started_before)
        if q.statuses:
            placeholders = ",".join("?" for _ in q.statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(q.statuses)
        if q.contact_id:
            clauses.append(
                "(observed_source_id=? OR observed_target_id=? OR resolved_source_id=? OR "
                "resolved_target_id=? OR group_id=?)"
            )
            params.extend([q.contact_id] * 5)
        authorized_terminal = q.authorized_terminal_id or q.source_terminal_id
        if authorized_terminal:
            # 同频道/同组共现口径：对讲频道（observed_target_id=频道逻辑 ID）
            # 与组呼（group_id）是公开半双工信道，成员物理上能听到频道内
            # 所有发言。授权终端除直连端点外，也能看到发往"自己参与过的
            # 频道/组"的通话（2026-09-29 真机复现：1048583 问"上一通电话"
            # 时查不到同频道 1048582 的北环大道通报，只能命中自己的查询
            # 语句形成的会话）。私人呼叫（target=对端电台）不共享：仅当
            # 双方都直接参与该 target 才可见。
            actor_endpoint_match = (
                "(observed_source_id=? OR observed_target_id=? "
                "OR resolved_source_id=? OR resolved_target_id=?)"
            )
            shared_channel_match = (
                "(observed_target_id IN (SELECT DISTINCT observed_target_id FROM call_session "
                f"WHERE {actor_endpoint_match} AND observed_target_id IS NOT NULL) "
                "OR group_id IN (SELECT DISTINCT group_id FROM call_session "
                f"WHERE {actor_endpoint_match} AND group_id IS NOT NULL))"
            )
            clauses.append(f"({actor_endpoint_match} OR {shared_channel_match})")
            params.extend([authorized_terminal] * 12)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT * FROM call_session"
            + where
            + f" ORDER BY {start_expr} DESC, call_id DESC LIMIT ? OFFSET ?"
        )
        params.extend([q.limit, q.offset])
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def add_participant(self, participant: CallParticipant) -> bool:
        values = participant.model_dump(mode="json")
        columns = list(values)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO call_participant({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                [values[name] for name in columns],
            )
        return int(cursor.rowcount) == 1

    def participants(self, call_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM call_participant WHERE call_id=? ORDER BY role, participant_id",
                (str(call_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_turn(self, turn: CallTurn) -> tuple[bool, CallTurn]:
        values = turn.model_dump(mode="json")
        values["asr_ok"] = 1 if turn.asr_ok else 0
        values["late_event"] = 1 if turn.late_event else 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM call_turn WHERE event_id=?", (turn.event_id,)
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return False, self._row_to_turn(existing)
                sequence_no = turn.sequence_no
                if sequence_no is None:
                    row = connection.execute(
                        "SELECT COALESCE(MAX(sequence_no),0)+1 AS next_no FROM call_turn WHERE call_id=?",
                        (turn.call_id,),
                    ).fetchone()
                    sequence_no = int(row["next_no"])
                values["sequence_no"] = sequence_no
                columns = list(values)
                connection.execute(
                    f"INSERT INTO call_turn({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [values[name] for name in columns],
                )
                row = connection.execute(
                    "SELECT * FROM call_turn WHERE event_id=?", (turn.event_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert row is not None
        return True, self._row_to_turn(row)

    def turns(self, call_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM call_turn WHERE call_id=?
                ORDER BY COALESCE(sequence_no, 2147483647), COALESCE(started_at, created_at), event_id
                """,
                (str(call_id),),
            ).fetchall()
        return [self._turn_dict(row) for row in rows]

    def summarizable_call_ids(self, call_ids: Iterable[str]) -> set[str]:
        """Return calls containing at least one non-synthetic transcript turn.

        The predicate mirrors ``HistoryQueryService``'s existing Python rule:
        use ``transcript`` when it is non-empty, otherwise ``normalized_text``;
        ignore whitespace-only content; and exclude ``direct-text`` turns.
        控制指令语音（message_category 以 control_ 开头）是对智能体的命令
        输入而非通话内容，与 direct-text 同样排除：否则"上一通电话"的
        hand-off 会命中查询语句自身形成的会话（2026-09-29 真机复现）。
        A bounded history scan currently supplies at most 200 ids, so a single
        parameterized ``IN`` query is sufficient and removes the N+1 lookup.
        """

        normalized_ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in call_ids
                if value is not None and str(value).strip()
            )
        )
        if not normalized_ids:
            return set()

        placeholders = ",".join("?" for _ in normalized_ids)
        sql = f"""
            SELECT DISTINCT call_id
            FROM call_turn
            WHERE call_id IN ({placeholders})
              AND TRIM(
                    CASE
                        WHEN transcript IS NOT NULL AND transcript <> '' THEN transcript
                        ELSE COALESCE(normalized_text, '')
                    END
                  ) <> ''
              AND LOWER(TRIM(COALESCE(asr_model, ''))) <> 'direct-text'
              AND COALESCE(message_category, '') NOT LIKE 'control_%'
        """
        with self._connect() as connection:
            rows = connection.execute(sql, normalized_ids).fetchall()
        return {str(row["call_id"]) for row in rows}

    def link_event(
        self,
        *,
        call_id: str,
        event_id: str,
        event_type: str,
        relation: str,
        occurred_at: str,
        confidence: float,
        evidence: Iterable[str] = (),
        late_event: bool = False,
    ) -> bool:
        evidence_json = json.dumps(
            sorted({str(item) for item in evidence if item}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO call_event_link(
                    call_id,event_id,event_type,relation,occurred_at,confidence,
                    evidence_json,late_event,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    call_id,
                    event_id,
                    event_type,
                    relation,
                    occurred_at,
                    max(0.0, min(1.0, float(confidence))),
                    evidence_json,
                    1 if late_event else 0,
                    utc_now_iso(),
                ),
            )
        return int(cursor.rowcount) == 1

    def event_links(self, call_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM call_event_link WHERE call_id=? ORDER BY occurred_at, event_id",
                (str(call_id),),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(str(item.pop("evidence_json")))
            except (json.JSONDecodeError, TypeError):
                item["evidence"] = []
            item["late_event"] = bool(item["late_event"])
            values.append(item)
        return values

    def link_capture(
        self,
        *,
        call_id: str,
        capture_session_id: str,
        audio_path: str | None,
        audio_sha256: str | None,
        started_at: str | None,
        ended_at: str | None,
        confidence: float,
        evidence_event_id: str | None,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO call_capture_link(
                    call_id,capture_session_id,audio_path,audio_sha256,started_at,ended_at,
                    confidence,evidence_event_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(call_id,capture_session_id) DO UPDATE SET
                    audio_path=COALESCE(excluded.audio_path,call_capture_link.audio_path),
                    audio_sha256=COALESCE(excluded.audio_sha256,call_capture_link.audio_sha256),
                    started_at=COALESCE(call_capture_link.started_at,excluded.started_at),
                    ended_at=COALESCE(excluded.ended_at,call_capture_link.ended_at),
                    confidence=MAX(call_capture_link.confidence,excluded.confidence),
                    evidence_event_id=COALESCE(excluded.evidence_event_id,call_capture_link.evidence_event_id)
                """,
                (
                    call_id,
                    capture_session_id,
                    audio_path,
                    audio_sha256,
                    started_at,
                    ended_at,
                    max(0.0, min(1.0, float(confidence))),
                    evidence_event_id,
                    utc_now_iso(),
                ),
            )
        return int(cursor.rowcount) >= 1

    def captures(self, call_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM call_capture_link WHERE call_id=? ORDER BY started_at, capture_session_id",
                (str(call_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_call(self, call_id: str, *, include_transcript: bool = True) -> dict[str, Any] | None:
        session = self.get_session(call_id)
        if session is None:
            return None
        result = session.model_dump(mode="json")
        result["participants"] = self.participants(call_id)
        result["turns"] = self.turns(call_id)
        if not include_transcript:
            for turn in result["turns"]:
                turn["transcript"] = "<redacted>" if turn.get("transcript") else None
                turn["normalized_text"] = "<redacted>" if turn.get("normalized_text") else None
        result["event_links"] = self.event_links(call_id)
        result["capture_links"] = self.captures(call_id)
        return result

    def close_stale_sessions(
        self,
        *,
        max_age_seconds: float = 86400.0,
        end_reason: str = "stale_active_sweep",
    ) -> int:
        """把长期停留在非终态的会话批量置为 ended，返回关闭数量。

        事件链断裂（缺 call.ended 事件、进程重启丢上下文）会让 active
        会话永久存活，之后通过 request_id 回绕/抓包会话复用不断吞掉
        新音频（2026-09-29 真机复现：8 月 25 日的 active 会话存活 35
        天，吞掉当日通话后半段）。以最近活动时间
        COALESCE(last_audio_at, answered_at, ringing_at, requested_at, created_at)
        计龄，超过 max_age_seconds 即关闭；默认 24 小时（隔夜 active
        即视为异常遗留）。
        """
        threshold = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1.0, float(max_age_seconds)))
        ).isoformat()
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE call_session
                    SET status='ended', end_reason=?,
                        ended_at=COALESCE(ended_at, ?), updated_at=?
                    WHERE status NOT IN ('ended','failed','rejected','timeout')
                      AND COALESCE(last_audio_at, answered_at, ringing_at, requested_at, created_at) < ?
                    """,
                    (str(end_reason), now, now, threshold),
                )
                count = int(cursor.rowcount or 0)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return count

    def get_projector_offset(self, projector_name: str = PROJECTOR_NAME) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_journal_offset FROM projector_offset WHERE projector_name=?",
                (str(projector_name),),
            ).fetchone()
        return 0 if row is None else int(row["last_journal_offset"])

    def set_projector_offset(
        self, offset: int, projector_name: str = PROJECTOR_NAME
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projector_offset(projector_name,last_journal_offset,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(projector_name) DO UPDATE SET
                    last_journal_offset=MAX(projector_offset.last_journal_offset,excluded.last_journal_offset),
                    updated_at=excluded.updated_at
                """,
                (str(projector_name), max(0, int(offset)), utc_now_iso()),
            )

    def metrics(self, *, low_confidence_threshold: float = 0.75) -> dict[str, Any]:
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM call_session").fetchone()[0])
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN observed_source_id IS NOT NULL THEN 1 ELSE 0 END) AS source_count,
                    SUM(CASE WHEN observed_target_id IS NOT NULL OR group_id IS NOT NULL THEN 1 ELSE 0 END) AS target_count,
                    SUM(CASE WHEN requested_at IS NOT NULL OR first_audio_at IS NOT NULL THEN 1 ELSE 0 END) AS time_count,
                    SUM(CASE WHEN correlation_confidence < ? THEN 1 ELSE 0 END) AS low_confidence_count
                FROM call_session
                """,
                (float(low_confidence_threshold),),
            ).fetchone()
            transcript_calls = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT call_id) FROM call_turn WHERE LENGTH(TRIM(COALESCE(transcript,'')))>0"
                ).fetchone()[0]
            )
            turn_total = int(connection.execute("SELECT COUNT(*) FROM call_turn").fetchone()[0])
            late_events = int(
                connection.execute(
                    "SELECT COUNT(*) FROM call_event_link WHERE late_event=1"
                ).fetchone()[0]
            )
        denominator = total or 1
        return {
            "schema_version": self.SCHEMA_VERSION,
            "path": str(self.path),
            "call_total": total,
            "turn_total": turn_total,
            "source_coverage": float(row["source_count"] or 0) / denominator,
            "target_coverage": float(row["target_count"] or 0) / denominator,
            "time_coverage": float(row["time_count"] or 0) / denominator,
            "transcript_coverage": transcript_calls / denominator,
            "low_confidence_count": int(row["low_confidence_count"] or 0),
            "low_confidence_ratio": int(row["low_confidence_count"] or 0) / denominator,
            "late_event_count": late_events,
            "projector_offset": self.get_projector_offset(),
        }

    def explain_recent_query(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM call_session "
                "ORDER BY COALESCE(requested_at, first_audio_at, created_at) DESC LIMIT 100"
            ).fetchall()
        return [str(row["detail"]) for row in rows]

    @staticmethod
    def _row_to_turn(row: sqlite3.Row) -> CallTurn:
        value = dict(row)
        value["asr_ok"] = bool(value["asr_ok"])
        value["late_event"] = bool(value["late_event"])
        return CallTurn.model_validate(value)

    @classmethod
    def _turn_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        return cls._row_to_turn(row).model_dump(mode="json")
