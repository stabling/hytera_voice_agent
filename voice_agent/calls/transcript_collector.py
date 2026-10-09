# =============================================================================
# 【维护注释】文件：voice_agent/calls/transcript_collector.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.event_models, .lifecycle_tracker, .models, .repository
# 主要公开符号：normalize_transcript, TranscriptCollector
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Idempotent ASR-to-CallTurn dual-write collector."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from shared_contracts.event_models import DomainEvent

from .lifecycle_tracker import CallLifecycleTracker, _parse
from .models import CallTurn
from .repository import CallStoreRepository
from .utils import text_or_none as _text


def normalize_transcript(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _turn_id(event_id: str) -> str:
    return "turn_" + hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()[:32]


class TranscriptCollector:
    def __init__(
        self,
        repository: CallStoreRepository,
        tracker: CallLifecycleTracker,
    ) -> None:
        self.repository = repository
        self.tracker = tracker

    def collect(
        self,
        event: DomainEvent | dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> tuple[bool, CallTurn]:
        domain_event = event if isinstance(event, DomainEvent) else DomainEvent.model_validate(event)
        resolved_call_id = call_id or domain_event.call_id or self.tracker.project(domain_event)
        session = self.repository.get_session(resolved_call_id)
        if session is None:  # pragma: no cover - tracker guarantees this
            raise RuntimeError(f"call session unavailable: {resolved_call_id}")
        data = domain_event.data or {}
        transcript = str(data.get("transcript") or data.get("text") or "")
        normalized = str(data.get("normalized_text") or normalize_transcript(transcript))
        ended = _parse(session.ended_at)
        observed = _parse(domain_event.occurred_at)
        late_event = bool(session.status in {"ended", "failed", "rejected", "timeout"} and ended and observed and observed > ended)
        turn = CallTurn(
            turn_id=_turn_id(domain_event.event_id),
            call_id=resolved_call_id,
            event_id=domain_event.event_id,
            request_id=_text(data.get("request_id")),
            speaker_radio_id=_text(data.get("speaker_radio_id") or data.get("source_id") or domain_event.actor_id),
            started_at=_text(data.get("started_at")) or domain_event.occurred_at,
            ended_at=_text(data.get("ended_at")) or domain_event.occurred_at,
            transcript=transcript,
            normalized_text=normalized,
            asr_ok=bool(data.get("ok", domain_event.event_type == "asr.completed")),
            asr_model=_text(data.get("model") or data.get("asr_model")),
            asr_confidence=self._confidence(data.get("confidence") or data.get("asr_confidence")),
            audio_path=_text(data.get("audio_path")),
            audio_sha256=_text(data.get("audio_sha256")),
            late_event=late_event,
            message_category=_text(data.get("message_category")),
        )
        inserted, stored = self.repository.add_turn(turn)
        capture_id = _text(data.get("capture_session_id") or data.get("session_id"))
        if capture_id:
            self.repository.link_capture(
                call_id=resolved_call_id,
                capture_session_id=capture_id,
                audio_path=turn.audio_path,
                audio_sha256=turn.audio_sha256,
                started_at=turn.started_at,
                ended_at=turn.ended_at,
                confidence=1.0 if domain_event.call_id else 0.9,
                evidence_event_id=domain_event.event_id,
            )
        return inserted, stored

    @staticmethod
    def _confidence(value: object) -> float | None:
        if value is None:
            return None
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return None
