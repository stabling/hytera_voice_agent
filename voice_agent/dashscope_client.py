# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/dashscope_client.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：., .contact_directory, .zone_channel_directory, .utils
# 主要公开符号：parse_http_error, http_post_json, is_qwen_audio3_asr_flash_model, is_qwen_audio3_asr_streaming_model, build_qwen_audio3_hotwords, build_qwen_audio3_context, build_qwen_audio3_asr_payload, build_qwen_audio3_streaming_run_task, call_qwen_audio3_asr_streaming, extract_qwen_audio3_asr_text, extract_qwen_audio3_annotations, build_qwen3_asr_payload, extract_qwen3_asr_text, build_local_openai_asr_payload, clean_local_asr_text, call_local_openai_asr
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""DashScope clients for Qwen ASR and semantic planning support.

Qwen3-ASR uses the OpenAI-compatible endpoint. Qwen-Audio-3.0-ASR-Flash
uses the native multimodal endpoint and supports inline hotwords/context.

429-safe strategy for batch ASR:
1. Smooth local rate limiting below the official model quota.
2. Add X-DashScope-Wait-Timeout for server-side burst queueing.
3. Retry transient 429/5xx/network errors with minute-window backoff.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

try:  # requests 在 requirements-agent64.txt；缺失时回退 urlopen
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None  # type: ignore[assignment]

try:  # websockets 在 requirements-agent64.txt；缺失时流式 ASR 回退 native HTTP
    from websockets.sync.client import connect as _ws_connect
except ImportError:  # pragma: no cover
    _ws_connect = None  # type: ignore[assignment]

from . import config
from .contact_directory import get_contact_map
from .zone_channel_directory import get_zone_channel_map
from .prompts import build_asr_context_prompt
from .utils import inc_stat, perf_ms

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP keep-alive 连接池：planner/ASR/播报等对 dashscope 的调用走共享 Session
# 复用 TCP+TLS 连接，消除每次 urlopen 的握手开销（Planner 热路径 ~30-80ms）。
# 契约：_HTTP_SESSION 可被测试 monkeypatch；异常仍转换为 urllib.error
# HTTPError/URLError，旧调用方 except 分支不变。回退开关：VOICE_HTTP_KEEPALIVE=false。
# ---------------------------------------------------------------------------
_HTTP_SESSION: Any = None
_HTTP_SESSION_LOCK = threading.Lock()


def _get_http_session() -> Any:
    """Return the shared keep-alive session, or None when disabled/unavailable."""
    global _HTTP_SESSION
    if _HTTP_SESSION is not None or _requests is None:
        return _HTTP_SESSION
    if os.environ.get("VOICE_HTTP_KEEPALIVE", "").strip().lower() in {"0", "false", "off", "no"}:
        return None
    with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION is None:
            session = _requests.Session()
            adapter = _requests.adapters.HTTPAdapter(
                pool_connections=4, pool_maxsize=8, max_retries=0
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _HTTP_SESSION = session
    return _HTTP_SESSION


_RATE_LOCKS: dict[str, threading.Lock] = {
    "asr": threading.Lock(),
    "planner": threading.Lock(),
    "generic": threading.Lock(),
}
_NEXT_ALLOWED_AT: dict[str, float] = {
    "asr": 0.0,
    "planner": 0.0,
    "generic": 0.0,
}


def _rate_limit_rpm(kind: str) -> float:
    if kind == "asr":
        return float(config.ASR_RATE_LIMIT_RPM or 0.0)
    if kind == "planner":
        return float(config.PLANNER_RATE_LIMIT_RPM or 0.0)
    return 0.0


def _smooth_rate_limit(kind: str) -> float:
    """Block until this process is allowed to send the next request.

    The lock is intentionally held during sleep so multiple worker threads cannot
    wake at the same instant and produce a burst. This sacrifices throughput for
    deterministic spacing.
    """
    rpm = _rate_limit_rpm(kind)
    if rpm <= 0:
        return 0.0
    interval = 60.0 / rpm
    lock = _RATE_LOCKS.setdefault(kind, threading.Lock())
    with lock:
        now = time.monotonic()
        wait_s = max(0.0, _NEXT_ALLOWED_AT.get(kind, 0.0) - now)
        if wait_s > 0:
            time.sleep(wait_s)
        now2 = time.monotonic()
        _NEXT_ALLOWED_AT[kind] = max(now2, _NEXT_ALLOWED_AT.get(kind, 0.0)) + interval
        return wait_s


def _extract_error_text(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def _is_billing_or_account_quota_error(status_code: int | None, error_obj: Any) -> bool:
    """Return True for 429 cases that retries cannot solve, such as arrears/quota plan."""
    if status_code != 429:
        return False
    text = _extract_error_text(error_obj).lower()
    permanent_markers = [
        "check your plan and billing",
        "billing details",
        "insufficient balance",
        "arrears",
        "欠费",
        "余额不足",
    ]
    return any(x in text for x in permanent_markers)


def _is_retryable_status(status_code: int | None, error_obj: Any = None) -> bool:
    if _is_billing_or_account_quota_error(status_code, error_obj):
        return False
    return status_code in {429, 500, 502, 503, 504}


def _can_retry_asr(attempt: int) -> bool:
    """Return whether a transient ASR failure may be retried after this attempt.

    ``ASR_MAX_RETRIES`` counts retries after the initial request.  A value of
    zero means unlimited retries only when ``ASR_RETRY_UNTIL_SUCCESS`` is true;
    otherwise zero means no retry.
    """

    completed_attempt = max(1, int(attempt))
    max_retries = max(0, int(config.ASR_MAX_RETRIES or 0))
    if bool(config.ASR_RETRY_UNTIL_SUCCESS) and max_retries == 0:
        return True
    return completed_attempt <= max_retries


def _retry_after_seconds(exc: urllib.error.HTTPError | None) -> float | None:
    if exc is None:
        return None
    value = None
    try:
        value = exc.headers.get("Retry-After")
    except Exception:
        value = None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except Exception:
        return None


def _jitter() -> float:
    high = max(0.0, float(config.ASR_RETRY_JITTER_SECONDS or 0.0))
    return random.uniform(0.0, high) if high > 0 else 0.0


def _asr_retry_delay(
    status_code: int | None, attempt: int, exc: urllib.error.HTTPError | None = None
) -> float:
    retry_after = _retry_after_seconds(exc)
    if status_code == 429:
        return max(float(config.ASR_429_SLEEP_SECONDS), retry_after or 0.0) + _jitter()
    base = max(1.0, float(config.ASR_RETRY_BASE_SECONDS))
    cap = max(base, float(config.ASR_RETRY_MAX_SECONDS))
    return min(cap, base * (2 ** max(0, attempt - 1))) + _jitter()


def parse_http_error(exc: urllib.error.HTTPError) -> Any:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
    try:
        return json.loads(body)
    except Exception:
        return body


def http_post_json(
    url: str,
    payload: dict,
    timeout_seconds: float,
    request_kind: str = "generic",
    wait_timeout_seconds: int | None = None,
    extra_headers: dict[str, str] | None = None,
    timing: dict[str, float] | None = None,
    api_key: str | None = None,
) -> tuple[int, dict, int, int]:
    """POST JSON to an OpenAI-compatible endpoint with optional smoothing and timing.

    ``api_key=None`` falls back to ``config.DASHSCOPE_API_KEY`` (legacy
    behavior); an explicit empty string omits the ``Authorization`` header
    entirely for no-auth self-hosted deployments (e.g. vLLM Qwen3-ASR-1.7B).
    """
    rate_wait_s = _smooth_rate_limit(request_kind)
    if timing is not None:
        timing["rate_limit_wait_ms"] = round(rate_wait_s * 1000.0, 3)

    serialize_started = time.perf_counter()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if timing is not None:
        timing["request_serialize_ms"] = round(
            (time.perf_counter() - serialize_started) * 1000.0,
            3,
        )
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    effective_key = (
        config.DASHSCOPE_API_KEY if api_key is None else str(api_key).strip()
    )
    if effective_key:
        headers["Authorization"] = "Bearer " + effective_key
    wait_timeout = int(
        config.DASHSCOPE_WAIT_TIMEOUT_SECONDS
        if wait_timeout_seconds is None
        else wait_timeout_seconds
    )
    if wait_timeout > 0:
        headers["X-DashScope-Wait-Timeout"] = str(wait_timeout)
    if extra_headers:
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    http_started = time.perf_counter()
    session = _get_http_session()
    if session is not None:
        # keep-alive：复用 TCP+TLS 连接；异常契约与 urlopen 保持一致。
        # 瞬态连接错误（SSL EOF / 连接重置，生产多发于进程重启后的首个
        # 请求）在传输层快速重试：重试会新建连接，第二次通常即成功。
        # 只重试 ConnectionError（SSLError 是其子类），不重试超时与 HTTP
        # 状态错误——避免长退避引发超时风暴；PLANNER_MAX_RETRIES=0 的
        # 部署依赖此兜底消化网络抖动。
        max_transport_retries = max(0, int(config.HTTP_TRANSPORT_RETRIES or 0))
        response: Any = None
        last_connect_exc: Any = None
        for transport_attempt in range(1 + max_transport_retries):
            if transport_attempt:
                time.sleep(min(1.0, 0.5 * transport_attempt))
                if timing is not None:
                    timing["transport_retries"] = transport_attempt
            try:
                response = session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=timeout_seconds + max(0, wait_timeout),
                )
                break
            except _requests.exceptions.Timeout as exc:
                raise urllib.error.URLError(
                    TimeoutError(f"http request timed out: {exc}")
                ) from exc
            except _requests.exceptions.ConnectionError as exc:
                last_connect_exc = exc
                logger.warning(
                    "HTTP keep-alive 连接瞬态错误，准备重试: attempt=%d error=%s",
                    transport_attempt + 1,
                    str(exc)[:160],
                )
                continue
            except _requests.RequestException as exc:
                raise urllib.error.URLError(f"{type(exc).__name__}: {exc}") from exc
        if response is None:
            raise urllib.error.URLError(
                f"{type(last_connect_exc).__name__}: {last_connect_exc}"
            ) from last_connect_exc
        status_code = int(response.status_code)
        resp_body = response.content.decode("utf-8", errors="replace")
        if status_code >= 400:
            raise urllib.error.HTTPError(
                url,
                status_code,
                str(getattr(response, "reason", "") or ""),
                response.headers,
                io.BytesIO(response.content),
            )
    else:
        with urllib.request.urlopen(req, timeout=timeout_seconds + max(0, wait_timeout)) as resp:
            status_code = resp.getcode()
            resp_body = resp.read().decode("utf-8", errors="replace")
    if timing is not None:
        timing["http_roundtrip_ms"] = round(
            (time.perf_counter() - http_started) * 1000.0,
            3,
        )

    decode_started = time.perf_counter()
    resp_json = json.loads(resp_body) if resp_body else {}
    if timing is not None:
        timing["response_json_ms"] = round(
            (time.perf_counter() - decode_started) * 1000.0,
            3,
        )
    return status_code, resp_json, len(body), len(resp_body.encode("utf-8", errors="ignore"))



_QWEN_AUDIO3_FLASH_MODEL = "qwen-audio-3.0-asr-flash"
_QWEN_AUDIO3_DOMAIN_TERMS = (
    "海能达",
    "数字对讲机",
    "组呼",
    "个呼",
    "双工呼叫",
    "短消息",
    "未读短消息",
    "切换区域",
    "切换信道",
    "待办提醒",
)
_MIME_TO_AUDIO_FORMAT = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/opus": "opus",
}


def is_qwen_audio3_asr_flash_model(model: str | None = None) -> bool:
    """Return whether the configured model requires the native Audio 3.0 API."""

    value = str(model if model is not None else config.ASR_MODEL).strip().lower()
    return value == _QWEN_AUDIO3_FLASH_MODEL or bool(
        re.fullmatch(r"qwen-audio-3\.0-asr-flash-\d{4}-\d{2}-\d{2}", value)
    )


_QWEN_AUDIO3_STREAMING_MODEL = "qwen-audio-3.0-asr-flash-streaming"


def is_qwen_audio3_asr_streaming_model(model: str | None = None) -> bool:
    """Return whether the configured model uses the WebSocket duplex ASR API."""

    value = str(model if model is not None else config.ASR_MODEL).strip().lower()
    return value == _QWEN_AUDIO3_STREAMING_MODEL or bool(
        re.fullmatch(r"qwen-audio-3\.0-asr-flash-streaming-\d{4}-\d{2}-\d{2}", value)
    )


def _valid_hotword_weight(value: Any, default: int) -> int:
    allowed = {1, 2, 3, 4, 5, 50}
    try:
        fallback = int(default)
    except (TypeError, ValueError):
        fallback = 4
    if fallback not in allowed:
        fallback = 4
    try:
        weight = int(value)
    except (TypeError, ValueError):
        weight = fallback
    return weight if weight in allowed else fallback


def _valid_hotword_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Official limits: no more than 15 total characters when non-ASCII is used;
    # ASCII-only terms may contain no more than 7 space-separated segments.
    if any(ord(ch) > 127 for ch in text):
        return text if len(text) <= 15 else ""
    return text if len(text.split()) <= 7 else ""


def _parse_extra_hotwords(raw: str) -> dict[str, int]:
    """Parse JSON or comma-separated ``word=weight`` hotword configuration."""

    raw = str(raw or "").strip()
    if not raw:
        return {}
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    items: list[tuple[Any, Any]] = []
    if isinstance(parsed, dict):
        items.extend(parsed.items())
    else:
        for part in re.split(r"[,，;；]", raw):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                word, weight = part.rsplit("=", 1)
            elif ":" in part:
                word, weight = part.rsplit(":", 1)
            else:
                word, weight = part, config.QWEN_AUDIO_ASR_ENTITY_HOTWORD_WEIGHT
            items.append((word, weight))
    result: dict[str, int] = {}
    for word, weight in items:
        clean = _valid_hotword_text(word)
        if clean:
            result[clean] = _valid_hotword_weight(
                weight, config.QWEN_AUDIO_ASR_ENTITY_HOTWORD_WEIGHT
            )
    return result


def build_qwen_audio3_hotwords() -> dict[str, int]:
    """Build request-level hotwords from the live business directories."""

    entity_weight = _valid_hotword_weight(
        config.QWEN_AUDIO_ASR_ENTITY_HOTWORD_WEIGHT, 4
    )
    term_weight = _valid_hotword_weight(config.QWEN_AUDIO_ASR_TERM_HOTWORD_WEIGHT, 3)
    hotwords: dict[str, int] = {}

    try:
        contact_map = get_contact_map()
        for kind in ("private", "group", "standby"):
            for name in contact_map.get(kind, {}):
                clean = _valid_hotword_text(name)
                if clean:
                    hotwords[clean] = max(hotwords.get(clean, 0), entity_weight)
    except Exception as exc:
        logger.warning("读取联系人热词失败: %s", exc)

    try:
        zone_channel_map = get_zone_channel_map()
        for kind in ("zones", "channels"):
            for name in zone_channel_map.get(kind, {}):
                clean = _valid_hotword_text(name)
                if clean:
                    hotwords[clean] = max(hotwords.get(clean, 0), entity_weight)
    except Exception as exc:
        logger.warning("读取区域/信道热词失败: %s", exc)

    for term in _QWEN_AUDIO3_DOMAIN_TERMS:
        hotwords[term] = max(hotwords.get(term, 0), term_weight)

    for word, weight in _parse_extra_hotwords(
        config.QWEN_AUDIO_ASR_EXTRA_HOTWORDS
    ).items():
        hotwords[word] = max(hotwords.get(word, 0), weight)

    # The model supports up to 2,000 inline hotwords. Keep deterministic order.
    return dict(list(sorted(hotwords.items()))[:2000])


def build_qwen_audio3_context(hotwords: dict[str, int] | None = None) -> str:
    """Build a compact exact-word context message, capped at the API limit."""

    hotwords = hotwords if hotwords is not None else build_qwen_audio3_hotwords()
    try:
        contacts = get_contact_map()
    except Exception:
        contacts = {}
    try:
        zone_channels = get_zone_channel_map()
    except Exception:
        zone_channels = {}

    private_names = list((contacts.get("private") or {}).keys())
    group_names = list((contacts.get("group") or {}).keys())
    zone_names = list((zone_channels.get("zones") or {}).keys())
    channel_names = list((zone_channels.get("channels") or {}).keys())
    context = build_asr_context_prompt(
        private_names=private_names,
        group_names=group_names,
        zone_names=zone_names,
        channel_names=channel_names,
        domain_terms=_QWEN_AUDIO3_DOMAIN_TERMS[2:],
    )
    configured_limit = int(config.QWEN_AUDIO_ASR_CONTEXT_MAX_CHARS or 0)
    if configured_limit <= 0:
        return ""
    return context[: min(400, configured_limit)]


def _audio_format_from_data_url(audio_data_url: str) -> str:
    value = str(audio_data_url or "").strip()
    if value.startswith("data:"):
        mime = value[5:].split(";", 1)[0].strip().lower()
        return _MIME_TO_AUDIO_FORMAT.get(mime, "wav")
    path = value.split("?", 1)[0].split("#", 1)[0]
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "wave": "wav",
        "mpeg": "mp3",
        "oga": "ogg",
    }.get(suffix, suffix or "wav")


def build_qwen_audio3_asr_payload(audio_data_url: str) -> dict:
    """Build the native DashScope request for Qwen-Audio-3.0-ASR-Flash."""

    audio_format = _audio_format_from_data_url(audio_data_url)
    hotwords = (
        build_qwen_audio3_hotwords()
        if config.QWEN_AUDIO_ASR_ENABLE_HOTWORDS
        else {}
    )
    messages: list[dict[str, Any]] = []
    if config.QWEN_AUDIO_ASR_ENABLE_CONTEXT:
        context = build_qwen_audio3_context(hotwords)
        if context:
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": context}],
                }
            )
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": audio_data_url}}
            ],
        }
    )
    parameters: dict[str, Any] = {"format": audio_format}
    if audio_format == "wav" and int(config.ASR_TARGET_SAMPLE_RATE or 0) > 0:
        parameters["sample_rate"] = str(config.ASR_TARGET_SAMPLE_RATE)
    if config.ASR_LANGUAGE:
        language_hints = [
            item.strip()
            for item in re.split(r"[,，;；]", config.ASR_LANGUAGE)
            if item.strip()
        ][:4]
        if language_hints:
            parameters["language_hints"] = language_hints
    if hotwords:
        parameters["vocabulary"] = hotwords
    return {
        "model": config.ASR_MODEL,
        "input": {"messages": messages},
        "parameters": parameters,
    }


def extract_qwen_audio3_asr_text(resp_json: dict) -> str:
    output = resp_json.get("output")
    if not isinstance(output, dict):
        return ""
    text = output.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    sentence = output.get("sentence")
    if isinstance(sentence, dict):
        text = sentence.get("text")
        if isinstance(text, str):
            return text.strip()
    return ""


def extract_qwen_audio3_annotations(resp_json: dict) -> list:
    output = resp_json.get("output")
    sentence = output.get("sentence") if isinstance(output, dict) else None
    words = sentence.get("words") if isinstance(sentence, dict) else None
    return words if isinstance(words, list) else []

def build_qwen3_asr_payload(wav_or_audio_data_url: str) -> dict:
    asr_options = {"enable_itn": config.ENABLE_ITN}
    if config.ASR_LANGUAGE:
        asr_options["language"] = config.ASR_LANGUAGE
    return {
        "model": config.ASR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": wav_or_audio_data_url}}
                ],
            },
        ],
        "stream": False,
        "asr_options": asr_options,
    }


def build_local_openai_asr_payload(audio_data_url: str) -> dict:
    """Build the OpenAI-compatible payload for the self-hosted local ASR.

    自部署服务（如 vLLM 部署的 Qwen3-ASR-1.7B）走 ``/v1/chat/completions``：
    音频以纯 base64（去 data-URL 前缀）内嵌在 ``input_audio.data``，格式标注
    在 ``input_audio.format``。不带 asr_options 等扩展参数，保持最普通的
    OpenAI 请求形状以兼容任何 OpenAI 兼容服务器。
    """

    audio_format = _audio_format_from_data_url(audio_data_url)
    audio_b64 = audio_data_url
    if audio_b64.startswith("data:"):
        audio_b64 = audio_b64.partition(",")[2]
    return {
        "model": config.LOCAL_ASR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": audio_b64, "format": audio_format},
                    }
                ],
            }
        ],
        "stream": False,
    }


def extract_qwen3_asr_text(resp_json: dict) -> str:
    try:
        content = resp_json["choices"][0]["message"].get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
            text = "".join(parts).strip()
            if text:
                return text
    except Exception:
        pass
    try:
        text = (resp_json.get("output") or {}).get("text")
        if isinstance(text, str):
            return text.strip()
    except Exception:
        pass
    return ""


def extract_annotations(resp_json: dict) -> list:
    try:
        return resp_json["choices"][0]["message"].get("annotations") or []
    except Exception:
        return []



def _new_asr_timing() -> dict[str, Any]:
    return {
        "rate_limit_wait_ms": 0.0,
        "request_serialize_ms": 0.0,
        "http_ms": 0.0,
        "response_json_ms": 0.0,
        "retry_wait_ms": 0.0,
        "attempt_ms": [],
        "attempts": 0,
        "qwen_asr_total_ms": 0.0,
    }


def _record_asr_attempt(timing: dict[str, Any], request_timing: dict[str, float], attempt_started: float) -> None:
    for source_key, target_key in (
        ("rate_limit_wait_ms", "rate_limit_wait_ms"),
        ("request_serialize_ms", "request_serialize_ms"),
        ("http_roundtrip_ms", "http_ms"),
        ("response_json_ms", "response_json_ms"),
    ):
        timing[target_key] = round(float(timing.get(target_key) or 0.0) + float(request_timing.get(source_key) or 0.0), 3)
    elapsed = perf_ms(attempt_started)
    timing.setdefault("attempt_ms", []).append(elapsed)
    # Compatibility field: now means cumulative ASR request-attempt time instead of only the last attempt.
    timing["http_request_ms"] = round(sum(float(v or 0.0) for v in timing.get("attempt_ms", [])), 3)


def _record_asr_retry(timing: dict[str, Any], retry_waits: list[float], delay_seconds: float) -> None:
    retry_waits.append(round(delay_seconds, 3))
    timing["retry_wait_ms"] = round(float(timing.get("retry_wait_ms") or 0.0) + delay_seconds * 1000.0, 3)


def _asr_return_error(
    error: str,
    timing: dict[str, Any],
    status_code: int | None = None,
    raw_response: Any = None,
    attempts: int = 1,
    retry_waits: list[float] | None = None,
) -> dict:
    timing["qwen_asr_total_ms"] = timing.get("qwen_asr_total_ms") or 0.0
    timing["attempts"] = attempts
    timing["retry_waits"] = retry_waits or []
    return {
        "ok": False,
        "error": error,
        "raw_text": "",
        "text": "",
        "annotations": [],
        "status_code": status_code,
        "model": config.ASR_MODEL,
        "usage": None,
        "raw_response": raw_response,
        "timing": timing,
        "attempts": attempts,
        "retry_waits": retry_waits or [],
    }


def call_qwen3_asr_flash(
    audio_data_url: str,
    *,
    endpoint_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    payload_builder: Any = None,
    require_api_key: bool = True,
) -> dict:
    """Call an OpenAI-compatible ASR endpoint with conservative retry policy.

    Defaults keep the legacy DashScope Qwen3-ASR-Flash behavior (endpoint,
    API key, model and payload all from config). Keyword arguments let the
    self-hosted local ASR reuse the same retry/timing/error contract.

    429, 503 and transient network failures are retried according to config. When
    ASR_RETRY_UNTIL_SUCCESS=true, transient retryable errors are retried forever.
    Permanent account/config/request errors still fail fast.
    """
    timing: dict[str, Any] = _new_asr_timing()
    retry_waits: list[float] = []
    t_all = time.perf_counter()
    effective_key = api_key if api_key is not None else config.DASHSCOPE_API_KEY
    if require_api_key and not effective_key:
        return _asr_return_error("missing DASHSCOPE_API_KEY", timing, attempts=0)
    effective_model = model_name or config.ASR_MODEL
    effective_url = endpoint_url or config.DASHSCOPE_CHAT_COMPLETIONS_URL

    payload = (
        payload_builder(audio_data_url)
        if payload_builder is not None
        else build_qwen3_asr_payload(audio_data_url)
    )
    attempt = 0
    empty_text_failures = 0

    while True:
        attempt += 1
        try:
            t1 = time.perf_counter()
            request_timing: dict[str, float] = {}
            status_code, resp_json, req_bytes, resp_bytes = http_post_json(
                effective_url,
                payload,
                config.HTTP_TIMEOUT_SECONDS,
                request_kind="asr",
                wait_timeout_seconds=config.DASHSCOPE_WAIT_TIMEOUT_SECONDS,
                timing=request_timing,
                api_key=api_key,
            )
            _record_asr_attempt(timing, request_timing, t1)
            timing["request_body_bytes"] = req_bytes
            timing["response_body_bytes"] = resp_bytes
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits

            raw_text = extract_qwen3_asr_text(resp_json)
            if 200 <= status_code < 300 and raw_text:
                return {
                    "ok": True,
                    "error": None,
                    "raw_text": raw_text,
                    "text": raw_text,
                    "annotations": extract_annotations(resp_json),
                    "status_code": status_code,
                    "model": resp_json.get("model") or effective_model,
                    "usage": resp_json.get("usage"),
                    "raw_response": resp_json,
                    "timing": timing,
                    "attempts": attempt,
                    "retry_waits": retry_waits,
                }

            if 200 <= status_code < 300 and not raw_text:
                empty_text_failures += 1
                if empty_text_failures <= max(0, int(config.ASR_EMPTY_TEXT_MAX_RETRIES or 0)):
                    delay = min(20.0, 3.0 * empty_text_failures) + _jitter()
                    _record_asr_retry(timing, retry_waits, delay)
                    logger.warning(
                        "ASR 返回空文本，准备重试: attempt=%d sleep=%.2fs", attempt, delay
                    )
                    time.sleep(delay)
                    continue
                return {
                    "ok": False,
                    "error": "empty ASR text after %d attempts" % attempt,
                    "raw_text": "",
                    "text": "",
                    "annotations": extract_annotations(resp_json),
                    "status_code": status_code,
                    "model": resp_json.get("model") or effective_model,
                    "usage": resp_json.get("usage"),
                    "raw_response": resp_json,
                    "timing": timing,
                    "attempts": attempt,
                    "retry_waits": retry_waits,
                }

            if _is_retryable_status(status_code, resp_json) and _can_retry_asr(attempt):
                delay = _asr_retry_delay(status_code, attempt)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "ASR 状态异常，准备重试: status=%s attempt=%d sleep=%.2fs",
                    status_code,
                    attempt,
                    delay,
                )
                time.sleep(delay)
                continue

            return _asr_return_error(
                "http status %s" % status_code,
                timing,
                status_code=status_code,
                raw_response=resp_json,
                attempts=attempt,
                retry_waits=retry_waits,
            )

        except urllib.error.HTTPError as exc:
            err_obj = parse_http_error(exc)
            status_code = exc.code
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits
            if _is_retryable_status(status_code, err_obj) and _can_retry_asr(attempt):
                delay = _asr_retry_delay(status_code, attempt, exc=exc)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "ASR HTTP 异常，准备重试: status=%s attempt=%d sleep=%.2fs error=%s",
                    status_code,
                    attempt,
                    delay,
                    _extract_error_text(err_obj)[:160],
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "http error: %s" % status_code,
                timing,
                status_code=status_code,
                raw_response=err_obj,
                attempts=attempt,
                retry_waits=retry_waits,
            )
        except Exception as exc:
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits
            if _can_retry_asr(attempt):
                delay = _asr_retry_delay(None, attempt)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "ASR 请求异常，准备重试: attempt=%d sleep=%.2fs error=%s",
                    attempt,
                    delay,
                    str(exc)[:160],
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "request failed: %s" % exc,
                timing,
                status_code=None,
                raw_response=None,
                attempts=attempt,
                retry_waits=retry_waits,
            )




def call_qwen_audio3_asr_flash(audio_data_url: str) -> dict:
    """Call Qwen-Audio-3.0-ASR-Flash through the native multimodal API."""

    timing: dict[str, Any] = _new_asr_timing()
    retry_waits: list[float] = []
    t_all = time.perf_counter()
    if not config.DASHSCOPE_API_KEY:
        return _asr_return_error("missing DASHSCOPE_API_KEY", timing, attempts=0)
    if not config.QWEN_AUDIO_ASR_URL:
        return _asr_return_error("missing QWEN_AUDIO_ASR_URL", timing, attempts=0)

    payload = build_qwen_audio3_asr_payload(audio_data_url)
    attempt = 0
    empty_text_failures = 0
    while True:
        attempt += 1
        try:
            t1 = time.perf_counter()
            request_timing: dict[str, float] = {}
            status_code, resp_json, req_bytes, resp_bytes = http_post_json(
                config.QWEN_AUDIO_ASR_URL,
                payload,
                config.HTTP_TIMEOUT_SECONDS,
                request_kind="asr",
                wait_timeout_seconds=config.DASHSCOPE_WAIT_TIMEOUT_SECONDS,
                extra_headers={"X-DashScope-SSE": "disable"},
                timing=request_timing,
            )
            _record_asr_attempt(timing, request_timing, t1)
            timing["request_body_bytes"] = req_bytes
            timing["response_body_bytes"] = resp_bytes
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits

            raw_text = extract_qwen_audio3_asr_text(resp_json)
            annotations = extract_qwen_audio3_annotations(resp_json)
            if 200 <= status_code < 300 and raw_text:
                return {
                    "ok": True,
                    "error": None,
                    "raw_text": raw_text,
                    "text": raw_text,
                    "annotations": annotations,
                    "status_code": status_code,
                    "model": config.ASR_MODEL,
                    "usage": resp_json.get("usage"),
                    "request_id": resp_json.get("request_id"),
                    "raw_response": resp_json,
                    "timing": timing,
                    "attempts": attempt,
                    "retry_waits": retry_waits,
                }

            if 200 <= status_code < 300 and not raw_text:
                empty_text_failures += 1
                if empty_text_failures <= max(0, int(config.ASR_EMPTY_TEXT_MAX_RETRIES or 0)):
                    delay = min(20.0, 3.0 * empty_text_failures) + _jitter()
                    _record_asr_retry(timing, retry_waits, delay)
                    logger.warning(
                        "Qwen-Audio 3.0 ASR 返回空文本，准备重试: attempt=%d sleep=%.2fs",
                        attempt,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                return {
                    "ok": False,
                    "error": "empty ASR text after %d attempts" % attempt,
                    "raw_text": "",
                    "text": "",
                    "annotations": annotations,
                    "status_code": status_code,
                    "model": config.ASR_MODEL,
                    "usage": resp_json.get("usage"),
                    "request_id": resp_json.get("request_id"),
                    "raw_response": resp_json,
                    "timing": timing,
                    "attempts": attempt,
                    "retry_waits": retry_waits,
                }

            if _is_retryable_status(status_code, resp_json) and _can_retry_asr(attempt):
                delay = _asr_retry_delay(status_code, attempt)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "Qwen-Audio 3.0 ASR 状态异常，准备重试: status=%s attempt=%d sleep=%.2fs",
                    status_code,
                    attempt,
                    delay,
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "http status %s" % status_code,
                timing,
                status_code=status_code,
                raw_response=resp_json,
                attempts=attempt,
                retry_waits=retry_waits,
            )
        except urllib.error.HTTPError as exc:
            err_obj = parse_http_error(exc)
            status_code = exc.code
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits
            if _is_retryable_status(status_code, err_obj) and _can_retry_asr(attempt):
                delay = _asr_retry_delay(status_code, attempt, exc=exc)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "Qwen-Audio 3.0 ASR HTTP 异常，准备重试: status=%s attempt=%d sleep=%.2fs error=%s",
                    status_code,
                    attempt,
                    delay,
                    _extract_error_text(err_obj)[:160],
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "http error: %s" % status_code,
                timing,
                status_code=status_code,
                raw_response=err_obj,
                attempts=attempt,
                retry_waits=retry_waits,
            )
        except Exception as exc:
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits
            if _can_retry_asr(attempt):
                delay = _asr_retry_delay(None, attempt)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "Qwen-Audio 3.0 ASR 请求异常，准备重试: attempt=%d sleep=%.2fs error=%s",
                    attempt,
                    delay,
                    str(exc)[:160],
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "request failed: %s" % exc,
                timing,
                status_code=None,
                raw_response=None,
                attempts=attempt,
                retry_waits=retry_waits,
            )


# ---------------------------------------------------------------------------
# 流式 ASR（qwen-audio-3.0-asr-flash-streaming，WebSocket 双工协议）：
# 建连 → run-task → task-started → 二进制音频帧（分块）→ result-generated*
# → finish-task → task-finished。本场景是 flush 后的整段音频一次性推流，
# 连接不跨请求复用（60s 空闲断连，池化收益低且引入 stale 风险）；
# timing/重试/错误契约与 native HTTP 版本保持一致。
# ---------------------------------------------------------------------------
_STREAMING_AUDIO_CHUNK_BYTES = 3200  # ~200ms WAV-8k/16k 帧，短音频快速推完
_RETRYABLE_STREAMING_ERROR_CODES = (
    "Throttling",
    "throttling",
    "Timeout",
    "timeout",
    "InternalError",
    "ServiceUnavailable",
    "Unavailable",
)


class _StreamingAsrTaskError(RuntimeError):
    """Raised when the streaming server rejects a task (task-failed event)."""

    def __init__(
        self,
        error_code: str | None,
        error_message: str | None,
        payload: Any = None,
    ) -> None:
        self.error_code = str(error_code or "")
        self.error_message = str(error_message or "")
        self.payload = payload
        super().__init__(f"{self.error_code}: {self.error_message}")


def build_qwen_audio3_streaming_run_task(
    task_id: str, audio_format: str = "wav"
) -> dict:
    """Build the WebSocket run-task directive for streaming ASR."""

    hotwords = (
        build_qwen_audio3_hotwords()
        if config.QWEN_AUDIO_ASR_ENABLE_HOTWORDS
        else {}
    )
    parameters: dict[str, Any] = {"format": audio_format}
    if audio_format == "wav" and int(config.ASR_TARGET_SAMPLE_RATE or 0) > 0:
        parameters["sample_rate"] = str(config.ASR_TARGET_SAMPLE_RATE)
    if config.ASR_LANGUAGE:
        language_hints = [
            item.strip()
            for item in re.split(r"[,，;；]", config.ASR_LANGUAGE)
            if item.strip()
        ][:4]
        if language_hints:
            parameters["language_hints"] = language_hints
    if hotwords:
        parameters["vocabulary"] = hotwords
    if config.QWEN_AUDIO_ASR_ENABLE_CONTEXT:
        context = build_qwen_audio3_context(hotwords)
        if context:
            parameters["context"] = context
    return {
        "header": {
            "action": "run-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": config.ASR_MODEL,
            "parameters": parameters,
            "input": {},
        },
    }


def _decode_audio_data_url(audio_data_url: str) -> bytes:
    """Decode a data-URL (or raw base64) audio payload to raw bytes."""

    value = str(audio_data_url or "").strip()
    b64 = value.partition(",")[2] if value.startswith("data:") else value
    if not b64:
        return b""
    try:
        return base64.b64decode(b64)
    except Exception:
        return b""


def _streaming_asr_once(
    audio_bytes: bytes, audio_format: str
) -> tuple[str, list, str | None, int]:
    """Run one full streaming ASR task; return (text, annotations, request_id, events)."""

    task_id = str(uuid.uuid4())
    run_task = build_qwen_audio3_streaming_run_task(task_id, audio_format)
    finish_task = {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }
    recv_timeout = max(5.0, float(config.HTTP_TIMEOUT_SECONDS or 30.0))
    with _ws_connect(
        config.QWEN_ASR_STREAMING_URL,
        additional_headers={"Authorization": "Bearer " + config.DASHSCOPE_API_KEY},
        open_timeout=recv_timeout,
        close_timeout=5.0,
    ) as connection:
        connection.send(json.dumps(run_task, ensure_ascii=False))
        final_parts: list[str] = []
        current_text = ""
        annotations: list = []
        request_id: str | None = None
        events = 0
        audio_sent = False
        while True:
            message = connection.recv(timeout=recv_timeout)
            if not isinstance(message, str):
                continue  # 服务器不会主动发二进制，防御性跳过
            data = json.loads(message)
            header = data.get("header") if isinstance(data, dict) else None
            if not isinstance(header, dict):
                continue
            event = str(header.get("event") or "")
            if not request_id and header.get("request_id"):
                request_id = str(header["request_id"])
            if event == "task-started":
                # 整段音频分块快速推流（短音频无需按实时速率 pacing）。
                for offset in range(0, len(audio_bytes), _STREAMING_AUDIO_CHUNK_BYTES):
                    connection.send(
                        audio_bytes[offset : offset + _STREAMING_AUDIO_CHUNK_BYTES]
                    )
                connection.send(json.dumps(finish_task, ensure_ascii=False))
                audio_sent = True
            elif event == "result-generated":
                events += 1
                payload = data.get("payload")
                output = payload.get("output") if isinstance(payload, dict) else None
                sentence = (
                    output.get("sentence") if isinstance(output, dict) else None
                )
                if not isinstance(sentence, dict):
                    continue
                text = str(sentence.get("text") or "").strip()
                words = sentence.get("words")
                if isinstance(words, list) and words:
                    annotations = words
                end_time = sentence.get("end_time")
                # end_time == -1 表示该句尚未结束（部分结果）；>=0 为句子终稿。
                sentence_ended = isinstance(end_time, (int, float)) and end_time >= 0
                if sentence_ended:
                    if text:
                        final_parts.append(text)
                    current_text = ""
                else:
                    current_text = text
            elif event == "task-finished":
                if not audio_sent:
                    raise RuntimeError("task finished before audio was sent")
                text = "".join(final_parts) + (current_text or "")
                return text.strip(), annotations, request_id, events
            elif event == "task-failed":
                raise _StreamingAsrTaskError(
                    header.get("error_code"),
                    header.get("error_message"),
                    data,
                )


def call_qwen_audio3_asr_streaming(audio_data_url: str) -> dict:
    """Call qwen-audio-3.0-asr-flash-streaming over the WebSocket duplex API."""

    timing: dict[str, Any] = _new_asr_timing()
    retry_waits: list[float] = []
    t_all = time.perf_counter()
    if not config.DASHSCOPE_API_KEY:
        return _asr_return_error("missing DASHSCOPE_API_KEY", timing, attempts=0)
    if not config.QWEN_ASR_STREAMING_URL:
        return _asr_return_error("missing QWEN_ASR_STREAMING_URL", timing, attempts=0)
    if _ws_connect is None:  # websockets 未安装时优雅回退 native HTTP
        logger.warning(
            "websockets 未安装，流式 ASR 回退 qwen-audio-3.0-asr-flash HTTP 调用"
        )
        return call_qwen_audio3_asr_flash(audio_data_url)
    audio_bytes = _decode_audio_data_url(audio_data_url)
    if not audio_bytes:
        return _asr_return_error(
            "empty audio payload for streaming ASR", timing, attempts=0
        )
    audio_format = _audio_format_from_data_url(audio_data_url)

    attempt = 0
    empty_text_failures = 0
    while True:
        attempt += 1
        try:
            t1 = time.perf_counter()
            raw_text, annotations, request_id, events = _streaming_asr_once(
                audio_bytes, audio_format
            )
            elapsed = perf_ms(t1)
            timing.setdefault("attempt_ms", []).append(elapsed)
            timing["http_request_ms"] = round(
                sum(float(v or 0.0) for v in timing.get("attempt_ms", [])), 3
            )
            timing["http_ms"] = timing["http_request_ms"]
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits
            timing["streaming_result_events"] = events
            timing["audio_payload_bytes"] = len(audio_bytes)

            if raw_text:
                return {
                    "ok": True,
                    "error": None,
                    "raw_text": raw_text,
                    "text": raw_text,
                    "annotations": annotations,
                    "status_code": 200,
                    "model": config.ASR_MODEL,
                    "usage": None,
                    "request_id": request_id,
                    "raw_response": {
                        "streaming": True,
                        "events": events,
                        "request_id": request_id,
                    },
                    "timing": timing,
                    "attempts": attempt,
                    "retry_waits": retry_waits,
                }

            empty_text_failures += 1
            if empty_text_failures <= max(
                0, int(config.ASR_EMPTY_TEXT_MAX_RETRIES or 0)
            ):
                delay = min(20.0, 3.0 * empty_text_failures) + _jitter()
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "流式 ASR 返回空文本，准备重试: attempt=%d sleep=%.2fs",
                    attempt,
                    delay,
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "empty ASR text after %d attempts" % attempt,
                timing,
                status_code=200,
                raw_response={"streaming": True, "events": events},
                attempts=attempt,
                retry_waits=retry_waits,
            )
        except _StreamingAsrTaskError as exc:
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits
            if (
                exc.error_code in _RETRYABLE_STREAMING_ERROR_CODES
                and _can_retry_asr(attempt)
            ):
                delay = _asr_retry_delay(None, attempt)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "流式 ASR task-failed，准备重试: attempt=%d sleep=%.2fs error=%s",
                    attempt,
                    delay,
                    str(exc)[:160],
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "task failed: %s" % exc,
                timing,
                status_code=None,
                raw_response=exc.payload,
                attempts=attempt,
                retry_waits=retry_waits,
            )
        except Exception as exc:
            timing["qwen_asr_total_ms"] = perf_ms(t_all)
            timing["attempts"] = attempt
            timing["retry_waits"] = retry_waits
            if _can_retry_asr(attempt):
                delay = _asr_retry_delay(None, attempt)
                _record_asr_retry(timing, retry_waits, delay)
                logger.warning(
                    "流式 ASR 请求异常，准备重试: attempt=%d sleep=%.2fs error=%s",
                    attempt,
                    delay,
                    str(exc)[:160],
                )
                time.sleep(delay)
                continue
            return _asr_return_error(
                "request failed: %s" % exc,
                timing,
                status_code=None,
                raw_response=None,
                attempts=attempt,
                retry_waits=retry_waits,
            )


_LOCAL_ASR_TEXT_RE = re.compile(r"<asr_text>(.*?)(?:</asr_text>|$)", re.DOTALL)


def clean_local_asr_text(text: str) -> str:
    """Strip self-hosted Qwen3-ASR output markers (e.g. vLLM serving).

    自部署 Qwen3-ASR 的 chat 输出带内部标记，形如
    ``language Chinese<asr_text>转写文本。``（有时带 ``</asr_text>`` 闭合，
    有时被截断只剩开头）。提取 ``<asr_text>`` 之后的内容作为转写文本。
    """

    value = str(text or "")
    match = _LOCAL_ASR_TEXT_RE.search(value)
    if match:
        return match.group(1).strip()
    return value.strip()


def call_local_openai_asr(audio_data_url: str) -> dict:
    """Call the self-hosted OpenAI-compatible ASR (e.g. vLLM Qwen3-ASR-1.7B).

    复用 call_qwen3_asr_flash 的重试/限流/timing/错误契约，仅替换端点、
    鉴权（无 API Key，不带 Authorization 头）、模型名与 payload 构造。
    """

    result = call_qwen3_asr_flash(
        audio_data_url,
        endpoint_url=config.LOCAL_ASR_CHAT_COMPLETIONS_URL,
        api_key=config.LOCAL_ASR_API_KEY,
        model_name=config.LOCAL_ASR_MODEL,
        payload_builder=build_local_openai_asr_payload,
        require_api_key=False,
    )
    # 错误路径的 _asr_return_error 只知道 config.ASR_MODEL，这里统一回填
    # 本地模型名，避免日志/结果里出现误导性的阿里模型名。
    if result.get("model") in (None, config.ASR_MODEL):
        result["model"] = config.LOCAL_ASR_MODEL
    if result.get("ok"):
        # raw_text 保留原始输出，text 是清理后的业务转写。
        result["text"] = clean_local_asr_text(result.get("raw_text"))
    return result


def call_configured_asr(audio_data_url: str) -> dict:
    """Dispatch to the API contract required by the configured ASR model."""

    if config.ASR_PROVIDER == "local":
        return call_local_openai_asr(audio_data_url)
    if is_qwen_audio3_asr_streaming_model():
        return call_qwen_audio3_asr_streaming(audio_data_url)
    if is_qwen_audio3_asr_flash_model():
        return call_qwen_audio3_asr_flash(audio_data_url)
    return call_qwen3_asr_flash(audio_data_url)

