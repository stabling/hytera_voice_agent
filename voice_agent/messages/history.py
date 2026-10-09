# =============================================================================
# 【维护注释】文件：voice_agent/messages/history.py
# 所属层次：用户历史查询层
# 主要职责：scope=user 强制的 user_voice/user_sms 历史查询、倒数第 N 通电话
#           （position_from_latest）、call_id 访问权限校验（需求 §19-§24）。
# 项目内直接依赖：.metrics, .store
# 主要公开符号：UserHistoryService
# 兼容性：数据源只能为 user_voice.sqlite3 / user_sms.sqlite3，即使调用方传
#         control/all 也不放宽（需求 §19）。
# 修改原则：actor_terminal_id 来自控制请求 source_id（需求 §20）；电话序号只统计
#           user_voice，排除 control/direct-text/ASR 失败/空 call_id（需求 §24）。
# 排障顺序：先看 actor_terminal_id 与 scope，再对照 user_voice 过滤规则。
# =============================================================================
"""用户历史查询服务（需求 §19-§24）。"""

from __future__ import annotations

import threading
from typing import Any

from .metrics import MessageMetrics
from .store import MessageArchiveStore

USER_SCOPE = "user"

# 倒数第 N 通的排除口径（需求 §24）。
_EXCLUDED_ORIGINS = {"direct-text", "text", "sms", "test"}
_EXCLUDED_STATUS = {"asr_failed"}


class UserHistoryService:
    """只能读 user_voice / user_sms 的历史查询门面。"""

    def __init__(
        self,
        store: MessageArchiveStore,
        metrics: MessageMetrics | None = None,
        *,
        max_limit: int = 200,
        classify_control_ids: Any = None,
    ) -> None:
        self.store = store
        self.metrics = metrics
        self.max_limit = max(1, int(max_limit))
        self._lock = threading.RLock()
        self._classify = classify_control_ids

    # ---- 通用 -----------------------------------------------------------
    def _limit(self, value: Any, default: int = 20) -> int:
        try:
            return max(1, min(int(value or default), self.max_limit))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _actor(actor_terminal_id: Any) -> str:
        text = str(actor_terminal_id or "").strip()
        if not text:
            raise PermissionError("actor_terminal_id is required")
        return text

    def _enforce_scope(self, scope: Any) -> str:
        value = str(scope or USER_SCOPE).strip().lower()
        if value != USER_SCOPE:
            # 需求 §19：即使传入 control/all 也不放宽，强制回退为 user。
            value = USER_SCOPE
        return value

    def _observe(self, timer_name: str, milliseconds: float) -> None:
        if self.metrics is not None:
            self.metrics.observe(timer_name, milliseconds)
            self.metrics.inc("history_query_count")

    # ---- 通话过滤口径 ----------------------------------------------------
    @staticmethod
    def _is_valid_user_call(row: dict[str, Any]) -> bool:
        """倒数第 N 通/最近通话的合法记录（需求 §24）。"""
        if not str(row.get("call_id") or "").strip():
            return False
        if str(row.get("origin") or "").strip().lower() in _EXCLUDED_ORIGINS:
            return False
        if str(row.get("status") or "").strip().lower() in _EXCLUDED_STATUS:
            return False
        if not str(row.get("text") or "").strip():
            return False
        return True

    def _user_call_rows(self, actor: str, *, days: int = 30, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self.store.messages_for_actor(
            "user_voice",
            actor_id=actor,
            days=days,
            limit=self.max_limit if limit is None else limit,
        )
        return [row for row in rows if self._is_valid_user_call(row)]

    # ---- 查询接口 ---------------------------------------------------------
    def recent_calls(
        self,
        *,
        actor_terminal_id: str,
        days: int = 7,
        limit: int = 20,
        scope: str = USER_SCOPE,
    ) -> dict[str, Any]:
        import time as _time

        self._enforce_scope(scope)
        actor = self._actor(actor_terminal_id)
        started = _time.perf_counter()
        with self._lock:
            valid_rows = self._user_call_rows(actor, days=int(days))
        display = valid_rows[: self._limit(limit)]
        call_id = str(valid_rows[0].get("call_id")) if valid_rows else ""
        elapsed = (_time.perf_counter() - started) * 1000.0
        self._observe("history_query_ms", elapsed)
        return {
            "schema_version": 1,
            "ok": True,
            "scope": USER_SCOPE,
            "actor_terminal_id": actor,
            "call_id": call_id,
            "calls": display,
            "count": len(display),
        }

    def call_by_position(
        self,
        *,
        actor_terminal_id: str,
        position_from_latest: int,
        scope: str = USER_SCOPE,
    ) -> dict[str, Any]:
        """倒数第 N 通电话（需求 §23）：1=最近一通，N=倒数第 N 通。"""
        import time as _time

        self._enforce_scope(scope)
        actor = self._actor(actor_terminal_id)
        try:
            position = int(position_from_latest)
        except (TypeError, ValueError):
            raise ValueError("position_from_latest must be a positive integer") from None
        if position < 1:
            raise ValueError("position_from_latest must be >= 1")
        started = _time.perf_counter()
        with self._lock:
            valid_rows = self._user_call_rows(actor)
        elapsed = (_time.perf_counter() - started) * 1000.0
        self._observe("history_query_ms", elapsed)
        if position > len(valid_rows):
            return {
                "schema_version": 1,
                "ok": True,
                "scope": USER_SCOPE,
                "actor_terminal_id": actor,
                "position_from_latest": position,
                "call_id": "",
                "call": None,
                "count": len(valid_rows),
                "message": "position_from_latest 超出该用户的历史通话数量",
            }
        row = valid_rows[position - 1]
        return {
            "schema_version": 1,
            "ok": True,
            "scope": USER_SCOPE,
            "actor_terminal_id": actor,
            "position_from_latest": position,
            "call_id": str(row.get("call_id") or ""),
            "call": row,
            "count": len(valid_rows),
        }

    def recent_sms(
        self,
        *,
        actor_terminal_id: str,
        limit: int = 20,
        start_time: str | None = None,
        end_time: str | None = None,
        contact_id: str | None = None,
        keyword: str | None = None,
        direction: str | None = None,
        source_id: str | None = None,
        target_id: str | None = None,
        group_id: str | None = None,
        scope: str = USER_SCOPE,
    ) -> dict[str, Any]:
        """历史短信查询（需求 §21）：只读 user_sms。"""
        import time as _time

        self._enforce_scope(scope)
        actor = self._actor(actor_terminal_id)
        started = _time.perf_counter()
        with self._lock:
            rows = self.store.recent_sms(
                actor_id=actor,
                start_time=start_time,
                end_time=end_time,
                contact_id=contact_id,
                keyword=keyword,
                direction=direction,
                source_id=source_id,
                target_id=target_id,
                group_id=group_id,
                limit=self._limit(limit, default=50),
            )
        elapsed = (_time.perf_counter() - started) * 1000.0
        self._observe("history_query_ms", elapsed)
        return {
            "schema_version": 1,
            "ok": True,
            "scope": USER_SCOPE,
            "actor_terminal_id": actor,
            "messages": rows,
            "count": len(rows),
        }

    # ---- 权限校验（需求 §22/§37） -----------------------------------------
    def call_access(self, actor_terminal_id: str, call_id: str) -> bool | None:
        """判断 call_id 是否属于该 actor 可访问的 user_voice。

        - True：user_voice 中存在该 actor 参与的记录；
        - False：call_id 只存在于 control_voice（或 user_voice 中该 actor 无权访问）
          ——禁止绕过历史入口读取控制通话；
        - None：四库中均无记录（CallStore 旧数据等），由调用方决定兼容策略。
        """
        actor = self._actor(actor_terminal_id)
        call = str(call_id or "").strip()
        if not call:
            return False
        with self._lock:
            user_rows = self.store.find_by_call_id("user_voice", call)
            control_rows = self.store.find_by_call_id("control_voice", call)
        if any(
            str(row.get("source_id") or "") == actor
            or str(row.get("target_id") or "") == actor
            for row in user_rows
        ):
            return True
        if control_rows or user_rows:
            return False
        return None
