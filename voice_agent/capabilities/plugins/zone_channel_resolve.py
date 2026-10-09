from __future__ import annotations

from typing import Any

from voice_agent.capabilities.plugin_api import LocalCapabilityContext
from voice_agent.zone_channel_directory import resolve_zone_channel_matches


class ZoneChannelResolvePlugin:
    capability_id = "radio_resolve_zone_channel"

    def execute(
        self,
        arguments: dict[str, Any],
        context: LocalCapabilityContext,
    ) -> dict[str, Any]:
        del context
        # Planner fields are hints, not authoritative entity types.  Preserve
        # the existing resolution order and ambiguity semantics exactly.
        result: dict[str, Any] = {}
        resolved_ids: dict[str, int] = {}

        for hinted_kind in ("zone", "channel"):
            raw_value = arguments.get(hinted_kind)
            if not raw_value:
                continue

            matches = resolve_zone_channel_matches(raw_value, hinted_kind=hinted_kind)
            if not matches:
                return {
                    "ok": False,
                    "error": f"{hinted_kind}_not_resolved",
                    "error_type": "NEEDS_CLARIFICATION",
                }

            for actual_kind, match in matches.items():
                entity_id = int(match["id"])
                previous_id = resolved_ids.get(actual_kind)
                if previous_id is not None and previous_id != entity_id:
                    return {
                        "ok": False,
                        "error": f"multiple_{actual_kind}_matches",
                        "error_type": "NEEDS_CLARIFICATION",
                    }
                resolved_ids[actual_kind] = entity_id
                result.update(
                    {
                        f"{actual_kind}_id": entity_id,
                        f"{actual_kind}_name": str(match["name"]),
                    }
                )

        return {"ok": True, "result": result}
