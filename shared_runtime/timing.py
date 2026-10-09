from __future__ import annotations

import logging
import time
from typing import Any, Callable, Protocol, TypeVar

from .diagnostics import log_trace

_T = TypeVar("_T")


class TimingPlugin(Protocol):
    """Pluggable observer for one atomic runtime timing measurement."""

    def record(
        self,
        logger: logging.Logger,
        event: str,
        *,
        elapsed_ms: float,
        ok: bool,
        fields: dict[str, Any],
    ) -> None: ...


class LoggingTimingPlugin:
    """Default timing plugin: write structured duration fields to project logs."""

    def record(
        self,
        logger: logging.Logger,
        event: str,
        *,
        elapsed_ms: float,
        ok: bool,
        fields: dict[str, Any],
    ) -> None:
        log_trace(
            logger,
            event,
            elapsed_ms=round(float(elapsed_ms), 3),
            ok=bool(ok),
            **fields,
        )


DEFAULT_TIMING_PLUGIN = LoggingTimingPlugin()


def timed_call(
    logger: logging.Logger,
    event: str,
    func: Callable[..., _T],
    *args: Any,
    timing_plugin: TimingPlugin | None = None,
    fields: dict[str, Any] | None = None,
    **kwargs: Any,
) -> _T:
    """Execute ``func`` unchanged and observe only its elapsed time.

    Exceptions are re-raised untouched after a timing record is emitted, so
    adding observability cannot change business error handling.
    """

    plugin = timing_plugin or DEFAULT_TIMING_PLUGIN
    started = time.perf_counter()
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        plugin.record(
            logger,
            event,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            ok=False,
            fields={
                **dict(fields or {}),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise
    plugin.record(
        logger,
        event,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        ok=True,
        fields=dict(fields or {}),
    )
    return result
