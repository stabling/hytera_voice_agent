# =============================================================================
# 【维护注释】文件：voice_agent/agent/framework.py
# 所属层次：LangGraph 编排层
# 主要职责：负责状态图、节点、依赖绑定、检查点和工作流路由；业务能力通过注册表和 runtime 注入，不直接硬编码设备协议。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：require_official_langgraph
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""LangGraph import boundary.

Production deployments of the 64-bit agent must install the official LangGraph
runtime.  A minimal compatibility implementation remains only so source-level
unit tests can run in development environments where optional agent dependencies
are not installed.  The production launcher rejects this fallback.
"""

from __future__ import annotations

import struct
import sys
from typing import Any, Callable

IS_WINDOWS_X86 = sys.platform == "win32" and struct.calcsize("P") == 4
LANGGRAPH_AVAILABLE = False

try:  # pragma: no cover - exercised in the dedicated x64 runtime
    from langchain_core.runnables import RunnableLambda as RunnableLambda
    from langgraph.graph import END as END
    from langgraph.graph import START as START
    from langgraph.graph import StateGraph as StateGraph
    from langgraph.types import Command as Command
    from langgraph.types import interrupt as interrupt

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback is covered by unit tests
    pass

if LANGGRAPH_AVAILABLE:
    FRAMEWORK_BACKEND = "langgraph"
else:
    FRAMEWORK_BACKEND = "test-compat"
    START = "__start__"
    END = "__end__"

    class Command:
        def __init__(self, *, resume: Any = None) -> None:
            self.resume = resume

    def interrupt(_value: Any) -> Any:
        raise RuntimeError("LangGraph interrupt requires the official LangGraph runtime")

    class RunnableLambda:
        """Minimal ``RunnableLambda`` subset used by source-level tests."""

        def __init__(self, func: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
            self._func = func

        def invoke(self, value: dict[str, Any]) -> dict[str, Any]:
            return self._func(value)

        def __call__(self, value: dict[str, Any]) -> dict[str, Any]:
            return self._func(value)

    class _CompiledGraph:
        def __init__(
            self,
            nodes: dict[str, Any],
            edges: dict[str, str],
            conditional_edges: dict[str, tuple[Callable[[dict[str, Any]], str], dict[str, str]]],
        ) -> None:
            self._nodes = nodes
            self._edges = edges
            self._conditional_edges = conditional_edges

        def invoke(self, input_state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
            state = dict(input_state)
            current = self._edges[START]
            while current != END:
                node = self._nodes[current]
                update = node.invoke(state) if hasattr(node, "invoke") else node(state)
                if update:
                    state.update(update)
                if current in self._conditional_edges:
                    router, mapping = self._conditional_edges[current]
                    current = mapping[router(state)]
                else:
                    current = self._edges[current]
            return state

    class StateGraph:
        """Minimal ``StateGraph`` subset required by ``VoiceAgentProcessor``."""

        def __init__(self, _state_schema: Any) -> None:
            self._nodes: dict[str, Any] = {}
            self._edges: dict[str, str] = {}
            self._conditional_edges: dict[
                str, tuple[Callable[[dict[str, Any]], str], dict[str, str]]
            ] = {}

        def add_node(self, name: str, node: Any) -> None:
            self._nodes[name] = node

        def add_edge(self, source: str, target: str) -> None:
            self._edges[source] = target

        def add_conditional_edges(
            self,
            source: str,
            path: Callable[[dict[str, Any]], str],
            path_map: dict[str, str],
        ) -> None:
            self._conditional_edges[source] = (path, path_map)

        def compile(self, **_kwargs: Any) -> _CompiledGraph:
            return _CompiledGraph(self._nodes, self._edges, self._conditional_edges)


def require_official_langgraph() -> None:
    """Fail fast when the x64 production environment lacks LangGraph."""

    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError(
            "Official LangGraph is not installed. Use langgraph_agent_x64/requirements.txt "
            "with a 64-bit Python 3.10+ interpreter."
        )
