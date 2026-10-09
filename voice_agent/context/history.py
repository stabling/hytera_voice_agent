# ============================================================================
# 【维护者中文说明：调用关系与稳定性边界】
# 文件：voice_agent/context/history.py
# 模块：voice_agent.context.history
# 分层：x64 智能体业务层
# 直接上游（静态 import 当前模块）：voice_agent.context、voice_agent.context.runtime
# 直接下游（当前模块静态 import）：无
# 稳定性要求：允许持续扩展能力，但工具 ID、服务边界和旧能力参数契约应保持稳定；新增能力优先通过注册表/YAML 接入。
# 测试原则：单文件自测默认只检查 AST 与项目内 import 调用图，不启动服务、不访问网络、
#           不连接设备、不执行 ADK/MCP 副作用；加 --import-deps 可进一步做依赖导入烟测。
# 自测命令：python -m voice_agent.context.history
# 维护规则：业务规则应放在所属层的唯一实现中；兼容入口只做代理，禁止复制一份相同逻辑。
# ============================================================================
"""Read-only, parameterized communication-history service."""

from __future__ import annotations

from typing import Any, Callable

from shared_runtime.logging_config import get_logger
from shared_runtime.timing import DEFAULT_TIMING_PLUGIN, timed_call

logger = get_logger(__name__)


class HistoryQueryService:
    def __init__(
        self,
        repository: Any,
        *,
        max_limit: int = 200,
        timing_plugin: Any | None = None,
        call_access: Callable[[str, str], bool | None] | None = None,
    ) -> None:
        self.repository = repository
        self.max_limit = max(1, int(max_limit))
        self.timing_plugin = timing_plugin or DEFAULT_TIMING_PLUGIN
        # 归档权限视图回调（需求 §22 / wiki 08 §2）：False=控制通话，
        # 从最近通话列表与 hand-off 候选中剔除；None=归档无记录的
        # CallStore 旧数据，保持兼容放行；未注入时不过滤。
        self.call_access = call_access

    def _timed(self, event: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        fields = kwargs.pop("_timing_fields", None)
        return timed_call(
            logger,
            event,
            func,
            *args,
            timing_plugin=self.timing_plugin,
            fields=fields,
            **kwargs,
        )

    def _limit(self, value: Any) -> int:
        return max(1, min(int(value or 20), self.max_limit))

    @staticmethod
    def _scope(actor_terminal_id: Any) -> str:
        value = str(actor_terminal_id or "").strip()
        if not value:
            raise PermissionError("actor_terminal_id is required")
        return value

    @staticmethod
    def _is_completed_call(row: dict[str, Any]) -> bool:
        """Accept completed CallStore rows and legacy rows without status fields."""

        status = str(row.get("status") or "").strip().lower()
        if status:
            return status in {"ended", "completed"}
        return bool(str(row.get("ended_at") or "").strip()) or "status" not in row

    def _has_summarizable_transcript(self, row: dict[str, Any]) -> bool | None:
        """Return whether a call has real transcript content suitable for hand-off.

        ``None`` means the repository does not expose transcript turns (for
        example a compatibility/mock repository).  In that case callers fall
        back to the legacy completed-call rule.

        Direct text API commands intentionally use the ``direct-text`` ASR model.
        They are command inputs, not historical radio/telephone conversations,
        and therefore must not become the target of "summarize the last call".
        Spoken control commands (``message_category`` starting with
        ``control_``) are excluded for the same reason: they are queries to
        the agent, not conversational content to summarize.
        """

        call_id = str(row.get("call_id") or "").strip()
        if not call_id:
            return False
        turns_reader = getattr(self.repository, "call_turns", None)
        if not callable(turns_reader):
            return None
        turns = list(self._timed("历史查询数据库耗时.call_turns", turns_reader, call_id, _timing_fields={"call_id": call_id}))
        transcript_turns = [
            item
            for item in turns
            if str(item.get("transcript") or item.get("normalized_text") or "").strip()
        ]
        if not transcript_turns:
            return False
        # A call containing at least one non-synthetic transcript is a valid
        # summary source.  Mixed records are retained rather than discarded.
        return any(
            str(item.get("asr_model") or "").strip().lower() != "direct-text"
            and not str(item.get("message_category") or "").strip().lower().startswith("control_")
            for item in transcript_turns
        )

    def _accessible_rows(self, terminal: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按归档权限视图剔除 actor 不可访问的通话（wiki 08 §2 / 需求 §22）。

        ``call_access(actor, call_id)`` 返回 False 表示该 call_id 只存在于
        control_voice（控制通话，禁止经历史入口读取），必须从展示列表和
        hand-off 候选中同时剔除，否则排在前面的控制通话会遮蔽后面真正
        可访问的用户通话。True 与 None（归档无记录的旧数据）均放行；
        未注入 call_access 时不过滤。
        """
        if self.call_access is None:
            return rows
        verdicts: dict[str, bool] = {}
        kept: list[dict[str, Any]] = []
        for row in rows:
            call_id = str(row.get("call_id") or "").strip()
            if not call_id:
                kept.append(row)
                continue
            if call_id not in verdicts:
                try:
                    verdicts[call_id] = self.call_access(terminal, call_id) is not False
                except Exception:
                    logger.warning("历史查询 call_access 校验异常，按兼容放行 call_id=%s", call_id, exc_info=True)
                    verdicts[call_id] = True
            if verdicts[call_id]:
                kept.append(row)
        return kept

    def _handoff_call_id(self, rows: list[dict[str, Any]]) -> str:
        """Pick the newest call that downstream single-call tools can consume."""

        bulk_reader = getattr(self.repository, "summarizable_call_ids", None)
        if callable(bulk_reader):
            call_ids = [
                str(row.get("call_id") or "").strip()
                for row in rows
                if str(row.get("call_id") or "").strip()
            ]
            summarizable_ids = self._timed("历史查询数据库耗时.summarizable_call_ids", bulk_reader, call_ids, _timing_fields={"candidate_count": len(call_ids)})
            if summarizable_ids is not None:
                allowed = set(summarizable_ids)
                for row in rows:
                    call_id = str(row.get("call_id") or "").strip()
                    if call_id and call_id in allowed:
                        return call_id
                return ""

        for row in rows:
            call_id = str(row.get("call_id") or "").strip()
            if not call_id:
                continue
            summarizable = self._has_summarizable_transcript(row)
            if summarizable is True:
                return call_id
            if summarizable is None and self._is_completed_call(row):
                return call_id
        return ""

    def recent_calls(self, *, actor_terminal_id: str, days: int = 7, limit: int = 20) -> dict[str, Any]:
        terminal = self._scope(actor_terminal_id)
        requested_limit = self._limit(limit)

        # The repository scope contains calls where the requester participated
        # as either caller or callee.  Scan up to the service's bounded maximum
        # before applying the transcript-aware hand-off rule; otherwise newer
        # synthetic/empty records could hide the latest usable conversation
        # merely because it fell just outside the display limit.
        scanned_rows = self._timed(
            "历史查询数据库耗时.recent_calls",
            self.repository.recent_calls,
            terminal_id=terminal,
            days=int(days),
            limit=self.max_limit,
            _timing_fields={"terminal_id": terminal, "days": int(days), "limit": self.max_limit},
        )
        accessible_rows = self._accessible_rows(terminal, list(scanned_rows))
        rows = list(accessible_rows[:requested_limit])

        # ``history_recent_calls`` historically returned only ``calls``.  The
        # planner can only pass scalar references such as ``${step_id.field}``,
        # so expose a scalar ``call_id`` for downstream call-content tools.
        # Selection intentionally scans beyond the display limit and requires a
        # transcript-bearing call.  This prevents the current /text/input command
        # (or an empty outbound request) from being handed to ``call_summarize``.
        call_id = self._handoff_call_id(list(accessible_rows))
        return {
            "schema_version": 1,
            "ok": True,
            "call_id": call_id,
            "calls": rows,
            "count": len(rows),
        }

    def entity_timeline(
        self, *, actor_terminal_id: str, entity_id: str, limit: int = 50
    ) -> dict[str, Any]:
        terminal = self._scope(actor_terminal_id)
        entity = str(entity_id or "").strip()
        if not entity:
            raise ValueError("entity_id is required")
        rows = self._timed(
            "历史查询数据库耗时.entity_timeline",
            self.repository.entity_timeline,
            entity_id=entity, terminal_id=terminal, actor_id=terminal, limit=self._limit(limit),
            _timing_fields={"entity_id": entity, "terminal_id": terminal},
        )
        return {"schema_version": 1, "ok": True, "timeline": rows, "count": len(rows)}

    def similar_outcomes(
        self, *, actor_terminal_id: str, operation_type: str, limit: int = 20
    ) -> dict[str, Any]:
        actor = self._scope(actor_terminal_id)
        operation = str(operation_type or "").strip()
        if not operation:
            raise ValueError("operation_type is required")
        rows = self._timed(
            "历史查询数据库耗时.similar_outcomes",
            self.repository.similar_outcomes,
            actor_id=actor, operation_type=operation, limit=self._limit(limit),
            _timing_fields={"actor_id": actor, "operation_type": operation},
        )
        return {"schema_version": 1, "ok": True, "outcomes": rows, "count": len(rows)}

    def open_tasks(self, *, actor_terminal_id: str, limit: int = 20) -> dict[str, Any]:
        actor = self._scope(actor_terminal_id)
        rows = self._timed("历史查询数据库耗时.open_tasks", self.repository.open_tasks, actor_id=actor, limit=self._limit(limit), _timing_fields={"actor_id": actor})
        return {"schema_version": 1, "ok": True, "tasks": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# 单文件调用关系自测入口（统一委托 shared_runtime.module_selftest，避免复制测试逻辑）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _selftest = __import__("importlib").import_module("shared_runtime.module_selftest")
    raise SystemExit(
        _selftest.run_module_self_test(
            __file__,
            import_dependencies="--import-deps" in __import__("sys").argv,
        )
    )
