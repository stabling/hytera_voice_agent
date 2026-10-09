# =============================================================================
# 【维护注释】文件：voice_agent/capabilities/executor.py
# 所属层次：能力注册与执行门面
# 主要职责：本地原子能力插件的兼容执行门面；旧 LocalCapabilityExecutor API 保持不变。
# 项目内直接依赖：voice_agent.capabilities.plugin_api, voice_agent.capabilities.plugins
# 主要公开符号：LocalCapabilityExecutor
# 兼容性：保留原构造函数、execute 以及时间解析私有方法；能力实现拆到独立插件模块。
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from voice_agent.operations.models import AtomicOperation

from .plugin_api import (
    LocalCapabilityContext,
    LocalCapabilityPlugin,
    LocalCapabilityPluginRegistry,
)
from .plugins import (
    ContactResolvePlugin,
    ReminderSchedulePlugin,
    ZoneChannelResolvePlugin,
)


class LocalCapabilityExecutor:
    """Compatibility facade over atomic local-capability plugins.

    Existing callers still construct and invoke this class exactly as before.
    Each capability now lives in an independent plugin module, and additional
    plugins can be registered without editing this dispatcher.
    """

    # Compatibility aliases retained for callers/tests that used the original
    # deterministic reminder parsing helpers directly.
    _CHINESE_DIGITS = ReminderSchedulePlugin._CHINESE_DIGITS
    _WEEKDAYS = ReminderSchedulePlugin._WEEKDAYS

    def __init__(
        self,
        reminder_scheduler: Any | None = None,
        *,
        plugins: Iterable[LocalCapabilityPlugin] = (),
    ) -> None:
        self.reminder_scheduler = reminder_scheduler
        self.plugin_registry = LocalCapabilityPluginRegistry(
            (
                ContactResolvePlugin(),
                ZoneChannelResolvePlugin(),
                ReminderSchedulePlugin(reminder_scheduler),
                *tuple(plugins),
            )
        )

    def register_plugin(self, plugin: LocalCapabilityPlugin) -> None:
        """Register one extra atomic capability plugin at runtime."""

        self.plugin_registry.register(plugin)

    def unregister_plugin(self, capability_id: str) -> LocalCapabilityPlugin | None:
        """Remove one plugin; primarily useful for composition tests/adapters."""

        return self.plugin_registry.unregister(capability_id)

    @classmethod
    def _parse_number(cls, raw: str | None) -> int | None:
        return ReminderSchedulePlugin.parse_number(raw)

    @staticmethod
    def _ceil_to_minute(value: datetime) -> datetime:
        return ReminderSchedulePlugin.ceil_to_minute(value)

    @classmethod
    def _extract_clock(cls, text: str) -> tuple[int, int] | None:
        return ReminderSchedulePlugin.extract_clock(text)

    @classmethod
    def _time_desc_to_target_datetime(
        cls,
        time_desc: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        return ReminderSchedulePlugin.time_desc_to_target_datetime(time_desc, now=now)

    def execute(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        operation: AtomicOperation,
        *,
        terminal_id: int | None = None,
        channel_id: int | None = None,
        request_id: str | int | None = None,
    ) -> dict[str, Any]:
        context = LocalCapabilityContext(
            operation=operation,
            terminal_id=terminal_id,
            channel_id=channel_id,
            request_id=request_id,
        )
        return self.plugin_registry.execute(capability_id, dict(arguments), context)
