# =============================================================================
# 【维护注释】文件：langgraph_agent_x64/run_agent.py
# 所属层次：x64 智能体启动层
# 主要职责：负责 64 位智能体/MCP 进程启动和环境隔离，避免与 x86 CLR/ADK 依赖冲突。
# 项目内直接依赖：shared_runtime.architecture, shared_runtime.env_file, shared_runtime.logging_config, shared_runtime.observability
# 主要公开符号：main
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Production launcher for the 64-bit LangGraph voice agent."""

from __future__ import annotations

from shared_runtime.architecture import require_windows_x64_python310_plus
from shared_runtime.env_file import load_env_file
from shared_runtime.logging_config import configure_project_logging
from shared_runtime.observability import initialize_observability
from shared_runtime.sqlite_compat import ensure_healthy_state_db


def main() -> int:
    load_env_file(".env.agent")
    require_windows_x64_python310_plus()
    configure_project_logging(log_dir="logs/langgraph_agent_x64")
    ensure_healthy_state_db()
    initialize_observability("hytera-voice-agent")
    from voice_agent.agent.framework import require_official_langgraph

    require_official_langgraph()
    from voice_agent.server import run_server

    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
