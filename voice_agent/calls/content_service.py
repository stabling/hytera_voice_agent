# =============================================================================
# 【维护注释】文件：voice_agent/calls/content_service.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：shared_contracts.models, .derived_repository, .models, .repository, .resolver, .summary_generator
# 主要公开符号：CallContentService, ResultDeliveryService
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Atomic, call-id-scoped transcript, summary and translation operations."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from shared_contracts.models import utc_now_iso
from shared_runtime.logging_config import get_logger
from shared_runtime.timing import DEFAULT_TIMING_PLUGIN, timed_call

from .derived_repository import DerivedContentRepository
from .models import AtomicContentResult, CallSelector
from .repository import CallStoreRepository
from .resolver import CallResolver
from .summary_generator import generate_call_summary, generate_call_translation

_TERMINAL_STATUSES = {"ended", "failed", "rejected", "timeout"}
logger = get_logger(__name__)


@dataclass
class CallContentService:
    repository: CallStoreRepository
    derived_repository: DerivedContentRepository
    resolver: CallResolver
    timing_plugin: Any | None = None

    def _timed(self, event: str, func: Callable[..., Any], *args: Any, fields: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return timed_call(
            logger,
            event,
            func,
            *args,
            timing_plugin=self.timing_plugin or DEFAULT_TIMING_PLUGIN,
            fields=fields,
            **kwargs,
        )

    def call_query(
        self, selector: dict[str, Any], *, actor_terminal_id: str | None
    ) -> dict[str, Any]:
        result = self._timed(
            "通话内容数据库耗时.call_query_resolve",
            self.resolver.resolve,
            selector,
            actor_terminal_id=actor_terminal_id,
            fields={"actor_terminal_id": actor_terminal_id},
        )
        return result.model_dump(mode="json")

    def get_transcript(
        self, *, call_id: str, actor_terminal_id: str | None
    ) -> dict[str, Any]:
        resolved = self._timed(
            "通话内容数据库耗时.resolve_call",
            self.resolver.resolve,
            CallSelector(call_id=call_id, relative_type="explicit"),
            actor_terminal_id=actor_terminal_id,
            fields={"call_id": call_id, "actor_terminal_id": actor_terminal_id},
        )
        if not resolved.ok or not resolved.call_id:
            return {"schema_version": 1, "ok": False, **resolved.model_dump(mode="json")}
        turns = self._timed("通话内容数据库耗时.load_turns", self.repository.turns, resolved.call_id, fields={"call_id": resolved.call_id})
        user_turns = self._user_voice_turns(turns)
        return {
            "schema_version": 1,
            "ok": True,
            "call_id": resolved.call_id,
            "turns": user_turns,
            "turn_count": len(user_turns),
            "empty": not any(str(item.get("transcript") or "").strip() for item in user_turns),
        }

    @staticmethod
    def _user_voice_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """只保留用户间通话内容；与中转台的交互不进入转写与摘要。

        控制指令语音（message_category 以 control_ 开头，对智能体的
        命令/查询）与 direct-text 文本命令是与中转台的交互而非用户间
        通话内容，与 HistoryQueryService.summarizable / CallStore
        summarizable_call_ids 的排除口径保持一致（2026-09-29 真机复现：
        "上一通电话"的转写混入了查询语句自身与"打电话给王志强"指令）。
        混合会话中的用户间内容保留，仅剔除交互 turn。
        """
        return [
            item
            for item in turns
            if not str(item.get("message_category") or "").strip().lower().startswith("control_")
            and str(item.get("asr_model") or "").strip().lower() != "direct-text"
        ]

    def summarize(
        self,
        *,
        call_id: str,
        actor_terminal_id: str | None,
        operation_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._timed(
            "通话概要总耗时",
            self._derive,
            operation_type="call.summarize",
            call_id=call_id,
            actor_terminal_id=actor_terminal_id,
            operation_id=operation_id,
            options=options or {},
            fields={"call_id": call_id, "operation_id": operation_id},
        )

    def translate(
        self,
        *,
        call_id: str,
        actor_terminal_id: str | None,
        target_language: str,
        operation_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(options or {})
        merged["target_language"] = str(target_language or "").strip().lower()
        return self._timed(
            "通话翻译总耗时",
            self._derive,
            operation_type="call.translate",
            call_id=call_id,
            actor_terminal_id=actor_terminal_id,
            operation_id=operation_id,
            options=merged,
            fields={"call_id": call_id, "operation_id": operation_id},
        )

    def wait_until_complete(self, call_id: str, *, timeout_seconds: float, poll_seconds: float = 0.25) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() <= deadline:
            session = self._timed(
                "通话内容数据库耗时.wait_load_session",
                self.repository.get_session,
                call_id,
                fields={"call_id": call_id},
            )
            if session is None:
                return False
            if session.status in _TERMINAL_STATUSES:
                return True
            time.sleep(max(0.05, float(poll_seconds)))
        return False

    def _derive(
        self,
        *,
        operation_type: str,
        call_id: str,
        actor_terminal_id: str | None,
        operation_id: str | None,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        op_id = str(operation_id or "op_" + uuid.uuid4().hex)
        cached = self._timed("通话内容数据库耗时.derived_cache_lookup", self.derived_repository.get_by_operation, op_id, fields={"operation_id": op_id, "operation_type": operation_type, "call_id": call_id})
        if cached is not None:
            return self._public(cached, cached=True)

        transcript_result = self.get_transcript(
            call_id=call_id, actor_terminal_id=actor_terminal_id
        )
        if not transcript_result.get("ok"):
            return {
                "schema_version": 1,
                "operation_type": operation_type,
                "operation_id": op_id,
                "call_id": call_id,
                "ok": False,
                "content": "",
                "source_turn_ids": [],
                "model": None,
                "prompt_hash": "0" * 64,
                "generated_at": utc_now_iso(),
                "cached": False,
                "error": transcript_result.get("error_code") or "CALL_NOT_FOUND",
            }
        turns = list(transcript_result.get("turns") or [])
        source_turn_ids = [str(item.get("turn_id")) for item in turns if item.get("turn_id")]
        session = self._timed(
            "通话内容数据库耗时.load_session",
            self.repository.get_session,
            call_id,
            fields={"call_id": call_id, "operation_id": op_id},
        )
        context = {} if session is None else session.model_dump(mode="json")
        prompt_hash = hashlib.sha256(
            json.dumps(
                {
                    "operation_type": operation_type,
                    "call_id": call_id,
                    "options": options,
                    "turns": [
                        {
                            "turn_id": item.get("turn_id"),
                            "speaker_radio_id": item.get("speaker_radio_id"),
                            "transcript": item.get("transcript"),
                            "started_at": item.get("started_at"),
                            "ended_at": item.get("ended_at"),
                        }
                        for item in turns
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if operation_type == "call.summarize":
            result = self._timed(
                "通话概要生成耗时",
                generate_call_summary,
                turns,
                call_context=context,
                max_chars=options.get("max_chars"),
                fields={"call_id": call_id, "operation_id": op_id, "turn_count": len(turns)},
            )
            content = str(result.get("summary") or "")
        else:
            result = self._timed(
                "通话翻译生成耗时",
                generate_call_translation,
                turns,
                target_language=str(options.get("target_language") or ""),
                call_context=context,
                max_chars=options.get("max_chars"),
                fields={"call_id": call_id, "operation_id": op_id, "turn_count": len(turns)},
            )
            content = str(result.get("translation") or "")
        generated_at = utc_now_iso()
        _, stored = self._timed(
            "通话内容数据库耗时.save_derived_content",
            self.derived_repository.save,
            operation_id=op_id,
            operation_type=operation_type,
            call_id=call_id,
            ok=bool(result.get("ok")),
            content=content,
            source_turn_ids=source_turn_ids,
            model=result.get("model"),
            prompt_hash=prompt_hash,
            options=options,
            generated_at=generated_at,
            error=result.get("error"),
            fields={"call_id": call_id, "operation_id": op_id, "operation_type": operation_type},
        )
        return self._public(stored, cached=False)

    @staticmethod
    def _public(stored: dict[str, Any], *, cached: bool) -> dict[str, Any]:
        result = AtomicContentResult(
            operation_type=stored["operation_type"],
            operation_id=stored["operation_id"],
            call_id=stored["call_id"],
            ok=bool(stored["ok"]),
            content=str(stored.get("content") or ""),
            source_turn_ids=list(stored.get("source_turn_ids") or []),
            model=stored.get("model"),
            prompt_hash=stored["prompt_hash"],
            generated_at=stored["generated_at"],
            cached=cached,
            error=stored.get("error"),
        ).model_dump(mode="json")
        result["derived_id"] = stored["derived_id"]
        result["content_hash"] = stored["content_hash"]
        return result


@dataclass
class ResultDeliveryService:
    derived_repository: DerivedContentRepository
    sender: Callable[..., dict[str, Any]]
    build_mcp_result: Callable[..., dict[str, Any]]
    next_request_id: Callable[[], int]

    def deliver(
        self,
        *,
        derived_id: str,
        operation_id: str,
        target_id: str,
        target_type: str = "individual",
        prefix: str = "",
    ) -> dict[str, Any]:
        previous = self.derived_repository.get_delivery(
            operation_id=operation_id, target_id=str(target_id), target_type=target_type
        )
        if previous is not None and previous.get("status") == "delivered":
            return {"ok": True, "duplicate": True, "delivery": previous}
        derived = self.derived_repository.get(derived_id)
        if derived is None:
            return {"ok": False, "duplicate": False, "error": "DERIVED_CONTENT_NOT_FOUND"}
        request_id = self.next_request_id()
        mcp_result = self.build_mcp_result(
            "sms_send",
            {
                "target_id": str(target_id),
                "target_type": target_type,
                "content": prefix + str(derived.get("content") or ""),
            },
            request_id=request_id,
        )
        response = self.sender(
            mcp_result,
            target_id=int(target_id),
            channel_id=None,
            encode=False,
        )
        stored = self.derived_repository.record_delivery(
            operation_id=operation_id,
            derived_id=derived_id,
            target_id=str(target_id),
            target_type=target_type,
            ok=bool(response.get("ok")),
            error=None if response.get("ok") else str(response.get("error") or "delivery failed"),
        )
        return {
            "ok": bool(response.get("ok")),
            "duplicate": False,
            "delivery": stored,
            "response": response,
        }
