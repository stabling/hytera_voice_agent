# =============================================================================
# 【维护注释】文件：shared_runtime/observability.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：TelemetryState, TraceCorrelationFilter, sanitize_baggage, initialize_observability, trace_headers, traced_http_handler, install_asgi_observability
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Optional OpenTelemetry bootstrap with safe cross-process propagation.

Business facts stay in the domain repositories. Telemetry is diagnostic only and
never carries credentials, audio, transcript content, or arbitrary user baggage.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)
_SENSITIVE = re.compile(
    r"(authorization|cookie|password|secret|token|api[_-]?key|audio|transcript|content)",
    re.I,
)


@dataclass(frozen=True)
class TelemetryState:
    enabled: bool
    service_name: str
    reason: str = ""


class TraceCorrelationFilter(logging.Filter):
    """Attach active OTel IDs to LogRecord without exposing baggage."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.otelTraceID = ""
        record.otelSpanID = ""
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if getattr(ctx, "is_valid", False):
                record.otelTraceID = f"{int(ctx.trace_id):032x}"
                record.otelSpanID = f"{int(ctx.span_id):016x}"
        except Exception:
            pass
        return True


def _enabled() -> bool:
    return os.environ.get("OTEL_ENABLE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def sanitize_baggage(values: Mapping[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in dict(values or {}).items():
        if _SENSITIVE.search(str(key)):
            continue
        text = str(value)
        if len(text) > 256:
            text = text[:256]
        out[str(key)] = text
    return out


def _install_log_correlation() -> None:
    root = logging.getLogger()
    marker = "_hytera_otel_trace_filter"
    if getattr(root, marker, False):
        return
    trace_filter = TraceCorrelationFilter()
    for handler in root.handlers:
        handler.addFilter(trace_filter)
    setattr(root, marker, True)


def initialize_observability(service_name: str) -> TelemetryState:
    if not _enabled():
        return TelemetryState(False, str(service_name), "disabled")
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover - deployment dependency boundary
        logger.warning("otel_disabled_missing_dependency service=%s error=%s", service_name, exc)
        return TelemetryState(False, str(service_name), "missing dependency")

    resource = Resource.create({"service.name": str(service_name)})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(meter_provider)
    _install_log_correlation()

    # Log export is optional even when traces/metrics are enabled. Some deployments
    # deliberately keep logs local and correlate by the injected trace/span fields.
    if os.environ.get("OTEL_LOG_EXPORT_ENABLE", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }:
        try:  # pragma: no cover - optional deployment path
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

            log_provider = LoggerProvider(resource=resource)
            log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
            logging.getLogger().addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=log_provider))
        except Exception as exc:
            logger.warning("otel_log_export_disabled service=%s error=%s", service_name, exc)
    return TelemetryState(True, str(service_name), "initialized")


def trace_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """Inject W3C trace context for internal service calls, never baggage."""
    carrier = dict(headers or {})
    if not _enabled():
        return carrier
    try:
        from opentelemetry.propagate import inject

        inject(carrier)
    except Exception:
        logger.exception("otel_context_inject_failed")
    for key in list(carrier):
        if str(key).lower() == "baggage":
            carrier.pop(key, None)
    return carrier


def _record_http_metric(span_name: str, method: str, duration_ms: float, status_code: int | None) -> None:
    if not _enabled():
        return
    try:
        from opentelemetry import metrics

        meter = metrics.get_meter("hytera.http")
        attrs = {"http.request.method": method, "http.route": span_name}
        if status_code is not None:
            attrs["http.response.status_code"] = int(status_code)
        meter.create_counter("hytera.http.server.requests").add(1, attrs)
        meter.create_histogram("hytera.http.server.duration", unit="ms").record(duration_ms, attrs)
    except Exception:
        logger.exception("otel_http_metric_failed span=%s", span_name)


def traced_http_handler(span_name: str):
    """Decorator for BaseHTTPRequestHandler methods; safe no-op without OTel."""

    def decorate(fn):
        def wrapped(self, *args, **kwargs):
            if not _enabled():
                return fn(self, *args, **kwargs)
            started = time.perf_counter()
            try:
                from opentelemetry import trace
                from opentelemetry.propagate import extract

                carrier = {
                    str(k): str(v)
                    for k, v in self.headers.items()
                    if str(k).lower() != "baggage"
                }
                ctx = extract(carrier)
                tracer = trace.get_tracer("hytera.http")
                with tracer.start_as_current_span(span_name, context=ctx) as span:
                    span.set_attribute("http.request.method", str(getattr(self, "command", "")))
                    span.set_attribute("url.path", str(getattr(self, "path", "")).split("?", 1)[0])
                    return fn(self, *args, **kwargs)
            except Exception:
                logger.exception("otel_http_span_failed span=%s", span_name)
                return fn(self, *args, **kwargs)
            finally:
                _record_http_metric(
                    span_name,
                    str(getattr(self, "command", "")),
                    (time.perf_counter() - started) * 1000.0,
                    None,
                )

        wrapped.__name__ = getattr(fn, "__name__", "wrapped")
        wrapped.__doc__ = getattr(fn, "__doc__", None)
        return wrapped

    return decorate


def install_asgi_observability(app: Any, service_name: str) -> Any:
    """Install minimal ASGI spans/metrics without instrumentation packages."""
    if not _enabled():
        return app
    try:
        from opentelemetry import trace
        from opentelemetry.propagate import extract
    except ImportError:
        return app

    @app.middleware("http")
    async def _otel_http(request, call_next):
        started = time.perf_counter()
        carrier = {k: v for k, v in request.headers.items() if k.lower() != "baggage"}
        ctx = extract(carrier)
        tracer = trace.get_tracer(str(service_name))
        response = None
        try:
            with tracer.start_as_current_span(
                f"{request.method} {request.url.path}", context=ctx
            ) as span:
                span.set_attribute("http.request.method", request.method)
                span.set_attribute("url.path", request.url.path)
                response = await call_next(request)
                span.set_attribute("http.response.status_code", response.status_code)
                return response
        finally:
            _record_http_metric(
                f"{request.method} {request.url.path}",
                request.method,
                (time.perf_counter() - started) * 1000.0,
                None if response is None else response.status_code,
            )

    return app
