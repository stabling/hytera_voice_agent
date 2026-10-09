from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DeliveryDecision:
    deliver: bool
    reason: str


class UserDeliveryPolicy(Protocol):
    def evaluate(self, state: dict[str, Any], text: str) -> DeliveryDecision: ...


class LogOnlyErrorDeliveryPolicy:
    """Suppress user-facing SMS for processing errors while preserving success text.

    This policy changes only the notification channel for errors.  It does not
    alter planning, execution, retries, operation state, or successful content
    delivery.
    """

    _ERROR_STATUSES = {"FAILED", "CANCELLED", "TIMED_OUT"}

    def evaluate(self, state: dict[str, Any], text: str) -> DeliveryDecision:
        del text
        execution = state.get("plan_execution") or {}
        status = str(execution.get("status") or "").strip().upper()
        if status in self._ERROR_STATUSES:
            return DeliveryDecision(False, f"execution_{status.lower()}")
        if execution.get("error"):
            return DeliveryDecision(False, "execution_error")
        if state.get("planner_block_error"):
            return DeliveryDecision(False, "planner_error")
        if not bool((state.get("asr_result") or {}).get("ok", True)):
            return DeliveryDecision(False, "asr_failed")
        if str(state.get("planner_gate") or "").strip().lower() == "reject":
            return DeliveryDecision(False, "planner_rejected")
        return DeliveryDecision(True, "normal_user_result")


DEFAULT_USER_DELIVERY_POLICY = LogOnlyErrorDeliveryPolicy()
