# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/http_pool.py
# 所属层次：语音智能体核心（HTTP 传输层）
# 主要职责：为 Agent 侧所有回环 HTTP 调用（MCP /mcp、网关结果短信、设备转发）
#           提供线程安全的 keep-alive 连接池，消除每次 urlopen 重建 TCP 连接的开销。
# 项目内直接依赖：无（仅标准库 + 可选 requests）
# 主要公开符号：pooled_post, get_shared_session
# 兼容性：requests 缺失或 VOICE_HTTP_KEEPALIVE=false 时自动回退 urllib.urlopen；
#         异常契约与 urlopen 一致（HTTPError / URLError），旧调用方 except 分支无需修改。
# 修改原则：传输层只做连接复用，不改业务语义；错误转换必须保留 body 可读。
# 排障顺序：先看 VOICE_HTTP_KEEPALIVE 环境开关，再看目标服务是否支持 keep-alive，
#           最后抓包确认连接是否复用（同一源端口多次请求）。
# =============================================================================
"""Thread-safe keep-alive HTTP POST transport for loopback service calls.

urllib.request.urlopen opens a fresh TCP connection per call.  On the agent
hot path (MCP tool call -> gateway result SMS -> device forward) that costs
a full local TCP handshake each time.  This module keeps one shared
``requests.Session`` (urllib3 connection pool) so repeated calls to the same
loopback endpoint reuse an established connection.

Compatibility contract (identical to ``urllib.request.urlopen``):
- HTTP >= 400 raises ``urllib.error.HTTPError`` whose ``read()`` returns the
  response body and whose ``headers`` supports ``get()``.
- Connection/timeout failures raise ``urllib.error.URLError``; timeouts use
  ``URLError(TimeoutError(...))`` so callers using ``isinstance(reason,
  TimeoutError)`` keep working.
- Set ``VOICE_HTTP_KEEPALIVE=false`` to fall back to plain ``urlopen``.
"""
from __future__ import annotations

import io
import os
import threading
import urllib.error
import urllib.request
from typing import Any

try:  # requests is in requirements-agent64.txt; keep a safe fallback anyway
    import requests
except ImportError:  # pragma: no cover - venv without requests
    requests = None  # type: ignore[assignment]

_SESSION_LOCK = threading.Lock()
_SESSION: Any = None


def _keepalive_enabled() -> bool:
    value = os.environ.get("VOICE_HTTP_KEEPALIVE", "").strip().lower()
    return value not in {"0", "false", "off", "no"}


def get_shared_session() -> Any:
    """Return the shared keep-alive session, or None when disabled/unavailable."""
    global _SESSION
    if _SESSION is not None or requests is None or not _keepalive_enabled():
        return _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=4, pool_maxsize=8, max_retries=0
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            _SESSION = session
    return _SESSION


def pooled_post(
    url: str,
    *,
    data: bytes,
    headers: dict[str, str],
    timeout: float | None,
) -> tuple[int, bytes]:
    """POST ``data`` to ``url`` reusing a keep-alive connection when possible.

    Returns ``(status_code, body_bytes)``.  Raises ``urllib.error.HTTPError``
    for HTTP >= 400 and ``urllib.error.URLError`` for transport failures,
    matching the old ``urlopen`` behavior so existing handlers still work.
    """
    session = get_shared_session()
    if session is None:
        request = urllib.request.Request(url, data=data, method="POST", headers=dict(headers))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.getcode() or 0), response.read()
    try:
        response = session.post(url, data=data, headers=dict(headers), timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise urllib.error.URLError(TimeoutError(f"http request timed out: {exc}")) from exc
    except requests.RequestException as exc:
        raise urllib.error.URLError(f"{type(exc).__name__}: {exc}") from exc
    status_code = int(response.status_code)
    content = response.content
    if status_code >= 400:
        raise urllib.error.HTTPError(
            url,
            status_code,
            str(getattr(response, "reason", "") or ""),
            response.headers,
            io.BytesIO(content),
        )
    return status_code, content
