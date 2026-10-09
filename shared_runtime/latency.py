from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .diagnostics import log_trace


def perf_ms(started: float) -> float:
    """Milliseconds elapsed since a ``time.perf_counter`` sample."""

    return round((time.perf_counter() - float(started)) * 1000.0, 3)


def now_ms() -> int:
    return int(time.time() * 1000.0)


def clamp_ms(value: Any, *, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        number = 0.0
    return round(number, 3)


def sum_ms(values: Iterable[Any]) -> float:
    total = 0.0
    for value in values:
        parsed = clamp_ms(value)
        if parsed is not None:
            total += parsed
    return round(total, 3)


def iso_to_epoch_ms(value: Any) -> int | None:
    """Parse an ISO-8601 timestamp used by gateway/agent contracts."""

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000.0)


def wall_elapsed_ms(start_ms: Any, end_ms: Any | None = None) -> float | None:
    try:
        start = float(start_ms)
        end = float(now_ms() if end_ms is None else end_ms)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, end - start), 3)


def iso_elapsed_ms(start_iso: Any, end_ms: Any | None = None) -> float | None:
    start = iso_to_epoch_ms(start_iso)
    if start is None:
        return None
    return wall_elapsed_ms(start, end_ms)


def rounded_tree(value: Any) -> Any:
    """Round floating duration values recursively without changing structure."""

    if isinstance(value, dict):
        return {str(key): rounded_tree(child) for key, child in value.items()}
    if isinstance(value, list):
        return [rounded_tree(child) for child in value]
    if isinstance(value, tuple):
        return [rounded_tree(child) for child in value]
    if isinstance(value, float):
        return round(value, 3)
    return value


def log_latency_tree(
    logger: logging.Logger,
    event: str,
    timing_ms: dict[str, Any],
    **fields: Any,
) -> None:
    """Emit one structured, correlation-friendly latency tree.

    ``timing_json`` is intentionally duplicated as compact JSON because several
    Windows log collectors flatten nested mappings differently.  Keeping both
    forms makes the same record usable in local text logs and structured sinks.
    """

    tree = rounded_tree(timing_ms)
    log_trace(
        logger,
        event,
        timing_ms=tree,
        timing_json=json.dumps(tree, ensure_ascii=False, separators=(",", ":")),
        **fields,
    )
