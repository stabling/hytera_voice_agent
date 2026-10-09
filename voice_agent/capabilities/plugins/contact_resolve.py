from __future__ import annotations

from typing import Any

from voice_agent.contact_directory import resolve_contact
from voice_agent.capabilities.plugin_api import LocalCapabilityContext


class ContactResolvePlugin:
    capability_id = "contact_resolve"

    def execute(
        self,
        arguments: dict[str, Any],
        context: LocalCapabilityContext,
    ) -> dict[str, Any]:
        del context
        item = resolve_contact(arguments.get("name"), arguments.get("contact_type"))
        if item is None:
            return {
                "ok": False,
                "error": "contact_not_resolved",
                "error_type": "NEEDS_CLARIFICATION",
            }
        return {
            "ok": True,
            "result": {
                "contact_id": str(item["callee_id"]),
                "target_id": str(item["callee_id"]),
                "target_type": "group" if item.get("type") == "group" else "private",
                "contact_type": str(item.get("type") or "private"),
                "name": str(item.get("name") or ""),
            },
        }
