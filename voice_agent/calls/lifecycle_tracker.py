# =============================================================================
# 【维护注释】文件：voice_agent/calls/lifecycle_tracker.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.event_models, shared_contracts.identifiers, shared_contracts.models, .correlator, .models, .repository
# 主要公开符号：ContactIdentityResolver, CallLifecycleTracker
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Projection of immutable domain events into mutable CallStore read models."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

from shared_contracts.event_models import DomainEvent
from shared_contracts.identifiers import new_call_id
from shared_contracts.models import utc_now_iso

from .correlator import CallCorrelationEngine, event_observations
from .models import CallParticipant, CallSession
from .repository import CallStoreRepository
from .utils import text_or_none as _text

_TERMINAL = {"ended", "failed", "rejected", "timeout"}
_STATUS_RANK = {
    "observed": 0,
    "requested": 1,
    "ringing": 2,
    "answered": 3,
    "active": 4,
    "ended": 5,
    "failed": 5,
    "rejected": 5,
    "timeout": 5,
}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _earliest(current: str | None, incoming: str | None) -> str | None:
    if not current:
        return incoming
    if not incoming:
        return current
    left = _parse(current)
    right = _parse(incoming)
    if left is None or right is None:
        return min(current, incoming)
    return current if left <= right else incoming


def _latest(current: str | None, incoming: str | None) -> str | None:
    if not current:
        return incoming
    if not incoming:
        return current
    left = _parse(current)
    right = _parse(incoming)
    if left is None or right is None:
        return max(current, incoming)
    return current if left >= right else incoming


def _participant_id(call_id: str, role: str, observed_id: str | None) -> str:
    digest = hashlib.sha256(f"{call_id}|{role}|{observed_id or ''}".encode("utf-8")).hexdigest()[:24]
    return f"part_{digest}"


class ContactIdentityResolver:
    """Read-only reverse lookup over the existing contact directory."""

    def __call__(self, observed_id: str | None, participant_type: str) -> dict[str, Any]:
        if not observed_id:
            return {
                "resolved_id": None,
                "display_name": None,
                "resolution_method": "missing_observed_id",
                "confidence": 0.0,
            }
        try:
            from voice_agent.contact_directory import get_contact_map

            mapping = get_contact_map()
        except Exception:
            mapping = {}
        kinds = ("group",) if participant_type == "group" else ("private", "standby")
        matches: list[tuple[str, str]] = []
        for kind in kinds:
            for name, value in (mapping.get(kind) or {}).items():
                if str(value) == observed_id:
                    matches.append((str(name), str(value)))
        if len(matches) == 1:
            return {
                "resolved_id": matches[0][1],
                "display_name": matches[0][0],
                "resolution_method": "contact_directory_exact_id",
                "confidence": 1.0,
            }
        return {
            "resolved_id": None,
            "display_name": None,
            "resolution_method": "unresolved_observed_id",
            "confidence": 0.0,
        }


class CallLifecycleTracker:
    def __init__(
        self,
        repository: CallStoreRepository,
        correlator: CallCorrelationEngine,
        *,
        identity_resolver: Callable[[str | None, str], dict[str, Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.correlator = correlator
        self.identity_resolver = identity_resolver or ContactIdentityResolver()

    def project(self, event: DomainEvent | dict[str, Any]) -> str:
        domain_event = event if isinstance(event, DomainEvent) else DomainEvent.model_validate(event)
        existing_link = self.repository.find_by_event_id(domain_event.event_id)
        if existing_link:
            return existing_link

        decision = self.correlator.correlate(domain_event)
        call_id = decision.call_id or new_call_id()
        existing = self.repository.get_session(call_id)
        observations = event_observations(domain_event)
        target_status = self._status_for(domain_event)
        occurred_at = domain_event.occurred_at
        late_event = self._is_late(existing, target_status, occurred_at)

        if existing is None:
            session = CallSession(
                call_id=call_id,
                direction=self._direction_for(domain_event),
                call_type=self._call_type_for(domain_event, observations),
                status=target_status,
                observed_source_id=observations["source_id"],
                observed_target_id=observations["target_id"],
                group_id=observations["group_id"],
                initial_channel_id=observations["channel_id"],
                request_id=observations["request_id"],
                correlation_method=decision.method,
                correlation_confidence=decision.confidence,
                provenance=domain_event.provenance,
                created_at=domain_event.recorded_at or utc_now_iso(),
                updated_at=domain_event.recorded_at or utc_now_iso(),
            )
        else:
            session = existing.model_copy(deep=True)
            session.revision += 1
            session.updated_at = domain_event.recorded_at or utc_now_iso()
            session.observed_source_id = session.observed_source_id or observations["source_id"]
            session.observed_target_id = session.observed_target_id or observations["target_id"]
            session.group_id = session.group_id or observations["group_id"]
            session.initial_channel_id = session.initial_channel_id or observations["channel_id"]
            session.request_id = session.request_id or observations["request_id"]
            if session.call_type == "unknown":
                session.call_type = self._call_type_for(domain_event, observations)
            if session.direction == "unknown":
                session.direction = self._direction_for(domain_event)
            if decision.confidence > session.correlation_confidence:
                session.correlation_confidence = decision.confidence
                session.correlation_method = decision.method
            if not late_event:
                session.status = self._advance_status(session.status, target_status)

        self._apply_times(session, domain_event, target_status, late_event)
        self._apply_resolved_ids(session, domain_event)
        self._compute_duration(session)
        session = self.repository.upsert_session(session)

        evidence = list(decision.evidence)
        if domain_event.causation_id:
            evidence.append(f"causation_id={domain_event.causation_id}")
        raw_evidence = domain_event.data.get("evidence_event_ids")
        if isinstance(raw_evidence, list):
            evidence.extend(str(item) for item in raw_evidence if item)
        self.repository.link_event(
            call_id=call_id,
            event_id=domain_event.event_id,
            event_type=domain_event.event_type,
            relation=self._relation_for(domain_event),
            occurred_at=domain_event.occurred_at,
            confidence=decision.confidence,
            evidence=evidence,
            late_event=late_event,
        )
        self._add_participants(session, domain_event)
        self._link_capture(session, domain_event, decision.confidence)
        return call_id

    @staticmethod
    def _status_for(event: DomainEvent) -> str:
        explicit = _text(event.data.get("lifecycle_state") or event.data.get("status"))
        if explicit in _STATUS_RANK:
            return explicit
        mapping = {
            "call.requested": "requested",
            "call.started": "requested",
            "call.ringing": "ringing",
            "call.answered": "answered",
            "call.audio_started": "active",
            "call.audio_activity": "active",
            "voice.received": "active",
            "voice.callback.accepted": "active",
            "asr.completed": "active",
            "asr.failed": "active",
            "call.ended": "ended",
            "call.failed": "failed",
            "call.rejected": "rejected",
            "call.timeout": "timeout",
        }
        return mapping.get(event.event_type, "observed")

    @staticmethod
    def _relation_for(event: DomainEvent) -> str:
        if event.event_type.startswith("asr."):
            return "transcript_evidence"
        if event.event_type.startswith("voice."):
            return "audio_evidence"
        if event.event_type.startswith("call."):
            return "lifecycle_evidence"
        return "supporting_evidence"

    @staticmethod
    def _direction_for(event: DomainEvent) -> str:
        explicit = _text(event.data.get("direction"))
        if explicit in {"inbound", "outbound", "unknown"}:
            return explicit
        if event.source_service == "mcp-server" and event.event_type in {"call.started", "call.requested"}:
            return "outbound"
        if event.event_type in {"voice.received", "voice.callback.accepted"}:
            return "inbound" if event.actor_id else "unknown"
        return "unknown"

    @staticmethod
    def _call_type_for(event: DomainEvent, observations: dict[str, str | None]) -> str:
        value = observations.get("call_type") or _text(event.data.get("target_type"))
        if value == "individual":
            value = "private"
        if value in {"private", "group", "standby", "broadcast"}:
            return value
        if observations.get("group_id"):
            return "group"
        return "unknown"

    @staticmethod
    def _advance_status(current: str, incoming: str) -> str:
        if current in _TERMINAL:
            return current
        if incoming in _TERMINAL:
            return incoming
        return incoming if _STATUS_RANK[incoming] >= _STATUS_RANK[current] else current

    @staticmethod
    def _is_late(existing: CallSession | None, incoming_status: str, occurred_at: str) -> bool:
        if existing is None or existing.status not in _TERMINAL:
            return False
        if incoming_status not in _TERMINAL:
            return True
        ended = _parse(existing.ended_at)
        observed = _parse(occurred_at)
        return bool(ended and observed and observed > ended)

    @staticmethod
    def _apply_times(
        session: CallSession,
        event: DomainEvent,
        status: str,
        late_event: bool,
    ) -> None:
        at = event.occurred_at
        if status == "requested":
            session.requested_at = _earliest(session.requested_at, at)
        elif status == "ringing":
            session.ringing_at = _earliest(session.ringing_at, at)
        elif status == "answered":
            session.answered_at = _earliest(session.answered_at, at)
        elif status == "active" and not late_event:
            session.first_audio_at = _earliest(session.first_audio_at, at)
            session.last_audio_at = _latest(session.last_audio_at, at)
        elif status in _TERMINAL:
            session.ended_at = _earliest(session.ended_at, at)
            session.end_reason = session.end_reason or _text(
                event.data.get("end_reason") or event.data.get("error_type") or status
            )

    @staticmethod
    def _compute_duration(session: CallSession) -> None:
        if not session.ended_at:
            session.duration_ms = None
            return
        start = session.answered_at or session.first_audio_at or session.requested_at
        start_dt = _parse(start)
        end_dt = _parse(session.ended_at)
        if start_dt is None or end_dt is None:
            session.duration_ms = None
            return
        session.duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))

    @staticmethod
    def _apply_resolved_ids(session: CallSession, event: DomainEvent) -> None:
        session.resolved_source_id = session.resolved_source_id or _text(event.data.get("resolved_source_id"))
        session.resolved_target_id = session.resolved_target_id or _text(event.data.get("resolved_target_id"))

    def _add_participants(self, session: CallSession, event: DomainEvent) -> None:
        entries = [
            ("caller", "radio", session.observed_source_id),
            ("group" if session.group_id else "callee", "group" if session.group_id else "radio", session.group_id or session.observed_target_id),
        ]
        for role, participant_type, observed_id in entries:
            if not observed_id:
                continue
            resolved = self.identity_resolver(observed_id, participant_type)
            participant = CallParticipant(
                participant_id=_participant_id(session.call_id, role, observed_id),
                call_id=session.call_id,
                observed_id=observed_id,
                resolved_id=_text(resolved.get("resolved_id")),
                display_name=_text(resolved.get("display_name")),
                role=role,
                participant_type=participant_type,
                resolution_method=str(resolved.get("resolution_method") or "unresolved"),
                confidence=float(resolved.get("confidence") or 0.0),
                evidence_event_id=event.event_id,
            )
            self.repository.add_participant(participant)

    def _link_capture(self, session: CallSession, event: DomainEvent, confidence: float) -> None:
        capture_id = _text(event.data.get("capture_session_id") or event.data.get("session_id"))
        if not capture_id:
            return
        self.repository.link_capture(
            call_id=session.call_id,
            capture_session_id=capture_id,
            audio_path=_text(event.data.get("audio_path")),
            audio_sha256=_text(event.data.get("audio_sha256")),
            started_at=event.occurred_at,
            ended_at=_text(event.data.get("capture_ended_at")),
            confidence=confidence,
            evidence_event_id=event.event_id,
        )
