# =============================================================================
# 【维护注释】文件：shared_runtime/event_publisher.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：shared_contracts.event_models, shared_contracts.event_publisher, .event_journal
# 主要公开符号：ExternalEventStore, EventPublisher, build_event_publisher
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Protocol

from shared_contracts.event_models import DomainEvent, EventQuery
from shared_contracts.event_publisher import PublishResult

from .event_journal import EventJournal

logger = logging.getLogger(__name__)


class ExternalEventStore(Protocol):
    def append(self, event: DomainEvent) -> Any: ...
    def get(self, event_id: str, *, scope: Any) -> dict[str, Any] | None: ...
    def query(self, query: EventQuery | dict[str, Any], *, scope: Any) -> list[dict[str, Any]]: ...


class EventPublisher:
    def __init__(
        self,
        *,
        source_service: str,
        journal_path: str | Path,
        mode: str = "false",
        fail_mode: str = "open",
        external_store: ExternalEventStore | None = None,
        external_primary: bool = False,
        external_fail_open: bool = True,
    ) -> None:
        self.source_service = str(source_service)
        normalized_mode = str(mode or "false").strip().lower()
        self.mode = normalized_mode if normalized_mode in {"false", "shadow", "true"} else "false"
        normalized_fail = str(fail_mode or "open").strip().lower()
        self.fail_mode = normalized_fail if normalized_fail in {"open", "closed"} else "open"
        self.journal_path = Path(journal_path).expanduser().resolve(strict=False)
        self.external_store = external_store
        self.external_primary = bool(external_primary and external_store is not None)
        self.external_fail_open = bool(external_fail_open)
        self._journal: EventJournal | None = None

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "true"}

    @property
    def journal(self) -> EventJournal:
        if self._journal is None:
            self._journal = EventJournal(self.journal_path)
        return self._journal

    def publish(self, event: DomainEvent | dict[str, Any]) -> PublishResult:
        domain_event = event if isinstance(event, DomainEvent) else DomainEvent.model_validate(event)
        if not self.enabled:
            return PublishResult(event_id=domain_event.event_id, published=False, disabled=True)
        try:
            result = self.journal.append(domain_event)
            if result.inserted and self.external_store is not None:
                try:
                    self.external_store.append(domain_event)
                except Exception:
                    logger.exception("external_event_write_failed event_id=%s", domain_event.event_id)
                    if self.external_primary and not self.external_fail_open:
                        raise
            return PublishResult(
                event_id=result.event_id,
                published=result.inserted,
                duplicate=result.duplicate,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "event_journal_write_failed source_service=%s event_type=%s event_id=%s error=%s",
                self.source_service,
                domain_event.event_type,
                domain_event.event_id,
                message,
            )
            if self.fail_mode == "closed":
                raise
            return PublishResult(event_id=domain_event.event_id, published=False, error=message)

    def get(self, event_id: str, *, scope: Any | None = None) -> dict[str, Any] | None:
        if self.external_primary:
            if scope is None:
                raise PermissionError("external event query requires explicit scope")
            return self.external_store.get(event_id, scope=scope)  # type: ignore[union-attr]
        if not self.journal_path.exists() and not self.enabled:
            return None
        return self.journal.get(event_id)

    def query(self, query: EventQuery | dict[str, Any], *, scope: Any | None = None) -> list[dict[str, Any]]:
        if self.external_primary:
            if scope is None:
                raise PermissionError("external event query requires explicit scope")
            return self.external_store.query(query, scope=scope)  # type: ignore[union-attr]
        if not self.journal_path.exists() and not self.enabled:
            return []
        return self.journal.query(query)

    def read_after_offset(self, offset: int, *, limit: int = 200) -> list[dict[str, Any]]:
        if not self.journal_path.exists() and not self.enabled:
            return []
        return self.journal.read_after_offset(offset, limit=limit)

    def stats(self) -> dict[str, Any]:
        if not self.journal_path.exists() and not self.enabled:
            return {
                "path": str(self.journal_path),
                "total": 0,
                "max_offset": 0,
                "enabled": False,
                "mode": self.mode,
            }
        return {**self.journal.stats(), "enabled": self.enabled, "mode": self.mode}


def build_event_publisher(
    *,
    source_service: str,
    default_path: str | Path = "data/state.sqlite3",
    external_store: ExternalEventStore | None = None,
    external_primary: bool = False,
    external_fail_open: bool = True,
) -> EventPublisher:
    return EventPublisher(
        source_service=source_service,
        journal_path=os.environ.get("EVENT_JOURNAL_PATH", str(default_path)),
        mode=os.environ.get("EVENT_JOURNAL_ENABLE", "false"),
        fail_mode=os.environ.get("EVENT_JOURNAL_FAIL_MODE", "open"),
        external_store=external_store,
        external_primary=external_primary,
        external_fail_open=external_fail_open,
    )
