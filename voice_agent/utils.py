# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/utils.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.
# 主要公开符号：now_ms, now_iso, perf_ms, ensure_dirs, new_task_id, inc_stat, update_stats, get_stats, append_latest_result, get_latest_results
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Small shared utilities."""

from __future__ import annotations

import itertools
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import config

logger = logging.getLogger(__name__)

TASK_COUNTER = itertools.count(1)
FILE_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
RESULT_LOCK = threading.Lock()
LOG_QUEUE: "queue.Queue[tuple[str, dict] | None]" = queue.Queue(maxsize=config.LOG_QUEUE_SIZE)
LATEST_RESULTS: List[dict] = []

STATS: Dict[str, Any] = {
    "service_started_at": None,
    "received_total": 0,
    "queued_total": 0,
    "dropped_total": 0,
    "processed_total": 0,
    "success_total": 0,
    "failed_total": 0,
    "log_dropped_total": 0,
    "last_ack": None,
    "last_result": None,
    "last_error": None,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def perf_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 3)


def ensure_dirs() -> None:
    config.SAVE_DIR.mkdir(parents=True, exist_ok=True)


def new_task_id() -> str:
    return "task_%s_%06d" % (now_ms(), next(TASK_COUNTER))


def inc_stat(key: str, delta: int = 1) -> None:
    with STATS_LOCK:
        STATS[key] = int(STATS.get(key, 0)) + delta


def update_stats(**kwargs: Any) -> None:
    with STATS_LOCK:
        STATS.update(kwargs)


def get_stats(extra: dict | None = None) -> dict:
    with STATS_LOCK:
        data = dict(STATS)
    if extra:
        data.update(extra)
    return data


def append_latest_result(result: dict) -> None:
    with RESULT_LOCK:
        LATEST_RESULTS.append(result)
        if len(LATEST_RESULTS) > config.MAX_LATEST_RESULTS:
            del LATEST_RESULTS[: -config.MAX_LATEST_RESULTS]


def get_latest_results() -> list:
    with RESULT_LOCK:
        return list(LATEST_RESULTS)


def write_jsonl_sync(path: str | Path, obj: dict) -> None:
    ensure_dirs()
    with FILE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def write_jsonl_async(path: str | Path, obj: dict) -> bool:
    try:
        LOG_QUEUE.put_nowait((str(path), obj))
        return True
    except queue.Full:
        inc_stat("log_dropped_total", 1)
        update_stats(last_error="log queue full")
        return False


def log_worker() -> None:
    logger.info("JSONL 日志写线程已启动")
    while True:
        item = LOG_QUEUE.get()
        try:
            if item is None:
                return
            path, obj = item
            write_jsonl_sync(path, obj)
        except Exception:
            logger.exception("写入 JSONL 失败")
            update_stats(last_error="write jsonl failed")
        finally:
            LOG_QUEUE.task_done()


def dedupe_keep_order(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        s = str(item or "").strip()
        if s and s not in seen:
            out.append(s)
            seen.add(s)
    return out


def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
