# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/calls/summary_generator.py
# 所属层次：通话领域层
# 主要职责：负责通话关联、生命周期、转写、摘要、翻译、查询和通话内容能力。
# 项目内直接依赖：.., ..dashscope_client
# 主要公开符号：normalize_transcript, generate_call_summary, generate_call_translation
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Pure call-content generators.

The generators accept an explicit transcript and call metadata.  They never own
call lifecycle state and never deliver messages.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
from typing import Any, Iterable

from .. import config
from ..dashscope_client import http_post_json
from ..prompts import build_call_summary_messages, build_call_translation_messages

CALL_SUMMARY_MAX_CHARS = max(80, int(os.environ.get("CALL_SUMMARY_MAX_CHARS", "80") or "80"))
CALL_SUMMARY_MAX_TRANSCRIPT_CHARS = max(
    1000, int(os.environ.get("CALL_SUMMARY_MAX_TRANSCRIPT_CHARS", "6000") or "6000")
)
CALL_SUMMARY_MAX_RETRIES = max(0, int(os.environ.get("CALL_SUMMARY_MAX_RETRIES", "2") or "2"))
CALL_TRANSLATION_MAX_CHARS = max(
    80, int(os.environ.get("CALL_TRANSLATION_MAX_CHARS", "1200") or "1200")
)
CALL_TRANSLATION_MAX_TRANSCRIPT_CHARS = max(
    1000, int(os.environ.get("CALL_TRANSLATION_MAX_TRANSCRIPT_CHARS", "6000") or "6000")
)
CALL_TRANSLATION_MAX_RETRIES = max(
    0, int(os.environ.get("CALL_TRANSLATION_MAX_RETRIES", "2") or "2")
)


def _extract_chat_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"].get("content")
    except Exception:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or item.get("content") or "")
            for item in content
            if isinstance(item, dict)
        ).strip()
    return ""


def _clip(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def _normalize_model_summary(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"^```(?:text|markdown)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(
        r"^(?:#+\s*)?(?:通话(?:摘要|概要)|高度概括|摘要)\s*[:：-]\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", raw_line).strip()
        if line:
            lines.append(line)
    return _clip(" ".join(lines), limit)


def _normalize_model_translation(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"^```(?:text|markdown)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(
        r"^(?:通话翻译(?:[（(](?:英文|中文)[）)])?|translation(?:\s+to\s+(?:english|chinese))?)\s*[:：-]\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return _clip("\n".join(line.strip() for line in value.splitlines() if line.strip()), limit)


def _turn_text(raw: dict[str, Any]) -> str:
    return " ".join(
        str(raw.get("text") or raw.get("transcript") or raw.get("normalized_text") or "").split()
    )


def normalize_transcript(
    transcript: Iterable[dict[str, Any]], *, max_chars: int
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    used = 0
    for raw in transcript:
        if not isinstance(raw, dict):
            continue
        text = _turn_text(raw)
        if not text:
            continue
        item = {
            "turn_id": raw.get("turn_id"),
            "source_id": raw.get("source_id") or raw.get("speaker_radio_id"),
            "channel_id": raw.get("channel_id"),
            "started_at": raw.get("started_at"),
            "ended_at": raw.get("ended_at"),
            "text": text,
        }
        serialized = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if used + len(serialized) > max_chars:
            break
        used += len(serialized)
        turns.append(item)
    return turns


def _fallback_summary(turns: list[dict[str, Any]], limit: int) -> str:
    texts: list[str] = []
    for turn in turns:
        text = str(turn.get("text") or "").strip()
        if not text or text in texts:
            continue
        source = turn.get("source_id")
        texts.append((f"终端{source}：" if source is not None else "") + text)
    if not texts:
        return "未获得可用的语音转写，无法生成通话摘要。"
    return _clip("自动转写要点：" + "；".join(texts), limit)


def _context(call_context: dict[str, Any] | None) -> dict[str, Any]:
    source = call_context or {}
    return {
        "call_id": source.get("call_id"),
        "call_type": source.get("call_type"),
        "callee_id": source.get("callee_id")
        or source.get("resolved_target_id")
        or source.get("observed_target_id"),
        "talk_mode": source.get("talk_mode"),
        "priority": source.get("priority"),
        "started_at": source.get("started_at")
        or source.get("requested_at")
        or source.get("first_audio_at"),
        "ended_at": source.get("ended_at"),
        "end_reason": source.get("end_reason"),
    }




def _summary_messages(
    turns: list[dict[str, Any]], limit: int, *, call_context: dict[str, Any]
) -> tuple[str, str]:
    return build_call_summary_messages(turns, limit, call_context=call_context)


def _translation_messages(
    turns: list[dict[str, Any]], language: str, limit: int, *, call_context: dict[str, Any]
) -> tuple[str, str]:
    return build_call_translation_messages(
        turns, language, limit, call_context=call_context
    )


def generate_call_summary(
    transcript: Iterable[dict[str, Any]],
    *,
    call_context: dict[str, Any] | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Generate one summary from an explicit transcript."""

    limit = max(80, int(max_chars or CALL_SUMMARY_MAX_CHARS))
    turns = normalize_transcript(transcript, max_chars=CALL_SUMMARY_MAX_TRANSCRIPT_CHARS)
    fallback = _fallback_summary(turns, limit)
    if not turns:
        return {"ok": False, "summary": fallback, "source": "fallback", "error": "empty transcript"}
    if not config.DASHSCOPE_API_KEY or not config.ENABLE_CALL_SUMMARY_LLM:
        return {
            "ok": True,
            "summary": fallback,
            "source": "fallback",
            "error": "call-summary LLM unavailable; transcript fallback used",
            "model": None,
        }

    summary_system, summary_user = _summary_messages(
        turns, limit, call_context=_context(call_context)
    )
    payload = {
        "model": config.CALL_SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": summary_system},
            {"role": "user", "content": summary_user},
        ],
        "temperature": config.CALL_SUMMARY_TEMPERATURE,
        "max_tokens": config.CALL_SUMMARY_MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    last_error: str | None = None
    for attempt in range(1, CALL_SUMMARY_MAX_RETRIES + 2):
        try:
            status_code, response, request_bytes, response_bytes = http_post_json(
                config.DASHSCOPE_CHAT_COMPLETIONS_URL,
                payload,
                config.CALL_SUMMARY_TIMEOUT_SECONDS,
                request_kind="generic",
                wait_timeout_seconds=config.DASHSCOPE_WAIT_TIMEOUT_SECONDS,
            )
            content = _normalize_model_summary(_extract_chat_content(response), limit)
            if 200 <= status_code < 300 and content:
                return {
                    "ok": True,
                    "summary": content,
                    "source": "qwen_call_summary",
                    "error": None,
                    "status_code": status_code,
                    "model": response.get("model") or config.CALL_SUMMARY_MODEL,
                    "usage": response.get("usage"),
                    "request_bytes": request_bytes,
                    "response_bytes": response_bytes,
                    "attempts": attempt,
                }
            last_error = f"summary http status={status_code}, empty={not bool(content)}"
            if status_code not in {429, 500, 502, 503, 504}:
                break
        except urllib.error.HTTPError as exc:
            last_error = f"summary http error: {exc.code}"
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except Exception as exc:
            last_error = f"summary request failed: {type(exc).__name__}: {exc}"
        if attempt <= CALL_SUMMARY_MAX_RETRIES:
            time.sleep(min(10.0, 2.0**attempt))
    return {
        "ok": True,
        "summary": fallback,
        "source": "fallback_after_error",
        "error": last_error,
        "model": None,
    }


def generate_call_translation(
    transcript: Iterable[dict[str, Any]],
    *,
    target_language: str,
    call_context: dict[str, Any] | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Translate one explicit transcript while preserving speaker labels."""

    language = str(target_language or "").strip().lower()
    if language not in {"en", "zh"}:
        return {
            "ok": False,
            "translation": "",
            "source": "invalid_target_language",
            "error": f"unsupported target language: {language or 'missing'}",
            "target_language": language or None,
        }
    limit = max(80, int(max_chars or CALL_TRANSLATION_MAX_CHARS))
    turns = normalize_transcript(transcript, max_chars=CALL_TRANSLATION_MAX_TRANSCRIPT_CHARS)
    if not turns:
        return {
            "ok": False,
            "translation": "",
            "source": "empty_transcript",
            "error": "empty transcript",
            "target_language": language,
        }
    if not config.DASHSCOPE_API_KEY or not config.ENABLE_CALL_TRANSLATION_LLM:
        return {
            "ok": False,
            "translation": "",
            "source": "unavailable",
            "error": "call-translation LLM unavailable",
            "target_language": language,
            "model": None,
        }

    system, user = _translation_messages(
        turns, language, limit, call_context=_context(call_context)
    )
    payload = {
        "model": config.CALL_TRANSLATION_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": config.CALL_TRANSLATION_TEMPERATURE,
        "max_tokens": config.CALL_TRANSLATION_MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    last_error: str | None = None
    for attempt in range(1, CALL_TRANSLATION_MAX_RETRIES + 2):
        try:
            status_code, response, request_bytes, response_bytes = http_post_json(
                config.DASHSCOPE_CHAT_COMPLETIONS_URL,
                payload,
                config.CALL_TRANSLATION_TIMEOUT_SECONDS,
                request_kind="generic",
                wait_timeout_seconds=config.DASHSCOPE_WAIT_TIMEOUT_SECONDS,
            )
            content = _normalize_model_translation(_extract_chat_content(response), limit)
            if 200 <= status_code < 300 and content:
                return {
                    "ok": True,
                    "translation": content,
                    "source": "qwen_call_translation",
                    "error": None,
                    "target_language": language,
                    "status_code": status_code,
                    "model": response.get("model") or config.CALL_TRANSLATION_MODEL,
                    "usage": response.get("usage"),
                    "request_bytes": request_bytes,
                    "response_bytes": response_bytes,
                    "attempts": attempt,
                }
            last_error = f"translation http status={status_code}, empty={not bool(content)}"
            if status_code not in {429, 500, 502, 503, 504}:
                break
        except urllib.error.HTTPError as exc:
            last_error = f"translation http error: {exc.code}"
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except Exception as exc:
            last_error = f"translation request failed: {type(exc).__name__}: {exc}"
        if attempt <= CALL_TRANSLATION_MAX_RETRIES:
            time.sleep(min(10.0, 2.0**attempt))
    return {
        "ok": False,
        "translation": "",
        "source": "error",
        "error": last_error,
        "target_language": language,
        "model": None,
    }
