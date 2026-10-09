# =============================================================================
# 【维护注释】文件：voice_agent/calls/resolver.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：.models, .repository
# 主要公开符号：CallResolver
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Resolve a runtime, stable ``call_id``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import CallResolveResult, CallSelector
from .repository import CallStoreRepository

_TERMINAL_STATUSES = {"ended", "failed", "rejected", "timeout"}


@dataclass(frozen=True)
class CallResolver:
    repository: CallStoreRepository
    default_scope: str = "same_actor_terminal"
    max_candidates: int = 10
    # 用户间通话授权回退（需求 §22）：显式 call_id 的端点四元组检查未
    # 命中时，回调 (actor_terminal_id, call_id) -> True 表示归档 user_voice
    # 中该 actor 是 source/target（用户间通话参与方），放行。频道广播场景
    # 下 CallStore 的 observed_target 可能投影为中转台 ID（CONTROL_IDS），
    # 端点四元组不含真实接收方（2026-09-29 真机复现：1048583 问"上一通电
    # 话"被 FORBIDDEN 拒绝，而归档 user_voice 的 target 正是 1048583）。
    # 未注入 / None / False / 回调异常均保持原端点口径（保守拒绝）。
    user_voice_access: Callable[[str, str], bool | None] | None = None

    def resolve(
        self,
        selector: CallSelector | dict[str, Any],
        *,
        actor_terminal_id: str | None = None,
    ) -> CallResolveResult:
        query = selector if isinstance(selector, CallSelector) else CallSelector.model_validate(selector)
        if query.relative_type in {"next", "future", "unknown"}:
            return CallResolveResult(ok=False, error_code="TARGET_NOT_RESOLVED")

        terminal = str(actor_terminal_id or query.source_terminal_id or "").strip() or None
        if self.default_scope == "same_actor_terminal" and not terminal:
            return CallResolveResult(ok=False, error_code="FORBIDDEN")

        if query.call_id:
            session = self.repository.get_session(query.call_id)
            if session is None:
                return CallResolveResult(ok=False, error_code="CALL_NOT_FOUND")
            if terminal and not self._authorized(session.model_dump(mode="json"), terminal):
                if not self._user_voice_authorized(terminal, query.call_id):
                    return CallResolveResult(ok=False, error_code="FORBIDDEN")
            return CallResolveResult(
                ok=True,
                call_id=session.call_id,
                candidates=[self._candidate(session.model_dump(mode="json"))],
                resolution_method="explicit_call_id",
            )

        payload = query.model_dump(mode="json")
        payload["authorized_terminal_id"] = terminal
        payload["limit"] = min(self.max_candidates + 1, max(1, query.limit))
        payload["offset"] = 0
        if query.relative_type == "current_active":
            payload["statuses"] = ["observed", "requested", "ringing", "answered", "active"]
        elif query.relative_type == "latest_completed":
            payload["statuses"] = sorted(_TERMINAL_STATUSES)
        candidates = self.repository.list_sessions(payload)

        if not candidates:
            return CallResolveResult(ok=False, error_code="CALL_NOT_FOUND")
        if query.relative_type == "latest_completed":
            # "latest" is deterministic only when the top candidate has a strictly
            # later effective time than the second result. Equal timestamps remain
            # ambiguous rather than silently selecting by display order.
            if len(candidates) > 1 and self._effective_time(candidates[0]) == self._effective_time(candidates[1]):
                return CallResolveResult(
                    ok=False,
                    error_code="TARGET_AMBIGUOUS",
                    candidates=[self._candidate(item) for item in candidates[: self.max_candidates]],
                )
            chosen = candidates[0]
            return CallResolveResult(
                ok=True,
                call_id=str(chosen["call_id"]),
                candidates=[self._candidate(chosen)],
                resolution_method="latest_completed_same_actor_terminal",
            )
        if len(candidates) > 1:
            return CallResolveResult(
                ok=False,
                error_code="TARGET_AMBIGUOUS",
                candidates=[self._candidate(item) for item in candidates[: self.max_candidates]],
            )
        chosen = candidates[0]
        return CallResolveResult(
            ok=True,
            call_id=str(chosen["call_id"]),
            candidates=[self._candidate(chosen)],
            resolution_method="selector_unique_match",
        )

    @staticmethod
    def _authorized(session: dict[str, Any], terminal: str) -> bool:
        fields = (
            session.get("observed_source_id"),
            session.get("observed_target_id"),
            session.get("resolved_source_id"),
            session.get("resolved_target_id"),
        )
        return terminal in {str(value) for value in fields if value is not None}

    def _user_voice_authorized(self, actor_terminal_id: str, call_id: str) -> bool:
        """用户间通话参与方回退授权；未注入或异常时按无归档证据处理。"""
        if self.user_voice_access is None:
            return False
        try:
            return self.user_voice_access(actor_terminal_id, call_id) is True
        except Exception:
            return False

    @staticmethod
    def _effective_time(session: dict[str, Any]) -> str:
        return str(
            session.get("ended_at")
            or session.get("last_audio_at")
            or session.get("first_audio_at")
            or session.get("requested_at")
            or session.get("created_at")
            or ""
        )

    @classmethod
    def _candidate(cls, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "call_id": session.get("call_id"),
            "status": session.get("status"),
            "call_type": session.get("call_type"),
            "source_id": session.get("resolved_source_id") or session.get("observed_source_id"),
            "target_id": session.get("resolved_target_id") or session.get("observed_target_id"),
            "group_id": session.get("group_id"),
            "started_at": session.get("requested_at") or session.get("first_audio_at"),
            "ended_at": session.get("ended_at"),
            "effective_time": cls._effective_time(session),
        }
