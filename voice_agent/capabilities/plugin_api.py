from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from voice_agent.operations.models import AtomicOperation


@dataclass(frozen=True)
class LocalCapabilityContext:
    """Execution context shared by atomic local-capability plugins."""

    operation: AtomicOperation
    terminal_id: int | None = None
    channel_id: int | None = None
    request_id: str | int | None = None


class LocalCapabilityPlugin(Protocol):
    """Minimal contract for one deterministic local capability."""

    capability_id: str

    def execute(
        self,
        arguments: dict[str, Any],
        context: LocalCapabilityContext,
    ) -> dict[str, Any]: ...


class LocalCapabilityPluginRegistry:
    """Small explicit registry used by the local executor.

    Registration is deterministic and rejects duplicate capability ids.  This
    keeps the existing capability contract stable while allowing future atomic
    capabilities to be composed by supplying additional plugin instances.
    """

    def __init__(self, plugins: Iterable[LocalCapabilityPlugin] = ()) -> None:
        self._plugins: dict[str, LocalCapabilityPlugin] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: LocalCapabilityPlugin) -> None:
        capability_id = str(getattr(plugin, "capability_id", "") or "").strip()
        if not capability_id:
            raise ValueError("local capability plugin requires capability_id")
        if capability_id in self._plugins:
            raise ValueError(f"duplicate local capability plugin: {capability_id}")
        self._plugins[capability_id] = plugin

    def unregister(self, capability_id: str) -> LocalCapabilityPlugin | None:
        return self._plugins.pop(str(capability_id), None)

    def contains(self, capability_id: str) -> bool:
        return str(capability_id) in self._plugins

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    def execute(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        context: LocalCapabilityContext,
    ) -> dict[str, Any]:
        plugin = self._plugins.get(str(capability_id))
        if plugin is None:
            return {
                "ok": False,
                "error": f"unknown_local_capability:{capability_id}",
                "error_type": "NO_CAPABILITY",
            }
        return plugin.execute(dict(arguments), context)
