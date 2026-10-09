# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/transport.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：shared_contracts.http_client, shared_contracts.security, shared_contracts.trace_context, shared_runtime.observability, ..capture.io_utils, .models
# 主要公开符号：CallbackTransportMixin
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from shared_contracts.http_client import SegmentedHttpError, post_bytes_segmented
from shared_contracts.security import outbound_headers
from shared_contracts.trace_context import extract_trace_context, inject_trace_headers
from shared_runtime.observability import trace_headers

from ..capture.io_utils import now_iso
from .models import CallbackSendResult

logger = logging.getLogger("hytera_adk_gateway.callbacks")


class CallbackTransportMixin:
    """Voice callback HTTP transport with phase-aware failure classification."""

    def _post_json_once(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        attempt: int = 1,
    ) -> CallbackSendResult:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        correlation_id = self._callback_correlation_id(payload)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "X-Correlation-ID": correlation_id,
            "X-Hytera-Event-ID": str(payload.get("event_id") or correlation_id)[:128],
            **outbound_headers(),
        }
        trace_context = extract_trace_context(payload=payload)
        inject_trace_headers(headers, trace_context)
        headers = trace_headers(headers)
        try:
            response = post_bytes_segmented(url, data, headers=headers, timeout=timeout)
            status = response.status
            timings = response.timings
            logger.info(
                "voice_callback_http correlation_id=%s event_id=%s payload_bytes=%d attempt=%d "
                "connect_ms=%.3f write_ms=%.3f read_ms=%.3f total_ms=%.3f "
                "status_code=%d exception_type=none",
                correlation_id,
                str(payload.get("event_id") or "")[:128],
                len(data),
                attempt,
                timings.connect_ms,
                timings.write_ms,
                timings.read_ms,
                timings.total_ms,
                status,
            )
            acknowledgement = self._parse_callback_ack(response.body)
            if not 200 <= status < 300:
                category = (
                    "http_429" if status == 429 else "http_5xx" if status >= 500 else "http_4xx_rejected"
                )
                return CallbackSendResult(
                    ok=False,
                    status_code=status,
                    category=category,
                    message=f"callback returned HTTP {status}",
                    acknowledgement=acknowledgement,
                    payload_bytes=len(data),
                    total_ms=timings.total_ms,
                    connect_ms=timings.connect_ms, write_ms=timings.write_ms, read_ms=timings.read_ms,
                )
            if not acknowledgement:
                return CallbackSendResult(
                    ok=False,
                    status_code=status,
                    category="ack_parse_error",
                    message="callback returned an empty or invalid acknowledgement",
                    acknowledgement={},
                    payload_bytes=len(data),
                    total_ms=timings.total_ms,
                    connect_ms=timings.connect_ms, write_ms=timings.write_ms, read_ms=timings.read_ms,
                )
            accepted = acknowledgement.get("ok") is not False and (
                acknowledgement.get("queued") is not False
                or acknowledgement.get("duplicate") is True
            )
            if not accepted:
                return CallbackSendResult(
                    ok=False,
                    status_code=status,
                    category="tool_rejected",
                    message="callback rejected payload: "
                    + str(
                        acknowledgement.get("dropped_reason")
                        or acknowledgement.get("error")
                        or "negative acknowledgement"
                    ),
                    acknowledgement=acknowledgement,
                    payload_bytes=len(data),
                    total_ms=timings.total_ms,
                    connect_ms=timings.connect_ms, write_ms=timings.write_ms, read_ms=timings.read_ms,
                )
            return CallbackSendResult(
                ok=True,
                status_code=status,
                category="acked",
                message="accepted",
                acknowledgement=acknowledgement,
                payload_bytes=len(data),
                total_ms=timings.total_ms,
                connect_ms=timings.connect_ms, write_ms=timings.write_ms, read_ms=timings.read_ms,
            )
        except SegmentedHttpError as exc:
            timings = exc.timings
            phase_category = {
                "connect": "connect_timeout" if isinstance(exc.original, TimeoutError) else "transport_error",
                "write": "write_timeout" if isinstance(exc.original, TimeoutError) else "transport_error",
                "read": "read_timeout" if isinstance(exc.original, TimeoutError) else "transport_error",
            }.get(exc.phase, "transport_error")
            # socket.timeout is not always a direct TimeoutError subclass on old Windows builds.
            if "timeout" in type(exc.original).__name__.lower():
                phase_category = f"{exc.phase}_timeout"
            message = f"{type(exc.original).__name__} during {exc.phase}: {exc.original}"
            logger.warning(
                "voice_callback_http correlation_id=%s event_id=%s payload_bytes=%d attempt=%d "
                "connect_ms=%.3f write_ms=%.3f read_ms=%.3f total_ms=%.3f "
                "failed_phase=%s exception_type=%s",
                correlation_id,
                str(payload.get("event_id") or "")[:128],
                len(data),
                attempt,
                timings.connect_ms,
                timings.write_ms,
                timings.read_ms,
                timings.total_ms,
                exc.phase,
                type(exc.original).__name__,
            )
            return CallbackSendResult(
                ok=False,
                status_code=None,
                category=phase_category,
                message=message,
                acknowledgement={},
                payload_bytes=len(data),
                total_ms=timings.total_ms,
                connect_ms=timings.connect_ms, write_ms=timings.write_ms, read_ms=timings.read_ms,
            )
        except Exception as exc:
            logger.warning(
                "voice_callback_http correlation_id=%s event_id=%s payload_bytes=%d attempt=%d "
                "failed_phase=prepare exception_type=%s",
                correlation_id,
                str(payload.get("event_id") or "")[:128],
                len(data),
                attempt,
                type(exc).__name__,
            )
            return CallbackSendResult(
                ok=False,
                status_code=None,
                category="unexpected_error",
                message=f"{type(exc).__name__}: {exc}",
                acknowledgement={},
                payload_bytes=len(data),
                total_ms=0.0,
            )

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        max_retries: int = 3,
    ) -> bool:
        """Compatibility wrapper used by the legacy delivery path."""

        last_result: CallbackSendResult | None = None
        for attempt in range(1, max(1, int(max_retries)) + 1):
            last_result = self._post_json_once(url, payload, timeout=timeout, attempt=attempt)
            if last_result.ok:
                with self._lock:
                    self._total_sent += 1
                    self._last_success_at = now_iso()
                    self._last_status_code = last_result.status_code
                    self._last_error = None
                    self._last_payload_preview = self._preview_payload(payload)
                return True
            retryable = last_result.category in {
                "connect_timeout",
                "write_timeout",
                "read_timeout",
                "transport_error",
                "http_429",
                "http_5xx",
                "ack_parse_error",
            }
            if not retryable or attempt >= max_retries:
                break
            time.sleep(min(float(2 ** (attempt - 1)), 6.0))
        assert last_result is not None
        self._remember_error(
            last_result.message,
            status_code=last_result.status_code,
            url=url,
        )
        return False

    @staticmethod
    def _callback_correlation_id(payload: dict[str, Any]) -> str:
        for key in ("correlation_id", "event_id", "request_id", "session_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value[:128]
        return uuid.uuid4().hex

    @staticmethod
    def _parse_callback_ack(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _remember_error(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str = "",
    ) -> None:
        with self._lock:
            self._total_failed += 1
            self._last_error_at = now_iso()
            self._last_error = message
            self._last_status_code = status_code
        logger.warning("语音回调发送失败 [%s]: %s", url or "unknown", message)
