# =============================================================================
# 【维护注释】文件：voice_agent/calls/correlator.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.event_models, .models, .repository
# 主要公开符号：event_observations, CallCorrelationEngine
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Deterministic-first call correlation with explicit heuristic evidence."""

from __future__ import annotations

from datetime import datetime, timezone

from shared_contracts.event_models import DomainEvent

from .models import CallSession, CorrelationDecision
from .repository import CallStoreRepository
from .utils import text_or_none as _text

_TERMINAL_STATUSES = {"ended", "failed", "rejected", "timeout"}


def _parse_time(value: object) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def event_observations(event: DomainEvent) -> dict[str, str | None]:
    data = event.data or {}
    call_type = _text(data.get("call_type"))
    target = _text(
        data.get("observed_target_id")
        or data.get("target_id")
        or data.get("callee_id")
    )
    group = _text(data.get("group_id"))
    if call_type == "group" and not group:
        group = target
        target = None
    return {
        "request_id": _text(data.get("request_id") or data.get("vendor_request_id")),
        "source_id": _text(data.get("observed_source_id") or data.get("source_id") or event.actor_id),
        "target_id": target,
        "group_id": group,
        "channel_id": _text(data.get("channel_id") or data.get("initial_channel_id")),
        "capture_session_id": _text(data.get("capture_session_id") or data.get("session_id") or event.subject),
        "call_type": call_type,
    }


class CallCorrelationEngine:
    def __init__(
        self,
        repository: CallStoreRepository,
        *,
        window_seconds: float = 120.0,
        min_score: float = 0.75,
        request_match_window_seconds: float = 3600.0,
    ) -> None:
        self.repository = repository
        # 对讲频道是半双工连续对话：相邻发言间隔常见 30~60s（含静音确认与
        # 切段间隔），窗口过窄会把同一频道的连续语音判为 outside_window。
        self.window_seconds = max(1.0, float(window_seconds))
        self.min_score = max(0.0, min(1.0, float(min_score)))
        # request_id 硬匹配的宽时间窗：外呼 request→ringing/answered 可能
        # 延迟数十秒，但绝不可能跨天。用于拒绝回绕复用同编号的僵尸会话。
        self.request_match_window_seconds = max(
            self.window_seconds, float(request_match_window_seconds)
        )

    def correlate(self, event: DomainEvent) -> CorrelationDecision:
        if event.call_id:
            existing = self.repository.get_session(event.call_id)
            return CorrelationDecision(
                call_id=event.call_id,
                method="event_call_id" if existing else "event_call_id_new",
                confidence=1.0,
                evidence=[f"event.call_id={event.call_id}"],
                create_new=existing is None,
            )

        if event.causation_id:
            caused_call_id = self.repository.find_by_event_id(event.causation_id)
            if caused_call_id:
                return CorrelationDecision(
                    call_id=caused_call_id,
                    method="causation_event",
                    confidence=1.0,
                    evidence=[f"causation_id={event.causation_id}"],
                )

        observations = event_observations(event)
        request_id = observations["request_id"]
        candidates = self.repository.active_sessions(limit=200)
        event_time = _parse_time(event.occurred_at)

        if request_id:
            # 无线电层 request_id 会回绕复用：历史遗留的 active 僵尸会话
            # 持有相同编号时，硬匹配会把新音频段错误并入几十天前的会话
            # （2026-09-29 真机复现：request_id=14 撞上 8 月 25 日会话）。
            # 仅端点一致且时间窗内的候选允许硬匹配。
            matches = [
                item
                for item in candidates
                if item.request_id == request_id
                and self._request_id_candidate_valid(item, observations, event_time)
            ]
            if len(matches) == 1:
                return CorrelationDecision(
                    call_id=matches[0].call_id,
                    method="request_id",
                    confidence=1.0,
                    evidence=[f"request_id={request_id}"],
                )

        capture_id = observations["capture_session_id"]
        if capture_id:
            with self.repository._connect() as connection:  # narrow internal query, same DB boundary
                rows = connection.execute(
                    """
                    SELECT DISTINCT c.call_id
                    FROM call_capture_link c
                    JOIN call_session s ON s.call_id=c.call_id
                    WHERE c.capture_session_id=?
                      AND s.status NOT IN ('ended','failed','rejected','timeout')
                    """,
                    (capture_id,),
                ).fetchall()
            # 抓包会话可能跨天存活（复用同一 capture 目录），僵尸会话的
            # 历史 capture 链接不能吞掉新音频；只保留时间窗内的候选。
            compatible = [
                row
                for row in rows
                if self._candidate_time_compatible(str(row["call_id"]), event_time)
            ]
            if len(compatible) == 1:
                return CorrelationDecision(
                    call_id=str(compatible[0]["call_id"]),
                    method="capture_session_evidence",
                    confidence=0.92,
                    evidence=[f"capture_session_id={capture_id}"],
                )

        scored: list[tuple[float, CallSession, list[str]]] = []
        for candidate in candidates:
            score, evidence = self._score(candidate, observations, event_time)
            if score > 0:
                scored.append((score, candidate, evidence))
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        if scored:
            best_score, best, evidence = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0.0
            # Ambiguous high-scoring candidates are kept separate rather than guessed.
            if best_score >= self.min_score and best_score - second_score >= 0.10:
                return CorrelationDecision(
                    call_id=best.call_id,
                    method="time_channel_target_heuristic",
                    confidence=round(best_score, 4),
                    evidence=evidence,
                )

        # A call-end with no identity can close a single active call, but remains
        # explicitly heuristic and never applies when multiple calls are active.
        if event.event_type in {"call.ended", "call.failed", "call.rejected", "call.timeout"}:
            if len(candidates) == 1:
                return CorrelationDecision(
                    call_id=candidates[0].call_id,
                    method="single_active_call",
                    confidence=max(self.min_score, 0.78),
                    evidence=["exactly_one_active_call"],
                )

        return CorrelationDecision(
            call_id=None,
            method="unmatched_new_call",
            confidence=0.5 if scored else 0.6,
            evidence=["no_unambiguous_existing_call"],
            create_new=True,
        )

    def _request_id_candidate_valid(
        self,
        candidate: CallSession,
        observations: dict[str, str | None],
        event_time: float | None,
    ) -> bool:
        """request_id 硬匹配的前置校验：端点一致 + 时间窗内。

        端点只在两边都有值时才比对，避免外呼会话（observed_source_id 为空）
        被误拒；时间窗取 last_audio/first_audio 等最近活动时间。
        """
        incoming_source = observations["source_id"]
        if (
            incoming_source
            and candidate.observed_source_id
            and incoming_source != candidate.observed_source_id
        ):
            return False
        incoming_target = observations["target_id"]
        if (
            incoming_target
            and candidate.observed_target_id
            and incoming_target != candidate.observed_target_id
        ):
            return False
        return self._candidate_time_compatible_by_session(candidate, event_time)

    def _candidate_time_compatible(self, call_id: str, event_time: float | None) -> bool:
        """capture 证据路径的时间兼容检查（按 call_id 加载会话）。"""
        if event_time is None:
            return True
        session = self.repository.get_session(call_id)
        if session is None:
            return False
        return self._candidate_time_compatible_by_session(session, event_time)

    def _candidate_time_compatible_by_session(
        self, candidate: CallSession, event_time: float | None
    ) -> bool:
        if event_time is None:
            return True
        candidate_time = _parse_time(
            candidate.last_audio_at
            or candidate.first_audio_at
            or candidate.answered_at
            or candidate.ringing_at
            or candidate.requested_at
            or candidate.created_at
        )
        if candidate_time is None:
            return True
        return abs(event_time - candidate_time) <= self.request_match_window_seconds

    def _score(
        self,
        candidate: CallSession,
        observations: dict[str, str | None],
        event_time: float | None,
    ) -> tuple[float, list[str]]:
        evidence: list[str] = []
        score = 0.0

        incoming_target = observations["group_id"] or observations["target_id"]
        candidate_target = candidate.group_id or candidate.observed_target_id
        if incoming_target and candidate_target:
            if incoming_target != candidate_target:
                return 0.0, ["target_mismatch"]
            score += 0.45
            evidence.append(f"target={incoming_target}")

        incoming_source = observations["source_id"]
        if incoming_source and candidate.observed_source_id:
            if incoming_source == candidate.observed_source_id:
                score += 0.20
                evidence.append(f"source={incoming_source}")
            elif candidate.direction == "inbound":
                return 0.0, ["source_mismatch"]

        channel = observations["channel_id"]
        if channel and candidate.initial_channel_id:
            if channel == candidate.initial_channel_id:
                score += 0.20
                evidence.append(f"channel={channel}")
            elif incoming_target is None:
                score -= 0.10

        incoming_type = observations["call_type"]
        if incoming_type and incoming_type != "unknown" and candidate.call_type != "unknown":
            if incoming_type != candidate.call_type:
                return 0.0, ["call_type_mismatch"]
            score += 0.05
            evidence.append(f"call_type={incoming_type}")

        candidate_time = _parse_time(
            candidate.last_audio_at
            or candidate.first_audio_at
            or candidate.answered_at
            or candidate.ringing_at
            or candidate.requested_at
            or candidate.created_at
        )
        if event_time is not None and candidate_time is not None:
            delta = abs(event_time - candidate_time)
            if delta > self.window_seconds:
                return 0.0, [f"outside_window={delta:.3f}s"]
            time_score = 0.20 * (1.0 - delta / self.window_seconds)
            score += max(0.02, time_score)
            evidence.append(f"time_delta_seconds={delta:.3f}")

        return max(0.0, min(1.0, score)), evidence
