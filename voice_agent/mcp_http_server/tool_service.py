# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/tool_service.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：voice_agent, voice_agent.calls.content_service, voice_agent.calls.derived_repository, voice_agent.calls.repository, voice_agent.calls.resolver, voice_agent.context.runtime, .device_executor, .tool_catalog
# 主要公开符号：RadioToolService
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""YAML 驱动的对讲机工具服务层。"""

from __future__ import annotations

from typing import Any, Dict

from voice_agent import config as agent_config
from voice_agent.calls.content_service import CallContentService
from voice_agent.calls.derived_repository import DerivedContentRepository
from voice_agent.calls.repository import CallStoreRepository
from voice_agent.calls.resolver import CallResolver
from voice_agent.context.runtime import get_default_context_memory_runtime
from voice_agent.messages import UserHistoryService, get_message_archive_service

from .device_executor import DeviceExecutor, build_executor
from .tool_catalog import ToolCatalog



class RadioToolService:
    """工具校验与设备执行边界。支持范围完全由 YAML 决定。"""

    def __init__(
        self,
        catalog: ToolCatalog | None = None,
        executor: DeviceExecutor | None = None,
        content_service: CallContentService | None = None,
        history_service: Any | None = None,
        user_history_service: UserHistoryService | None = None,
    ) -> None:
        self.catalog = catalog or ToolCatalog()
        self.executor = executor or build_executor()
        self._content_service = content_service
        self._history_service = history_service
        self._user_history_service_instance = user_history_service

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any] | None) -> Dict[str, Any]:
        """按 ToolCatalog 的 canonical 契约校验并执行；不再做旧名称/旧枚举兼容转换。"""
        if not self.catalog.contains(tool_name):
            raise ValueError(
                f"{tool_name!r} is not a supported tool. "
                f"Available: {self.catalog.names()}"
            )

        normalized = self.catalog.validate_arguments(tool_name, dict(arguments or {}))
        if tool_name in {"call_query", "call_get_transcript", "call_summarize", "call_translate"}:
            return self._execute_call_content(tool_name, normalized)
        if tool_name in {
            "history_recent_calls", "history_entity_timeline",
            "history_similar_outcomes", "history_open_tasks",
        }:
            return self._execute_history(tool_name, normalized)
        if tool_name in {"history_call_by_position", "history_recent_sms"}:
            return self._execute_user_history(tool_name, normalized)

        spec = self.catalog.get(tool_name)
        if spec.executor_kind == "local":
            raise ValueError(
                f"{tool_name!r} is an agent-local capability and must not be sent to the MCP wire executor"
            )
        result = self.executor.execute(tool_name, normalized, spec)
        self._archive_outbound_sms(tool_name, normalized, result)
        return result

    # ---- 出站短信归档（需求 §2.2/§13） -----------------------------------

    def _archive_outbound_sms(self, tool_name: str, arguments: Dict[str, Any], result: Dict[str, Any]) -> None:
        """中转台主动下发的短信 source 固定为 LOCAL_CONTROL_ID，异步归档。"""
        if tool_name != "sms_send":
            return
        try:
            service = get_message_archive_service()
            if not service.enabled:
                return
            code = result.get("code")
            service.submit_outbound_sms(
                target_id=str(arguments.get("target_id") or ""),
                content=str(arguments.get("content") or ""),
                target_type=str(arguments.get("target_type") or "private"),
                ok=(code is None or int(code) == 0),
            )
        except Exception:  # 归档失败不影响设备执行结果
            import logging

            logging.getLogger(__name__).exception("sms_send 出站归档失败")

    # ---- 用户消息历史（需求 §19-§24） ------------------------------------

    def _user_history(self) -> UserHistoryService:
        if self._user_history_service_instance is None:
            service = get_message_archive_service()
            self._user_history_service_instance = UserHistoryService(
                service.store, service.metrics
            )
        return self._user_history_service_instance

    def _execute_user_history(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        service = self._user_history()
        actor = str(arguments.get("actor_terminal_id") or "").strip()
        if not actor:
            raise PermissionError("actor_terminal_id is required")
        if tool_name == "history_call_by_position":
            return service.call_by_position(
                actor_terminal_id=actor,
                position_from_latest=int(arguments.get("position_from_latest") or 1),
            )
        return service.recent_sms(
            actor_terminal_id=actor,
            limit=int(arguments.get("limit") or 20),
            start_time=str(arguments.get("start_time") or "") or None,
            end_time=str(arguments.get("end_time") or "") or None,
            contact_id=str(arguments.get("contact_id") or "") or None,
            keyword=str(arguments.get("keyword") or "") or None,
            direction=str(arguments.get("direction") or "") or None,
            source_id=str(arguments.get("source_id") or "") or None,
            target_id=str(arguments.get("target_id") or "") or None,
            group_id=str(arguments.get("group_id") or "") or None,
        )


    def _history_query_service(self) -> Any:
        if self._history_service is None:
            self._history_service = get_default_context_memory_runtime().history_service
        return self._history_service

    def _execute_history(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        service = self._history_query_service()
        actor = str(arguments.get("actor_terminal_id") or "").strip()
        if not actor:
            raise PermissionError("actor_terminal_id is required")
        if tool_name == "history_recent_calls":
            return service.recent_calls(
                actor_terminal_id=actor,
                days=int(arguments.get("days") or agent_config.CONTEXT_RECENT_CALL_DAYS),
                limit=int(arguments.get("limit") or 20),
            )
        if tool_name == "history_entity_timeline":
            return service.entity_timeline(
                actor_terminal_id=actor,
                entity_id=str(arguments.get("entity_id") or ""),
                limit=int(arguments.get("limit") or 50),
            )
        if tool_name == "history_similar_outcomes":
            return service.similar_outcomes(
                actor_terminal_id=actor,
                operation_type=str(arguments.get("operation_type") or ""),
                limit=int(arguments.get("limit") or 20),
            )
        return service.open_tasks(
            actor_terminal_id=actor, limit=int(arguments.get("limit") or 20)
        )

    def _call_content_service(self) -> CallContentService:
        if self._content_service is None:
            repository = CallStoreRepository(agent_config.CALL_STORE_PATH)
            derived = DerivedContentRepository(agent_config.CALL_STORE_PATH)
            self._content_service = CallContentService(
                repository=repository,
                derived_repository=derived,
                resolver=CallResolver(
                    repository,
                    default_scope=agent_config.CALL_RESOLVE_DEFAULT_SCOPE,
                    max_candidates=agent_config.CALL_RESOLVE_MAX_CANDIDATES,
                    # 摘要对象=用户间通话（需求 §22）：端点四元组未命中时
                    # 回退归档 user_voice 参与方判定，与 runtime 固定流程
                    # 共用同一授权口径。
                    user_voice_access=self._user_history().call_access,
                ),
            )
        return self._content_service

    def _enforce_call_access(self, actor: str | None, call_id: str) -> None:
        """需求 §22：电话内容工具必须验证 call_id 属于用户可访问的 user_voice。

        - False：call_id 只存在于 control_voice（或 user_voice 中无权）→ 拒绝；
        - None：四库均无记录（CallStore 旧数据），保持兼容放行。
        """
        if not actor or not call_id:
            return
        access = self._user_history().call_access(actor, call_id)
        if access is False:
            raise PermissionError(
                f"call_id {call_id!r} is not accessible for actor {actor!r}"
            )

    def _execute_call_content(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        service = self._call_content_service()
        actor = str(arguments.get("actor_terminal_id") or "").strip() or None
        if tool_name != "call_query":
            self._enforce_call_access(actor, str(arguments.get("call_id") or ""))
        if tool_name == "call_query":
            return service.call_query(dict(arguments["selector"]), actor_terminal_id=actor)
        if tool_name == "call_get_transcript":
            return service.get_transcript(
                call_id=str(arguments["call_id"]), actor_terminal_id=actor
            )
        if tool_name == "call_summarize":
            return service.summarize(
                call_id=str(arguments["call_id"]),
                actor_terminal_id=actor,
                operation_id=str(arguments["operation_id"]),
                options=dict(arguments.get("options") or {}),
            )
        return service.translate(
            call_id=str(arguments["call_id"]),
            actor_terminal_id=actor,
            operation_id=str(arguments["operation_id"]),
            target_language=str(arguments["target_language"]),
            options=dict(arguments.get("options") or {}),
        )

    def mcp_tools(self) -> list[dict[str, Any]]:
        return self.catalog.mcp_tool_definitions()
