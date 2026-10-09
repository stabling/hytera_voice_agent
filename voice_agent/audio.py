# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/audio.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：., .utils
# 主要公开符号：strip_data_url_prefix, decode_base64_payload, detect_payload_type, detect_codec, is_wav_bytes, raw_audio_to_pcm16, pcm16_to_wav_bytes, bytes_to_data_url, pcm16_diagnostics, callback_audio_to_data_url
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Audio decoding, diagnostics, preprocessing and data-URL conversion."""

from __future__ import annotations

import audioop
import base64
import binascii
import hashlib
import io
import json
import mimetypes
import re
import statistics
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .utils import perf_ms


def strip_data_url_prefix(payload_b64: str) -> str:
    payload_b64 = str(payload_b64 or "").strip()
    if payload_b64.startswith("data:") and "," in payload_b64:
        return payload_b64.split(",", 1)[1].strip()
    return payload_b64


def decode_base64_payload(payload_b64: str) -> tuple[bytes | None, str | None]:
    if not payload_b64:
        return None, "empty audio_payload_b64"
    payload_b64 = strip_data_url_prefix(payload_b64)
    payload_b64 = "".join(payload_b64.split())
    try:
        return base64.b64decode(payload_b64, validate=False), None
    except binascii.Error as exc:
        return None, "base64 decode failed: %s" % exc
    except Exception as exc:
        return None, "base64 decode error: %s" % exc


def _candidate_dicts(data: dict) -> list[dict]:
    packet = data.get("packet") if isinstance(data, dict) else None
    packet = packet if isinstance(packet, dict) else {}
    packet_rtp = packet.get("rtp") if isinstance(packet.get("rtp"), dict) else {}
    direct_rtp = data.get("rtp") if isinstance(data.get("rtp"), dict) else {}
    return [data, packet, packet_rtp, direct_rtp]


def detect_payload_type(data: dict) -> int:
    """Read RTP PT from both the top level and nested callback packet metadata."""
    for obj in _candidate_dicts(data or {}):
        for key in ("payload_type", "rtp_payload_type", "first_payload_type", "payload_type_first"):
            value = obj.get(key)
            if value not in (None, ""):
                try:
                    return int(value)
                except Exception:
                    pass
    return 0


def detect_codec(data: dict) -> str:
    """Detect codec without silently losing nested RTP metadata."""
    for obj in _candidate_dicts(data or {}):
        codec = str(obj.get("audio_codec") or obj.get("codec") or "").strip().lower()
        if codec in ("pcmu", "mulaw", "mu-law", "ulaw", "g711u", "g.711u"):
            return "pcmu"
        if codec in ("pcma", "alaw", "a-law", "g711a", "g.711a"):
            return "pcma"
        if codec in ("pcm", "pcm16", "linear16", "s16le", "l16"):
            return "pcm16"
        if codec in ("wav", "wave"):
            return "wav"
        if codec in ("mp3", "m4a", "aac", "flac", "ogg", "opus"):
            return codec

    payload_type = detect_payload_type(data or {})
    if payload_type == 0:
        return "pcmu"
    if payload_type == 8:
        return "pcma"
    # Dynamic RTP PT cannot be inferred from the numeric value alone. Keep a
    # configurable fallback rather than always pretending it is PCMU.
    return str(getattr(config, "DEFAULT_CALLBACK_CODEC", "pcmu") or "pcmu").strip().lower()


def is_wav_bytes(raw: bytes | None) -> bool:
    return bool(raw and raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WAVE")


def _wav_to_pcm16(raw_audio: bytes) -> tuple[bytes | None, int, str | None]:
    try:
        with wave.open(io.BytesIO(raw_audio), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if sample_width != config.DEFAULT_SAMPLE_WIDTH:
            frames = audioop.lin2lin(frames, sample_width, config.DEFAULT_SAMPLE_WIDTH)
        if channels > 1:
            frames = audioop.tomono(frames, config.DEFAULT_SAMPLE_WIDTH, 0.5, 0.5)
        return frames, int(sample_rate), None
    except Exception as exc:
        return None, config.DEFAULT_SAMPLE_RATE, "read wav failed: %s" % exc


def raw_audio_to_pcm16(raw_audio: bytes, codec: str) -> tuple[bytes | None, int, str | None]:
    if not raw_audio:
        return None, config.DEFAULT_SAMPLE_RATE, "empty raw audio"
    if is_wav_bytes(raw_audio) or codec == "wav":
        return _wav_to_pcm16(raw_audio)
    if codec == "pcmu":
        return (
            audioop.ulaw2lin(raw_audio, config.DEFAULT_SAMPLE_WIDTH),
            config.DEFAULT_SAMPLE_RATE,
            None,
        )
    if codec == "pcma":
        return (
            audioop.alaw2lin(raw_audio, config.DEFAULT_SAMPLE_WIDTH),
            config.DEFAULT_SAMPLE_RATE,
            None,
        )
    if codec == "pcm16":
        return raw_audio, config.DEFAULT_SAMPLE_RATE, None
    return None, config.DEFAULT_SAMPLE_RATE, "unsupported raw codec: %s" % codec


def pcm16_to_wav_bytes(pcm16_bytes: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(config.DEFAULT_CHANNELS)
        wf.setsampwidth(config.DEFAULT_SAMPLE_WIDTH)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16_bytes)
    return buf.getvalue()


def bytes_to_data_url(data: bytes, mime_type: str) -> str:
    return "data:%s;base64,%s" % (mime_type, base64.b64encode(data).decode("ascii"))


def _frame_rms_values(pcm16: bytes, sample_rate: int) -> tuple[list[int], int]:
    frame_samples = max(1, int(sample_rate * max(10, config.AUDIO_ANALYSIS_FRAME_MS) / 1000.0))
    frame_bytes = frame_samples * config.DEFAULT_SAMPLE_WIDTH
    values: list[int] = []
    for pos in range(0, len(pcm16), frame_bytes):
        frame = pcm16[pos : pos + frame_bytes]
        if len(frame) < config.DEFAULT_SAMPLE_WIDTH:
            continue
        values.append(audioop.rms(frame, config.DEFAULT_SAMPLE_WIDTH))
    return values, frame_bytes


def pcm16_diagnostics(pcm16: bytes, sample_rate: int) -> dict[str, Any]:
    if not pcm16:
        return {
            "sample_rate": sample_rate,
            "duration_seconds": 0.0,
            "rms": 0,
            "peak": 0,
            "dc_offset": 0,
            "silence_ratio": 1.0,
        }
    frame_rms, _ = _frame_rms_values(pcm16, sample_rate)
    overall_rms = audioop.rms(pcm16, config.DEFAULT_SAMPLE_WIDTH)
    peak = audioop.max(pcm16, config.DEFAULT_SAMPLE_WIDTH)
    dc_offset = audioop.avg(pcm16, config.DEFAULT_SAMPLE_WIDTH)
    if frame_rms:
        ordered = sorted(frame_rms)
        noise_count = max(1, len(ordered) // 5)
        noise_floor = int(statistics.median(ordered[:noise_count]))
    else:
        noise_floor = 0
    silence_threshold = max(
        int(config.AUDIO_SILENCE_MIN_RMS),
        int(noise_floor * config.AUDIO_SILENCE_NOISE_MULTIPLIER),
        int(overall_rms * config.AUDIO_SILENCE_OVERALL_RMS_RATIO),
    )
    silence_ratio = (
        sum(1 for value in frame_rms if value <= silence_threshold) / float(len(frame_rms))
        if frame_rms
        else 1.0
    )
    return {
        "sample_rate": int(sample_rate),
        "duration_seconds": round(len(pcm16) / 2.0 / float(sample_rate), 4),
        "pcm16_bytes": len(pcm16),
        "rms": int(overall_rms),
        "peak": int(peak),
        "peak_dbfs": round(20.0 * __import__("math").log10(max(1, peak) / 32767.0), 2),
        "dc_offset": int(dc_offset),
        "zero_crossings": int(audioop.cross(pcm16, config.DEFAULT_SAMPLE_WIDTH)),
        "frame_count": len(frame_rms),
        "frame_rms_min": min(frame_rms) if frame_rms else 0,
        "frame_rms_median": int(statistics.median(frame_rms)) if frame_rms else 0,
        "frame_rms_max": max(frame_rms) if frame_rms else 0,
        "estimated_noise_floor_rms": int(noise_floor),
        "silence_threshold_rms": int(silence_threshold),
        "silence_ratio": round(silence_ratio, 4),
    }


def _trim_silence(pcm16: bytes, sample_rate: int) -> tuple[bytes, dict[str, Any]]:
    frame_rms, frame_bytes = _frame_rms_values(pcm16, sample_rate)
    if not frame_rms:
        return pcm16, {"trimmed": False, "reason": "no_frames"}
    diag = pcm16_diagnostics(pcm16, sample_rate)
    threshold = int(diag["silence_threshold_rms"])
    active = [idx for idx, value in enumerate(frame_rms) if value > threshold]
    if not active:
        return pcm16, {"trimmed": False, "reason": "no_active_frame", "threshold_rms": threshold}

    pad_frames = max(
        0, int(round(config.AUDIO_TRIM_PADDING_MS / float(config.AUDIO_ANALYSIS_FRAME_MS)))
    )
    start_frame = max(0, active[0] - pad_frames)
    end_frame = min(len(frame_rms), active[-1] + pad_frames + 1)
    start_byte = start_frame * frame_bytes
    end_byte = min(len(pcm16), end_frame * frame_bytes)
    trimmed = pcm16[start_byte:end_byte]
    min_keep_bytes = int(config.AUDIO_MIN_KEEP_SECONDS * sample_rate) * config.DEFAULT_SAMPLE_WIDTH
    if len(trimmed) < min_keep_bytes:
        return pcm16, {
            "trimmed": False,
            "reason": "trim_result_too_short",
            "threshold_rms": threshold,
            "candidate_seconds": round(len(trimmed) / 2.0 / sample_rate, 4),
        }
    return trimmed, {
        "trimmed": start_byte > 0 or end_byte < len(pcm16),
        "threshold_rms": threshold,
        "start_seconds": round(start_byte / 2.0 / sample_rate, 4),
        "end_seconds": round(end_byte / 2.0 / sample_rate, 4),
        "removed_leading_seconds": round(start_byte / 2.0 / sample_rate, 4),
        "removed_trailing_seconds": round((len(pcm16) - end_byte) / 2.0 / sample_rate, 4),
    }


def _normalize_pcm16(pcm16: bytes) -> tuple[bytes, dict[str, Any]]:
    if not pcm16:
        return pcm16, {"gain": 1.0, "dc_removed": 0}
    dc = audioop.avg(pcm16, config.DEFAULT_SAMPLE_WIDTH)
    centered = audioop.bias(pcm16, config.DEFAULT_SAMPLE_WIDTH, -dc) if dc else pcm16
    peak = audioop.max(centered, config.DEFAULT_SAMPLE_WIDTH)
    rms = audioop.rms(centered, config.DEFAULT_SAMPLE_WIDTH)
    gains: list[float] = [1.0]
    if peak > 0:
        gains.append(config.AUDIO_TARGET_PEAK / float(peak))
    if rms > 0:
        gains.append(config.AUDIO_TARGET_RMS / float(rms))
    # Use the smaller requested gain so neither the peak nor RMS target is exceeded.
    gain = max(config.AUDIO_MIN_GAIN, min(config.AUDIO_MAX_GAIN, min(gains[1:] or gains)))
    normalized = (
        audioop.mul(centered, config.DEFAULT_SAMPLE_WIDTH, gain)
        if abs(gain - 1.0) > 0.01
        else centered
    )
    return normalized, {
        "dc_removed": int(dc),
        "input_peak": int(peak),
        "input_rms": int(rms),
        "gain": round(gain, 4),
        "output_peak": int(audioop.max(normalized, config.DEFAULT_SAMPLE_WIDTH))
        if normalized
        else 0,
        "output_rms": int(audioop.rms(normalized, config.DEFAULT_SAMPLE_WIDTH))
        if normalized
        else 0,
    }


def _resample_pcm16(pcm16: bytes, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate:
        return pcm16
    converted, _state = audioop.ratecv(
        pcm16,
        config.DEFAULT_SAMPLE_WIDTH,
        config.DEFAULT_CHANNELS,
        int(source_rate),
        int(target_rate),
        None,
    )
    return converted


def _safe_name(value: str | None) -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "audio"))
    return text[:100] or "audio"


def _debug_dir(debug_id: str | None) -> Path:
    day = datetime.now().strftime("%Y%m%d")
    return Path(config.AUDIO_DEBUG_DIR) / day / _safe_name(debug_id)


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.endswith("_b64") or key_text in {
                "audio_b64",
                "udp_payload_b64",
                "audio_payload_b64",
            }:
                cleaned[key_text] = "<omitted base64>"
            else:
                cleaned[key_text] = _sanitize_metadata(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value[:200]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "<truncated>"
    return value


def _write_debug_file(path: Path, data: bytes | str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return str(path.resolve())


def _save_debug_bundle(
    *,
    debug_id: str | None,
    codec: str,
    raw_audio: bytes,
    decoded_pcm16: bytes | None,
    decoded_rate: int | None,
    processed_pcm16: bytes | None,
    processed_rate: int | None,
    metadata: dict[str, Any],
) -> dict[str, str]:
    if not config.AUDIO_DEBUG_SAVE:
        return {}
    root = _debug_dir(debug_id)
    ext = {"pcmu": "ulaw", "pcma": "alaw", "pcm16": "pcm"}.get(codec, codec or "bin")
    files: dict[str, str] = {}
    files["raw"] = _write_debug_file(root / ("00_callback_raw.%s" % ext), raw_audio)
    if decoded_pcm16 is not None and decoded_rate:
        files["decoded_wav"] = _write_debug_file(
            root / ("01_decoded_%dhz.wav" % decoded_rate),
            pcm16_to_wav_bytes(decoded_pcm16, decoded_rate),
        )
    if processed_pcm16 is not None and processed_rate:
        files["asr_wav"] = _write_debug_file(
            root / ("02_asr_input_%dhz.wav" % processed_rate),
            pcm16_to_wav_bytes(processed_pcm16, processed_rate),
        )
    files["diagnostics"] = _write_debug_file(
        root / "diagnostics.json",
        json.dumps(metadata, ensure_ascii=False, indent=2),
    )
    return files


def callback_audio_to_data_url(
    data: dict, *, debug_id: str | None = None
) -> tuple[str | None, dict]:
    timing: dict[str, Any] = {
        "base64_decode_ms": 0.0, "audio_decode_ms": 0.0, "trim_ms": 0.0,
        "normalize_ms": 0.0, "resample_ms": 0.0, "wav_build_ms": 0.0,
        "data_url_encode_ms": 0.0, "debug_bundle_write_ms": 0.0, "convert_total_ms": 0.0,
    }
    t_all = time.perf_counter()
    audio_payload_b64 = data.get("audio_payload_b64") or data.get("audio_b64")
    t0 = time.perf_counter()
    raw_audio, err = decode_base64_payload(audio_payload_b64)
    timing["base64_decode_ms"] = perf_ms(t0)
    if err:
        timing["convert_total_ms"] = perf_ms(t_all)
        return None, {"ok": False, "error": err, "timing": timing, "audio_info": {}}
    assert raw_audio is not None
    codec = detect_codec(data)
    return raw_bytes_to_data_url(
        raw_audio,
        codec=codec,
        source_name="callback",
        timing=timing,
        t_all=t_all,
        debug_id=debug_id,
        callback_metadata=data,
    )


def raw_bytes_to_data_url(
    raw_audio: bytes,
    codec: str,
    source_name: str = "audio",
    timing: dict | None = None,
    t_all: float | None = None,
    *,
    debug_id: str | None = None,
    callback_metadata: dict | None = None,
) -> tuple[str | None, dict]:
    timing = timing or {}
    for key in ("base64_decode_ms", "audio_decode_ms", "trim_ms", "normalize_ms", "resample_ms",
                "wav_build_ms", "data_url_encode_ms", "debug_bundle_write_ms", "convert_total_ms"):
        timing.setdefault(key, 0.0)
    t_all = t_all or time.perf_counter()
    codec = (codec or "wav").strip().lower()

    passthrough_mime = {
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
    }

    if codec in passthrough_mime and not is_wav_bytes(raw_audio):
        encode_started = time.perf_counter()
        data_url = bytes_to_data_url(raw_audio, passthrough_mime[codec])
        timing["data_url_encode_ms"] = perf_ms(encode_started)
        audio_info = {
            "input_codec": codec,
            "raw_bytes": len(raw_audio),
            "raw_sha256": hashlib.sha256(raw_audio).hexdigest(),
            "source": source_name,
            "passthrough": True,
        }
        debug_meta = {
            "audio_info": audio_info,
            "callback_metadata": _sanitize_metadata(callback_metadata or {}),
            "reason": "compressed passthrough; no stdlib decoder available",
        }
        debug_started = time.perf_counter()
        audio_info["debug_files"] = _save_debug_bundle(
            debug_id=debug_id,
            codec=codec,
            raw_audio=raw_audio,
            decoded_pcm16=None,
            decoded_rate=None,
            processed_pcm16=None,
            processed_rate=None,
            metadata=debug_meta,
        )
        timing["debug_bundle_write_ms"] = perf_ms(debug_started)
    else:
        t1 = time.perf_counter()
        pcm16, source_rate, pcm_err = raw_audio_to_pcm16(raw_audio, codec)
        timing["audio_decode_ms"] = perf_ms(t1)
        if pcm_err:
            timing["convert_total_ms"] = perf_ms(t_all)
            return None, {
                "ok": False,
                "error": pcm_err,
                "timing": timing,
                "audio_info": {
                    "input_codec": codec,
                    "raw_bytes": len(raw_audio),
                    "source": source_name,
                },
            }
        assert pcm16 is not None
        decoded_pcm16 = pcm16
        decoded_diag = pcm16_diagnostics(decoded_pcm16, source_rate)

        trim_info: dict[str, Any] = {"trimmed": False, "reason": "preprocess_disabled"}
        normalize_info: dict[str, Any] = {"gain": 1.0, "reason": "preprocess_disabled"}
        processed = decoded_pcm16
        if config.AUDIO_PREPROCESS_ENABLE:
            trim_started = time.perf_counter()
            processed, trim_info = _trim_silence(processed, source_rate)
            timing["trim_ms"] = perf_ms(trim_started)
            normalize_started = time.perf_counter()
            processed, normalize_info = _normalize_pcm16(processed)
            timing["normalize_ms"] = perf_ms(normalize_started)

        target_rate = int(config.ASR_TARGET_SAMPLE_RATE or source_rate)
        t2 = time.perf_counter()
        processed = _resample_pcm16(processed, source_rate, target_rate)
        timing["resample_ms"] = perf_ms(t2)
        processed_diag = pcm16_diagnostics(processed, target_rate)

        duration_seconds = float(processed_diag["duration_seconds"])
        if config.MIN_AUDIO_SECONDS > 0 and duration_seconds < config.MIN_AUDIO_SECONDS:
            timing["convert_total_ms"] = perf_ms(t_all)
            return None, {
                "ok": False,
                "error": "audio too short: %.3fs" % duration_seconds,
                "timing": timing,
                "audio_info": {
                    "input_codec": codec,
                    "raw_bytes": len(raw_audio),
                    "duration_seconds": round(duration_seconds, 4),
                    "sample_rate": target_rate,
                    "source": source_name,
                    "decoded_diagnostics": decoded_diag,
                    "trim": trim_info,
                },
            }

        t3 = time.perf_counter()
        wav_bytes = pcm16_to_wav_bytes(processed, target_rate)
        timing["wav_build_ms"] = perf_ms(t3)
        encode_started = time.perf_counter()
        data_url = bytes_to_data_url(wav_bytes, "audio/wav")
        timing["data_url_encode_ms"] = perf_ms(encode_started)

        audio_info = {
            "input_codec": codec,
            "payload_type": detect_payload_type(callback_metadata or {}),
            "raw_bytes": len(raw_audio),
            "raw_sha256": hashlib.sha256(raw_audio).hexdigest(),
            "decoded_pcm16_bytes": len(decoded_pcm16),
            "source_sample_rate": source_rate,
            "asr_pcm16_bytes": len(processed),
            "asr_sample_rate": target_rate,
            "wav_bytes": len(wav_bytes),
            "duration_seconds": round(duration_seconds, 4),
            "source": source_name,
            "decoded_diagnostics": decoded_diag,
            "trim": trim_info,
            "normalize": normalize_info,
            "asr_input_diagnostics": processed_diag,
        }
        debug_meta = {
            "debug_id": debug_id,
            "audio_info": audio_info,
            "callback_metadata": _sanitize_metadata(callback_metadata or {}),
            "interpretation": {
                "high_silence_ratio": decoded_diag.get("silence_ratio", 0) >= 0.55,
                "very_low_level": decoded_diag.get("peak", 0) < 5000,
                "possible_wrong_codec": decoded_diag.get("peak", 0) >= 32000
                and decoded_diag.get("silence_ratio", 0) < 0.05,
            },
        }
        debug_started = time.perf_counter()
        audio_info["debug_files"] = _save_debug_bundle(
            debug_id=debug_id,
            codec=codec,
            raw_audio=raw_audio,
            decoded_pcm16=decoded_pcm16,
            decoded_rate=source_rate,
            processed_pcm16=processed,
            processed_rate=target_rate,
            metadata=debug_meta,
        )
        timing["debug_bundle_write_ms"] = perf_ms(debug_started)

    timing["convert_total_ms"] = perf_ms(t_all)
    data_url_bytes = len(data_url.encode("utf-8"))
    audio_info["data_url_bytes"] = data_url_bytes
    if data_url_bytes > config.MAX_DATA_URL_BYTES:
        return None, {
            "ok": False,
            "error": "data url too large: %d bytes" % data_url_bytes,
            "timing": timing,
            "audio_info": audio_info,
        }
    return data_url, {"ok": True, "error": None, "timing": timing, "audio_info": audio_info}


def audio_file_to_data_url(
    audio_path: str | Path, codec: str | None = None
) -> tuple[str | None, dict]:
    p = Path(audio_path)
    if not p.exists():
        return None, {
            "ok": False,
            "error": "audio file not found: %s" % p,
            "timing": {},
            "audio_info": {},
        }
    raw = p.read_bytes()
    ext = p.suffix.lower().lstrip(".")
    if codec is None:
        codec = "wav" if is_wav_bytes(raw) else ext
        if ext in ("ulaw", "pcmu"):
            codec = "pcmu"
        elif ext in ("alaw", "pcma"):
            codec = "pcma"
        elif ext in ("pcm", "raw"):
            codec = "pcm16"
    if codec not in ("wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "pcmu", "pcma", "pcm16"):
        guessed = mimetypes.guess_type(str(p))[0]
        if guessed and guessed.startswith("audio/"):
            return bytes_to_data_url(raw, guessed), {
                "ok": True,
                "error": None,
                "timing": {"convert_total_ms": 0.0},
                "audio_info": {
                    "input_codec": ext or guessed,
                    "raw_bytes": len(raw),
                    "source": str(p),
                    "passthrough": True,
                },
            }
    return raw_bytes_to_data_url(raw, codec=codec or "wav", source_name=str(p), debug_id=p.stem)
