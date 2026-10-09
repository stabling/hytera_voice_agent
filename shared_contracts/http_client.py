# =============================================================================
# 【维护注释】文件：shared_contracts/http_client.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：.security, .trace_context
# 主要公开符号：HttpPhaseTimings, SegmentedHttpResponse, SegmentedHttpError, get_json, post_json, post_bytes_segmented, classify_http_status
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Small dependency-free HTTP clients used across the bitness boundary."""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .security import outbound_headers
from .trace_context import TraceContext, inject_trace_headers


@dataclass(frozen=True)
class HttpPhaseTimings:
    """Measured HTTP transport phases in milliseconds."""

    connect_ms: float = 0.0
    write_ms: float = 0.0
    read_ms: float = 0.0
    total_ms: float = 0.0
    failed_phase: str | None = None

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "connect_ms": self.connect_ms,
            "write_ms": self.write_ms,
            "read_ms": self.read_ms,
            "total_ms": self.total_ms,
            "failed_phase": self.failed_phase,
        }


@dataclass(frozen=True)
class SegmentedHttpResponse:
    """HTTP response plus phase timings; response bodies are never logged here."""

    status: int
    body: bytes
    headers: dict[str, str]
    timings: HttpPhaseTimings


class SegmentedHttpError(OSError):
    """Transport failure annotated with the phase active when it occurred."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        timings: HttpPhaseTimings,
        original: BaseException,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.timings = timings
        self.original = original


def get_json(
    url: str, *, timeout: float = 2.0, trace_context: TraceContext | None = None
) -> dict[str, Any]:
    headers = {"Accept": "application/json", **outbound_headers()}
    inject_trace_headers(headers, trace_context)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    value = json.loads(body.decode("utf-8")) if body else {}
    if not isinstance(value, dict):
        raise ValueError("HTTP response must be a JSON object")
    return value


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 5.0,
    retries: int = 0,
    trace_context: TraceContext | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        **outbound_headers(),
    }
    inject_trace_headers(headers, trace_context)
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            request = urllib.request.Request(url, data=body, method="POST", headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read()
            value = json.loads(response_body.decode("utf-8")) if response_body else {}
            if not isinstance(value, dict):
                raise ValueError("HTTP response must be a JSON object")
            return value
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(min(0.25 * (2**attempt), 2.0))
    assert last_error is not None
    raise last_error


def _rounded_ms(started_at: float, ended_at: float | None = None) -> float:
    end = time.perf_counter() if ended_at is None else ended_at
    return round(max(0.0, (end - started_at) * 1000.0), 3)


_KEEPALIVE_DISABLED_VALUES = {"0", "false", "off", "no"}
_CONNECTION_POOLS = threading.local()


def _keepalive_enabled() -> bool:
    """Shared keep-alive pool switch (default on; SHARED_HTTP_KEEPALIVE=false off)."""

    value = os.environ.get("SHARED_HTTP_KEEPALIVE", "").strip().lower()
    return value not in _KEEPALIVE_DISABLED_VALUES


def _pooled_connections() -> dict[tuple, http.client.HTTPConnection]:
    pools = getattr(_CONNECTION_POOLS, "connections", None)
    if pools is None:
        pools = {}
        _CONNECTION_POOLS.connections = pools
    return pools


def _new_segmented_connection(
    parsed: Any, port: int, timeout: float
) -> http.client.HTTPConnection:
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)


def post_bytes_segmented(
    url: str,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> SegmentedHttpResponse:
    """POST bytes while measuring connect, write, read and total phases.

    Retains HTTP/1.1 and the dependency-free runtime.  By default each call
    now reuses a per-thread keep-alive connection when the server allows it
    (``Connection: keep-alive``), removing the local TCP handshake from the
    gateway->agent callback hot path.  Set ``SHARED_HTTP_KEEPALIVE=false`` to
    restore the old connect-per-request behavior.  A stale pooled connection
    (server closed it idle) is retried once on a fresh connection.
    """

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute http(s) URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query

    request_headers = dict(headers or {})
    inject_trace_headers(request_headers)
    lower_header_names = {name.lower() for name in request_headers}
    if "content-length" not in lower_header_names:
        request_headers["Content-Length"] = str(len(body))

    keep_alive = _keepalive_enabled() and "connection" not in lower_header_names
    if keep_alive:
        request_headers["Connection"] = "keep-alive"
    elif "connection" not in lower_header_names:
        request_headers["Connection"] = "close"

    pool_key = (parsed.scheme, parsed.hostname, port, float(timeout))
    pools = _pooled_connections() if keep_alive else None

    for attempt in (1, 2):
        connection: http.client.HTTPConnection | None = None
        reused = False
        if pools is not None and attempt == 1:
            connection = pools.pop(pool_key, None)
            reused = connection is not None
        if connection is None:
            connection = _new_segmented_connection(parsed, port, timeout)

        total_t0 = time.perf_counter()
        connect_ms = 0.0
        write_ms = 0.0
        read_ms = 0.0
        phase = "connect"
        phase_t0 = total_t0
        pooled_back = False
        try:
            if not reused:
                connection.connect()
                connect_ms = _rounded_ms(phase_t0)

            phase = "write"
            phase_t0 = time.perf_counter()
            connection.putrequest(
                "POST",
                target,
                skip_host=any(name.lower() == "host" for name in request_headers),
                skip_accept_encoding=True,
            )
            for name, value in request_headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            if body:
                connection.send(body)
            write_ms = _rounded_ms(phase_t0)

            phase = "read"
            phase_t0 = time.perf_counter()
            response = connection.getresponse()
            response_body = response.read()
            read_ms = _rounded_ms(phase_t0)
            timings = HttpPhaseTimings(
                connect_ms=connect_ms,
                write_ms=write_ms,
                read_ms=read_ms,
                total_ms=_rounded_ms(total_t0),
            )
            result = SegmentedHttpResponse(
                status=int(response.status),
                body=response_body,
                headers={name: value for name, value in response.getheaders()},
                timings=timings,
            )
            if pools is not None and not response.will_close:
                pools[pool_key] = connection
                pooled_back = True
            return result
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
            partial_ms = _rounded_ms(phase_t0)
            if phase == "connect":
                connect_ms = partial_ms
            elif phase == "write":
                write_ms = partial_ms
            elif phase == "read":
                read_ms = partial_ms
            # 仅当复用了池内连接且失败发生在请求发送/响应读取阶段时，
            # 可能是服务端已闲置关闭导致的 stale 连接（RemoteDisconnected/
            # BrokenPipe/ConnectionReset）：换新连接重试一次。
            stale_retry = (
                reused
                and attempt == 1
                and phase in {"write", "read"}
                and isinstance(exc, (http.client.HTTPException, ConnectionError))
                and not isinstance(exc, (TimeoutError, socket.timeout))
            )
            if not pooled_back:
                try:
                    connection.close()
                except Exception:
                    pass
            if stale_retry:
                continue
            timings = HttpPhaseTimings(
                connect_ms=connect_ms,
                write_ms=write_ms,
                read_ms=read_ms,
                total_ms=_rounded_ms(total_t0),
                failed_phase=phase,
            )
            raise SegmentedHttpError(
                f"HTTP {phase} failed: {type(exc).__name__}: {exc}",
                phase=phase,
                timings=timings,
                original=exc,
            ) from exc
        finally:
            if not pooled_back:
                try:
                    connection.close()
                except Exception:
                    pass


def classify_http_status(status_code: int) -> tuple[str, bool]:
    """Return a stable callback failure category and retryability decision."""

    status = int(status_code)
    if status == 429:
        return "http_429", True
    if 500 <= status <= 599:
        return "http_5xx", True
    if 400 <= status <= 499:
        return "http_4xx_rejected", False
    if 200 <= status <= 299:
        return "accepted", False
    return "unexpected_status", False
